"""File utilities for uploads and temporary storage."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from fastapi import HTTPException, UploadFile

from utils.logging_utils import get_logger

logger = get_logger(__name__)

# Chunk size for streaming uploads to disk (4 MB)
CHUNK_SIZE = 4 * 1024 * 1024
# 0 (default) = no size limit; set AUTOLYZE_MAX_UPLOAD_BYTES to enforce a cap
MAX_UPLOAD_BYTES = int(os.getenv("AUTOLYZE_MAX_UPLOAD_BYTES", "0"))
_SAFE_STEM_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SAFE_SUFFIX_RE = re.compile(r"^\.[A-Za-z0-9]{1,10}$")


async def save_upload_to_temp(file: UploadFile) -> str:
    """
    Stream an uploaded file to a named temporary file on disk.

    The caller is responsible for calling cleanup_temp_file() when done.
    A hard byte limit is enforced to reduce DoS risk from oversized uploads.

    Args:
        file: FastAPI UploadFile object.

    Returns:
        Absolute path to the written temp file.
    """
    suffix = _get_suffix(file.filename)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        await file.seek(0)
        total = 0
        while True:
            chunk = await file.read(CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if MAX_UPLOAD_BYTES and total > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"Uploaded file is too large. Maximum size is {MAX_UPLOAD_BYTES} bytes.",
                )
            tmp.write(chunk)
    except Exception:
        try:
            tmp.close()
        finally:
            cleanup_temp_file(tmp.name)
        raise
    finally:
        if not tmp.closed:
            tmp.close()

    logger.debug("Saved upload '%s' to temp file '%s'", file.filename, tmp.name)
    return tmp.name


def cleanup_temp_file(path: str) -> None:
    """
    Delete a temporary file from disk. Silently ignores missing files.

    Args:
        path: Absolute path returned by save_upload_to_temp().
    """
    try:
        if path and os.path.exists(path):
            os.unlink(path)
            logger.debug("Deleted temp file: %s", path)
    except OSError as exc:
        logger.warning("Could not delete temp file '%s': %s", path, exc)


def safe_filename_stem(filename: str | None) -> str:
    """Return a filesystem-safe stem derived from an uploaded filename."""
    base_name = os.path.basename(filename or "")
    stem = Path(base_name).stem
    stem = _SAFE_STEM_RE.sub("_", stem).strip("._")
    return stem[:120] or "upload"


def build_unique_output_path(base_dir: str, filename: str | None, extension: str) -> Path:
    """
    Build a unique path inside base_dir using a sanitized filename stem.
    Raises ValueError if the resolved path ever escapes base_dir.
    """
    base = Path(base_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)

    ext = extension if extension.startswith(".") else f".{extension}"
    if not _SAFE_SUFFIX_RE.match(ext):
        raise ValueError(f"Unsafe file extension: {extension!r}")

    stem = safe_filename_stem(filename)

    counter = 0
    while True:
        candidate_name = stem if counter == 0 else f"{stem}_{counter}"
        candidate = (base / f"{candidate_name}{ext}").resolve()
        try:
            candidate.relative_to(base)
        except ValueError as exc:
            raise ValueError("Refusing to write outside the target directory.") from exc

        if not candidate.exists():
            return candidate
        counter += 1


def _get_suffix(filename: str | None) -> str:
    """Extract a safe file extension to preserve in the temp filename."""
    if not filename:
        return ".tmp"
    base_name = os.path.basename(filename)
    suffix = Path(base_name).suffix.lower()
    if suffix and _SAFE_SUFFIX_RE.match(suffix):
        return suffix
    return ".tmp"
