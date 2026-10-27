"""
Data Analysis Router
POST /api/v1/data/analyze  - Analyze a dataset; streams NDJSON progress
                             events ({"stage": ...}) then the final result.
POST /api/v1/data/chat     - Ask a follow-up question about the analyzed dataset.
POST /api/v1/data/export   - Export analysis results as an Excel workbook for Power BI.
"""

import asyncio
import json

from fastapi import APIRouter, File, UploadFile, HTTPException
from fastapi.responses import Response, StreamingResponse

from models.schemas import DataAnalysisData, DataChatRequest, DataChatResponse
from services.data_analysis_service import analyze_dataset_stream, chat_about_dataset
from services.export_service import build_powerbi_workbook
from utils.file_utils import save_upload_to_temp, cleanup_temp_file
from utils.logging_utils import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/data")

ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xls"}

# Keepalive tuning for proxied streaming (see event_stream below).
KEEPALIVE_SECONDS = 10.0
KEEPALIVE_LINE = " " * 2048 + "\n"

# Cloudflare and nginx will buffer a streaming response unless told not to,
# which defeats the keepalive entirely - the bytes never leave the edge.
STREAM_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


@router.post("/analyze", summary="Analyze dataset (streams real pipeline stages, then the result)")
async def data_analyze(file: UploadFile = File(...)):
    _validate_file(file)
    logger.info("Dataset upload received: %s", file.filename)

    tmp_path = await save_upload_to_temp(file)

    async def event_stream():
        # Run the analysis pipeline in a background task and consume its events
        # via a queue.  While waiting for each event we yield padded blank NDJSON
        # lines every 10 s as a keepalive so Cloudflare / nginx / other proxies
        # don't tear down the connection during long ML/LLM stages.  The padding
        # matters: edge proxies coalesce tiny writes, so a bare "\n" can sit in a
        # buffer and never reach the client.  The client trims each line and
        # skips empty ones, so whitespace is inert.
        queue: asyncio.Queue = asyncio.Queue()

        async def _run_pipeline() -> None:
            try:
                async for event in analyze_dataset_stream(tmp_path, file.filename):
                    await queue.put(event)
            except Exception as exc:
                logger.error("Data analysis pipeline error: %s", exc, exc_info=True)
                await queue.put({"status": "error", "detail": f"Data analysis failed: {exc}"})
            finally:
                await queue.put(None)  # sentinel - signals the consumer to stop

        pipeline_task = asyncio.create_task(_run_pipeline())
        get_task: asyncio.Future = asyncio.ensure_future(queue.get())
        try:
            while True:
                # Wait up to 10 s for the next event; on timeout send a keepalive
                # line and loop (asyncio.wait never cancels get_task).
                done, _ = await asyncio.wait({get_task}, timeout=KEEPALIVE_SECONDS)
                if not done:
                    yield KEEPALIVE_LINE
                    continue
                event = get_task.result()
                if event is None:
                    break
                yield json.dumps(event, default=str) + "\n"
                get_task = asyncio.ensure_future(queue.get())
        finally:
            get_task.cancel()
            if not pipeline_task.done():
                pipeline_task.cancel()
                try:
                    await pipeline_task
                except (asyncio.CancelledError, Exception):
                    pass
            cleanup_temp_file(tmp_path)

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers=STREAM_HEADERS,
    )


@router.post("/chat", response_model=DataChatResponse, summary="Ask AI a question about the dataset")
async def data_chat(body: DataChatRequest):
    if not body.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty.")
    try:
        answer = await chat_about_dataset(body.question, body.context)
    except Exception as exc:
        logger.error("Chat failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Chat failed: {exc}")

    return DataChatResponse(status="success", data={"answer": answer})


@router.post("/export", summary="Export analysis results as an Excel workbook for Power BI")
async def data_export(body: DataAnalysisData):
    try:
        xlsx_bytes = await asyncio.to_thread(build_powerbi_workbook, body)
    except Exception as exc:
        logger.error("Export failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Export failed: {exc}")

    safe_stem = "".join(
        ch for ch in (body.filename.rsplit(".", 1)[0] or "dataset") if ch.isalnum() or ch in "-_ "
    ).strip() or "dataset"
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{safe_stem}_powerbi.xlsx"'},
    )


def _validate_file(file: UploadFile) -> None:
    import os
    if not (file.filename or "").strip():
        raise HTTPException(status_code=400, detail="Filename is required.")
    ext = os.path.splitext(os.path.basename(file.filename or ""))[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. Allowed: {ALLOWED_EXTENSIONS}",
        )
