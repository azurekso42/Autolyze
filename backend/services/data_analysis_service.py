"""
Data Analysis Service

Pipeline per the capstone methodology - all computation happens in
Python/Apache Spark; the LLM only interprets the precomputed results:

  file -> Apache Spark ETL (cleaning + feature engineering) -> summary stats
       -> demand forecasting (Linear Regression / Random Forest /
          Gradient Boosting via Spark MLlib, chronological split,
          RMSE / MAE / R2)
       -> customer segmentation (K-Means on RFM features, k chosen
          by Silhouette Coefficient)
       -> LLM interprets the computed results -> response

When Spark (or Java) is unavailable, the same pipeline runs on
pandas/scikit-learn as a fallback.
"""

from __future__ import annotations

import asyncio
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import numpy as np

from sklearn.cluster import KMeans
from sklearn.ensemble import HistGradientBoostingRegressor, RandomForestRegressor
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score, silhouette_score
from sklearn.preprocessing import StandardScaler

from models.schemas import DataAnalysisData, DataSummary
from services import analytics_policy as policy
from services.analytics_policy import ML_MAX_ROWS
from services.llm_service import call_llm, extract_json_from_llm
from utils.logging_utils import get_logger

logger = get_logger(__name__)

MAX_CORRELATIONS = 5
MAX_PROMPT_CHARS = 5000
PREVIEW_ROWS     = 25
# CSVs above this size skip pandas and are ingested directly by Spark
LARGE_CSV_BYTES  = int(os.getenv("AUTOLYZE_LARGE_CSV_MB", "300")) * 1024 * 1024

# Spark only earns its overhead at scale. Measured on 5,000 rows: Spark ETL
# 30.1s / forecasting 11.6s / segmentation 6.3s, versus 0.0s / 1.6s / 0.2s for
# pandas + sklearn - roughly 27x slower on data that fits in memory. Below this
# row count the distributed path is pure cost, so don't pay it.
SPARK_MIN_ROWS   = int(os.getenv("AUTOLYZE_SPARK_MIN_ROWS", "1000000"))

ANALYSIS_PROMPT = """
You are a senior data analyst. Every number below was already computed by the
analytics engine. Your ONLY job is to interpret these precomputed results for
a business audience - do NOT perform or invent any calculations of your own.

ANALYTICS ENGINE: {engine}

DATASET SUMMARY (computed):
{summary}

DEMAND FORECASTING RESULTS (computed):
{forecast}

CUSTOMER SEGMENTATION RESULTS (computed):
{segments}

Provide:
1. Exactly 5 concise, specific insights interpreting the results above
   (what the model metrics mean, which model to trust, what the segments
   represent, what the correlations suggest for the business).
2. Exactly 5 recommended follow-up questions a business analyst might ask.

Respond with ONLY valid JSON in this exact structure:
{{
  "insights": [
    "insight 1",
    "insight 2",
    "insight 3",
    "insight 4",
    "insight 5"
  ],
  "recommended_questions": [
    "question 1",
    "question 2",
    "question 3",
    "question 4",
    "question 5"
  ]
}}

No explanation. No markdown fences. Just the JSON object.
"""

CHAT_PROMPT = """
You are a data analyst assistant. A user has uploaded a dataset and is asking
a question about it. The context below contains results already computed by
the analytics engine - interpret them; do not perform new calculations or
invent numbers that are not in the context.

DATASET CONTEXT:
{context}

USER QUESTION:
{question}

Answer concisely and specifically based on the context provided.
If you cannot answer from the context alone, say so honestly.
Respond in 1-3 sentences. Do not use markdown. Plain text only.
"""


@dataclass(frozen=True)
class ColumnRoles:
    """Which column plays which role in a dataset.

    Detected once per analysis and threaded through every stage. Both
    detections scan or fully parse a column, and five stages need them:
    forecasting, segmentation, the summary charts, the forward projection and
    the RFM build. Re-deriving them per stage cost four redundant datetime
    parses of the same column on every upload.
    """
    time_col:     Optional[str]
    parsed_time:  Optional[pd.Series]
    measure:      Optional[pd.Series]
    measure_name: Optional[str]

    @classmethod
    def detect(cls, df: pd.DataFrame) -> "ColumnRoles":
        time_col, parsed_time = _find_datetime_col(df)
        measure, measure_name = _pick_measure(df)
        return cls(time_col, parsed_time, measure, measure_name)

    def aligned_to(self, df: pd.DataFrame) -> "ColumnRoles":
        """Restrict the detected series to df's index.

        Stages that sample or sort still hold the roles detected on the full
        frame. Pandas would align these on the union of both indexes, quietly
        reintroducing the rows the caller just dropped, so narrow them first.
        """
        return ColumnRoles(
            self.time_col,
            None if self.parsed_time is None else self.parsed_time.reindex(df.index),
            None if self.measure is None else self.measure.reindex(df.index),
            self.measure_name,
        )


async def analyze_dataset_stream(file_path: str, filename: str):
    """Async generator: yields {"stage": ...} progress events as each real
    pipeline step starts, then a final {"status": "success", "data": ...}."""
    sdf = None
    time_col = None
    total_rows = None
    # Scale-up factor for summed aggregates when the pandas frame is a sample
    # of a larger dataset (see the large-file branch below). 1.0 = whole frame.
    scale = 1.0
    engine = "pandas + scikit-learn (Spark unavailable)"

    file_size = os.path.getsize(file_path)
    if Path(filename).suffix.lower() == ".csv" and file_size > LARGE_CSV_BYTES:
        # Too big for pandas: Spark reads/cleans the file directly; only a
        # bounded sample comes back for the summary/preview/charts.
        yield {"stage": f"Large file ({file_size / 1e9:.1f} GB) - Apache Spark is reading it directly..."}
        logger.info("Large CSV (%d bytes): Spark direct ingestion for %s", file_size, filename)
        try:
            from services import spark_engine
            sdf, df, time_col, total_rows, sample_frac = await asyncio.to_thread(
                spark_engine.spark_etl_file, file_path)
            # Only a bounded sample came back as pandas, so every summed chart
            # built from it is short by that fraction. Report the row count of
            # the whole file next to totals that only cover 1/scale of it and
            # the dashboard contradicts itself, so scale the sums back up.
            scale = 1.0 / sample_frac if sample_frac > 0 else 1.0
            engine = "Apache Spark (direct large-file ingestion + Spark MLlib)"
        except Exception as exc:
            logger.error("Spark direct ingestion failed: %s", exc, exc_info=True)
            raise ValueError(
                "This file is too large to analyze without Apache Spark, "
                f"and the Spark pipeline failed: {exc}")
    else:
        yield {"stage": "Reading dataset..."}
        logger.info("Loading dataset: %s", filename)
        # Heavy pandas/sklearn/Spark work runs in a thread so the event loop
        # stays responsive (other requests, and flushing these progress events).
        df = await asyncio.to_thread(_load_dataframe, file_path, filename)
        logger.info("Dataset loaded: %d rows x %d columns", *df.shape)

        # ── Computed analytics: Spark above the threshold, pandas below ────
        if df.shape[0] >= SPARK_MIN_ROWS:
            yield {"stage": f"Cleaning {df.shape[0]:,} rows (Apache Spark ETL)..."}
            try:
                from services import spark_engine
                sdf, df, time_col = await asyncio.to_thread(spark_engine.spark_etl, df)
                engine = "Apache Spark (distributed ETL + Spark MLlib)"
            except Exception as exc:
                logger.warning("Spark pipeline unavailable (%s); falling back to pandas/sklearn.", exc)
                df = await asyncio.to_thread(_clean_dataframe, df)
        else:
            yield {"stage": f"Cleaning {df.shape[0]:,} rows..."}
            logger.info(
                "%d rows is below the %d-row Spark threshold; using pandas/sklearn.",
                df.shape[0], SPARK_MIN_ROWS,
            )
            df = await asyncio.to_thread(_clean_dataframe, df)
            engine = "pandas + scikit-learn"

    # Detect the time column and the business measure once; every stage below
    # takes them as arguments rather than re-deriving them from the frame.
    roles = await asyncio.to_thread(ColumnRoles.detect, df)

    # Forecasting and segmentation are independent, so they run concurrently
    yield {"stage": "Training forecasting models + segmenting customers (parallel)..."}
    if sdf is not None:
        from services import spark_engine
        forecasting, segmentation = await asyncio.gather(
            asyncio.to_thread(spark_engine.spark_forecasting, sdf, time_col),
            asyncio.to_thread(spark_engine.spark_segmentation, sdf, time_col),
        )
        sdf.unpersist()
    else:
        forecasting, segmentation = await asyncio.gather(
            asyncio.to_thread(_run_forecasting, df, roles),
            asyncio.to_thread(_run_segmentation, df, roles),
        )

    ml_results = {"forecasting": forecasting, "segmentation": segmentation, "engine": engine}

    yield {"stage": "Computing summary statistics and charts..."}
    summary = await asyncio.to_thread(_build_summary, df, roles, scale)
    if total_rows is not None:
        # Large-file path: df is a bounded sample; report the true row count
        summary.rows = total_rows

    yield {"stage": "Projecting the forward forecast..."}
    summary.forecast = _make_json_safe(
        await asyncio.to_thread(_run_forward_forecast, df, roles, scale))

    forecast_text = _forecasting_to_text(forecasting)
    forward_text = _forward_to_text(summary.forecast)
    if forward_text:
        forecast_text += "\n" + forward_text
    prompt = ANALYSIS_PROMPT.format(
        engine=engine,
        summary=_truncate(_summary_to_text(df, summary), MAX_PROMPT_CHARS // 2),
        forecast=_truncate(forecast_text, 1400),
        segments=_truncate(_segmentation_to_text(segmentation), 1200),
    )

    yield {"stage": "AI is interpreting the computed results..."}
    logger.info("Sending computed results to LLM for interpretation.")
    llm_available = True
    try:
        llm_out = await call_llm(prompt, temperature=0.4, max_tokens=2048)
        insights, questions = _parse_llm_output(llm_out.content)
    except Exception as exc:
        # LLM being down should not discard the computed analytics
        logger.warning("LLM interpretation failed (%s); returning computed results only.", exc)
        insights = ["AI interpretation unavailable (LLM not reachable) - showing computed results only."]
        questions = []
        llm_available = False

    logger.info("Analysis complete: %d insights, %d questions.", len(insights), len(questions))

    result = DataAnalysisData(
        filename=filename,
        summary=summary,
        insights=insights,
        recommended_questions=questions,
        ml_results=_make_json_safe(ml_results),
        llm_available=llm_available,
    )
    yield {"status": "success", "data": result.model_dump()}


# The insights path needs JSON, so call_llm asks for JSON by default. Chat
# needs prose, and the default would otherwise contradict CHAT_PROMPT's "plain
# text only" - the system message wins, and the user is shown a raw
# {"answer": "..."} envelope instead of the sentence inside it.
CHAT_SYSTEM_PROMPT = (
    "You are a precise data analysis assistant. "
    "Reply with plain prose only. "
    "Never use JSON, markdown, code fences, or bullet points."
)

# Field names a model reaches for when it wraps prose in an object anyway.
_ANSWER_KEYS = ("answer", "response", "text", "content", "message", "reply")


def _unwrap_answer(raw: str) -> str:
    """Return the prose from a reply, unwrapping a JSON envelope if present.

    Instructing the model is not a guarantee, so a wrapped answer is unwrapped
    rather than shown to the user as raw JSON.
    """
    text = (raw or "").strip()
    if not (text.startswith("{") or text.startswith("```")):
        return text
    try:
        obj = extract_json_from_llm(text)
    except ValueError:
        return text
    if not isinstance(obj, dict):
        return text
    for key in _ANSWER_KEYS:
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    # An object with exactly one string value is unambiguous whatever it is called.
    strings = [v.strip() for v in obj.values() if isinstance(v, str) and v.strip()]
    if len(strings) == 1:
        return strings[0]
    logger.warning("Chat reply was JSON with no recognizable answer field: %.200s", text)
    return text


async def chat_about_dataset(question: str, context: str) -> str:
    prompt = CHAT_PROMPT.format(context=_truncate(context, 3000), question=question)
    llm_out = await call_llm(prompt, temperature=0.5, max_tokens=1024,
                             system_prompt=CHAT_SYSTEM_PROMPT)
    return _unwrap_answer(llm_out.content)


def _load_dataframe(file_path: str, filename: str) -> pd.DataFrame:
    ext = Path(filename).suffix.lower()
    try:
        if ext == ".csv":
            try:
                # pyarrow parses large CSVs multi-threaded (much faster)
                df = pd.read_csv(file_path, engine="pyarrow")
            except Exception:
                df = pd.read_csv(file_path)
        elif ext in {".xlsx", ".xls"}:
            df = pd.read_excel(file_path)
        else:
            raise ValueError(f"Unsupported dataset format: '{ext}'")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Could not parse dataset '{filename}': {exc}")

    if df.empty:
        raise ValueError("The uploaded dataset is empty.")

    df.columns = [str(c).strip() for c in df.columns]
    return df


def _clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Data cleaning per methodology 3.2.1: dedup, apply the validity rules,
    drop rows with no identifier, drop high-null columns, then fill the
    remaining nulls (numeric: median, categorical: mode)."""
    before = len(df)
    df = df.drop_duplicates()
    if len(df) < before:
        logger.info("Cleaning: removed %d duplicate rows.", before - len(df))

    # Validation rules (3.2.1). Applied before imputation, so an invalid value
    # is never carried into the median that fills its neighbours.
    for col in df.select_dtypes(include="number").columns:
        rule = policy.validity_rule(col)
        if rule is None:
            continue
        keep = df[col] > 0 if rule == "positive" else df[col] >= 0
        keep = keep | df[col].isna()      # nulls are the imputation's business
        dropped = int((~keep).sum())
        if not dropped:
            continue
        if not policy.rule_is_enforceable(dropped, len(df)):
            logger.info(
                "Cleaning: '%s' fails %s on %d of %d rows (%.1f%%); treating "
                "that as a state the column encodes, not an error, and keeping "
                "the rows.", col, rule, dropped, len(df), 100 * dropped / len(df))
            continue
        df = df[keep]
        logger.info("Cleaning: removed %d rows failing %s on '%s'.",
                    dropped, rule, col)

    # Identifiers are dropped, never imputed - see policy.never_impute.
    id_cols = [c for c in df.columns if policy.never_impute(c) and df[c].isna().any()]
    if id_cols:
        before_ids = len(df)
        df = df.dropna(subset=id_cols)
        logger.info("Cleaning: removed %d rows with no %s.",
                    before_ids - len(df), ", ".join(id_cols))

    df = df.dropna(axis=1, thresh=int(len(df) * policy.MIN_NON_NULL_FRACTION))

    for col in df.select_dtypes(include="number").columns:
        df[col] = df[col].fillna(df[col].median())
    for col in df.select_dtypes(exclude="number").columns:
        mode = df[col].mode()
        df[col] = df[col].fillna(mode.iloc[0] if not mode.empty else "unknown")

    return df


def _build_summary(df: pd.DataFrame, roles: ColumnRoles | None = None,
                   scale: float = 1.0) -> DataSummary:
    roles = roles or ColumnRoles.detect(df)
    dtypes      = {col: str(dtype) for col, dtype in df.dtypes.items()}
    null_counts = df.isnull().sum().to_dict()

    numeric_df    = df.select_dtypes(include="number")
    numeric_stats: Dict[str, Any] = {}
    if not numeric_df.empty:
        desc = numeric_df.describe().to_dict()
        numeric_stats = _make_json_safe(desc)

    top_correlations: List[Dict[str, Any]] = []
    if numeric_df.shape[1] >= 2:
        corr_matrix = numeric_df.corr().abs()
        upper = corr_matrix.where(
            np.triu(np.ones(corr_matrix.shape), k=1).astype(bool)
        )
        pairs = (
            upper.stack()
            .dropna()  # pandas >=3 stack() keeps the masked NaN cells
            .reset_index()
            .rename(columns={"level_0": "col_a", "level_1": "col_b", 0: "correlation"})
            .sort_values("correlation", ascending=False)
            .head(MAX_CORRELATIONS)
        )
        top_correlations = _make_json_safe(pairs.to_dict(orient="records"))

    preview_df   = df.head(PREVIEW_ROWS)
    preview_rows = _make_json_safe(preview_df.values.tolist())

    distribution, distribution_label = _build_distribution(df, numeric_df, scale)
    timeseries, category_totals = _build_charts(df, roles, scale)

    return DataSummary(
        rows=int(df.shape[0]),
        columns=int(df.shape[1]),
        column_names=list(df.columns),
        dtypes=dtypes,
        null_counts={k: int(v) for k, v in null_counts.items()},
        numeric_stats=numeric_stats,
        top_correlations=top_correlations,
        preview_rows=preview_rows,
        distribution=distribution,
        distribution_label=distribution_label,
        timeseries=timeseries,
        category_totals=category_totals,
    )


def _pick_measure(df: pd.DataFrame):
    """Pick the business measure to chart: a sales-like column, else
    quantity x price (revenue), else the highest-variance non-ID numeric."""
    numeric = df.select_dtypes(include="number")
    if numeric.empty:
        return None, None

    named = [c for c in numeric.columns if policy.MEASURE_HINT.search(c)]
    if named:
        return pd.to_numeric(df[named[0]], errors="coerce"), named[0]

    qty   = next((c for c in numeric.columns if policy.QTY_HINT.search(c)), None)
    price = next((c for c in numeric.columns if policy.PRICE_HINT.search(c)), None)
    if qty and price:
        rev = pd.to_numeric(df[qty], errors="coerce") * pd.to_numeric(df[price], errors="coerce")
        return rev, f"Revenue ({qty} x {price})"
    if price:
        return pd.to_numeric(df[price], errors="coerce"), price

    candidates = [
        c for c in numeric.columns
        if not policy.ID_HINT.search(c)
        and not policy.is_identifier(df[c].nunique(), len(df))
    ] or list(numeric.columns)
    col = numeric[candidates].var().idxmax()
    return numeric[col], col


def _aggregate_timeseries(df: pd.DataFrame, roles: ColumnRoles, scale: float = 1.0):
    """Sum the business measure per period. Returns (series, freq, fmt, name)
    with a regular DatetimeIndex, or None when no measure/date is found."""
    if roles.measure is None or roles.time_col is None:
        return None
    t = pd.DataFrame({"t": roles.parsed_time, "v": roles.measure}).dropna()
    if len(t) < 2:
        return None
    span_days = (t["t"].max() - t["t"].min()).days
    freq, fmt = (
        ("D", "%Y-%m-%d") if span_days <= 62 else
        ("W", "%Y-%m-%d") if span_days <= 270 else
        ("MS", "%Y-%m")
    )
    g = t.groupby(pd.Grouper(key="t", freq=freq))["v"].sum()
    g = g.reindex(pd.date_range(g.index.min(), g.index.max(), freq=freq), fill_value=0.0)
    if scale != 1.0:
        g = g * scale
    return g, freq, fmt, roles.measure_name


def _sampled_note(scale: float) -> str:
    """Suffix marking a chart whose totals are estimated, not counted."""
    if scale == 1.0:
        return ""
    return f" (estimated from a {100.0 / scale:.1f}% sample)"


def _build_charts(df: pd.DataFrame, roles: ColumnRoles, scale: float = 1.0):
    """Aggregations for the UI charts: measure summed over time (line) and
    measure summed per top category (bar). Empty dicts when not applicable."""
    timeseries: Dict[str, Any] = {}
    category_totals: Dict[str, Any] = {}
    try:
        measure, name = roles.measure, roles.measure_name
        if measure is None:
            return timeseries, category_totals

        note = _sampled_note(scale)

        agg = _aggregate_timeseries(df, roles, scale)
        if agg is not None:
            g, freq, fmt, _ = agg
            timeseries = {
                "title": f"{name} over time{note}",
                "labels": [d.strftime(fmt) for d in g.index],
                "values": [round(float(v), 2) for v in g.values],
            }

        cat_cols = [
            c for c in df.select_dtypes(include="object").columns
            if 2 <= df[c].nunique() <= 50
        ]
        if cat_cols:
            col = min(cat_cols, key=lambda c: df[c].nunique())
            g = measure.groupby(df[col].astype(str)).sum().sort_values(ascending=False).head(8)
            category_totals = {
                "title": f"{name} by {col}{note}",
                "labels": [str(k) for k in g.index],
                "values": [round(float(v * scale), 2) for v in g.values],
            }
    except Exception as exc:
        logger.warning("Chart data build failed (non-fatal): %s", exc)
    return _make_json_safe(timeseries), _make_json_safe(category_totals)


def _build_distribution(df: pd.DataFrame, numeric_df: pd.DataFrame, scale: float = 1.0):
    """Real distribution counts for the UI doughnut: value counts of the
    lowest-cardinality categorical column (top 6 + Other), else counts per
    equal-width bin of the first numeric column."""
    try:
        cat_cols = [
            c for c in df.select_dtypes(include="object").columns
            if 2 <= df[c].nunique() <= 50
        ]
        note = _sampled_note(scale)
        if cat_cols:
            col = min(cat_cols, key=lambda c: df[c].nunique())
            counts = df[col].value_counts()
            dist = {str(k): int(round(v * scale)) for k, v in counts.head(6).items()}
            if len(counts) > 6:
                dist["Other"] = int(round(counts.iloc[6:].sum() * scale))
            return dist, f"{col} distribution{note}"

        if not numeric_df.empty:
            col = numeric_df.columns[0]
            # Quantile bins: robust to outliers/skew (equal-width bins put
            # nearly all rows in one bin when extremes are present)
            binned = pd.qcut(numeric_df[col].dropna(), q=6, duplicates="drop")
            dist = {
                f"{iv.left:g} to {iv.right:g}": int(round(n * scale))
                for iv, n in binned.value_counts().sort_index().items()
                if n > 0
            }
            if len(dist) >= 2:
                return dist, f"{col} distribution{note}"
    except Exception as exc:
        logger.warning("Distribution build failed (non-fatal): %s", exc)
    return {}, None


def _summary_to_text(df: pd.DataFrame, summary: DataSummary) -> str:
    lines = [
        f"Shape      : {summary.rows} rows x {summary.columns} columns",
        f"Columns    : {', '.join(summary.column_names)}",
    ]
    if summary.numeric_stats:
        lines.append(f"Numeric stats (describe):\n{df.select_dtypes(include='number').describe().to_string()}")
    if summary.top_correlations:
        corr_lines = []
        for c in summary.top_correlations:
            corr_value = _format_numeric_value(c.get("correlation"))
            if corr_value is None:
                continue
            corr_lines.append(
                f"  {c.get('col_a')} <-> {c.get('col_b')}: r={corr_value}"
            )
        if corr_lines:
            lines.append("Top correlations:\n" + "\n".join(corr_lines))
    return "\n".join(lines)


def _parse_llm_output(content: str):
    try:
        data      = extract_json_from_llm(content)
        insights  = [str(i) for i in data.get("insights", [])][:5]
        questions = [str(q) for q in data.get("recommended_questions", [])][:5]
        return insights, questions
    except (ValueError, KeyError, TypeError) as exc:
        logger.error("Failed to parse LLM analysis output: %s", exc)
        return (
            ["Unable to generate insights - LLM returned an unexpected format."],
            ["Please try re-uploading the dataset."],
        )


# ═══════════════════════════════════════════════════════════════════════════
# DEMAND FORECASTING (methodology 3.3.x)
# Linear Regression / Random Forest / Gradient Boosting, chronological
# 80/20 split, evaluated with RMSE, MAE and R².
# ═══════════════════════════════════════════════════════════════════════════

def _find_datetime_col(df: pd.DataFrame):
    """Return (column_name, parsed_series) for the first date-like column."""
    for col in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            return col, df[col]
    for col in df.select_dtypes(include="object").columns:
        # Probe a sample first so non-date text columns are rejected cheaply;
        # only the winning column gets a full parse.
        probe = df[col].dropna()
        if probe.empty:
            continue
        if len(probe) > policy.DATE_PROBE_ROWS:
            probe = probe.sample(policy.DATE_PROBE_ROWS, random_state=42)
        if pd.to_datetime(probe, errors="coerce").notna().mean() > policy.DATE_PARSE_THRESHOLD:
            parsed = pd.to_datetime(df[col], errors="coerce")
            if parsed.notna().mean() > policy.DATE_PARSE_THRESHOLD:
                return col, parsed

    # No timestamp column, but the parts may still be there. Without this the
    # chronological split falls back to row order, which on a table sorted by
    # series is not chronological and quietly evaluates a different question.
    parts = policy.pick_date_parts(df.columns)
    if parts:
        year, month, day = parts
        if all(pd.api.types.is_numeric_dtype(df[c]) for c in (year, month, day)):
            composed = pd.to_datetime(
                {"year": df[year], "month": df[month], "day": df[day]},
                errors="coerce")
            if composed.notna().mean() > policy.DATE_PARSE_THRESHOLD:
                logger.info("Composed a timestamp from '%s', '%s' and '%s' so the "
                            "split is chronological rather than row order.",
                            year, month, day)
                return policy.COMPOSED_DATE_COL, composed
    return None, None


def _run_forecasting(df: pd.DataFrame, roles: ColumnRoles | None = None) -> dict:
    result: dict = {"skipped": False, "skip_reason": None}
    try:
        roles = roles or ColumnRoles.detect(df)
        # Row-sampling is sound here: each row is an independent training
        # example, so a uniform sample trains the same relationship.
        if len(df) > ML_MAX_ROWS:
            df = df.sample(ML_MAX_ROWS, random_state=42)
            logger.info("Forecasting: dataset sampled to %d rows.", ML_MAX_ROWS)
        df = df.copy()

        roles = roles.aligned_to(df)
        time_col, parsed_time = roles.time_col, roles.parsed_time

        numeric_cols = df.select_dtypes(include="number").columns.tolist()
        if not numeric_cols:
            return {"skipped": True, "skip_reason": "No numeric columns found."}

        # Target: prefer a sales-like column name (strongest keyword wins),
        # else highest variance
        target_col = policy.pick_target(numeric_cols) or df[numeric_cols].var().idxmax()

        # Feature engineering (3.3.0): calendar features from the date column
        if time_col is not None:
            df["_month"]   = parsed_time.dt.month
            df["_weekday"] = parsed_time.dt.dayofweek
            df["_day"]     = parsed_time.dt.day

        # Encode low-cardinality categoricals; drop IDs / near-unique columns
        for col in df.select_dtypes(exclude="number").columns:
            if col != time_col and policy.is_encodable(df[col].nunique(), len(df)):
                df[col] = pd.factorize(df[col])[0]
            else:
                df = df.drop(columns=[col])

        feature_cols = [
            c for c in df.select_dtypes(include="number").columns
            if c != target_col
        ]
        if not feature_cols:
            return {"skipped": True, "skip_reason": "No usable feature columns besides the target."}

        # Chronological split (3.3.5): sort by time, no shuffle - avoids leakage
        if time_col is not None:
            df = df.assign(_t=parsed_time).sort_values("_t").drop(columns="_t")

        X = df[feature_cols].to_numpy(dtype=float)
        y = df[target_col].to_numpy(dtype=float)
        if not policy.can_split(len(df)):
            return {"skipped": True, "skip_reason": "Not enough rows to train and evaluate models."}
        split = policy.split_index(len(df))
        X_train, X_test = X[:split], X[split:]
        y_train, y_test = y[:split], y[split:]

        models = {
            "Linear Regression": LinearRegression(),
            "Random Forest": RandomForestRegressor(
                n_estimators=100, max_depth=12, random_state=42, n_jobs=-1),
            # Histogram-based gradient boosting: same algorithm family,
            # orders of magnitude faster on large datasets
            "Gradient Boosting": HistGradientBoostingRegressor(random_state=42),
        }

        metrics: dict = {}
        importances: dict = {}
        for name, model in models.items():
            try:
                model.fit(X_train, y_train)
                preds = model.predict(X_test)
                metrics[name] = {
                    "rmse": round(float(np.sqrt(mean_squared_error(y_test, preds))), 4),
                    "mae":  round(float(mean_absolute_error(y_test, preds)), 4),
                    "r2":   round(float(r2_score(y_test, preds)), 4),
                }
                logger.info("%s: RMSE=%.4f MAE=%.4f R2=%.4f", name,
                            metrics[name]["rmse"], metrics[name]["mae"], metrics[name]["r2"])
                if name == "Random Forest":
                    imp = dict(zip(feature_cols, model.feature_importances_))
                    importances = {
                        k: round(float(v), 4)
                        for k, v in sorted(imp.items(), key=lambda x: -x[1])[:10]
                    }
            except Exception as exc:
                logger.warning("%s failed: %s", name, exc)

        if not metrics:
            return {"skipped": True, "skip_reason": "All forecasting models failed to train."}

        result.update({
            "target_column": target_col,
            "time_column": time_col,
            "n_features": len(feature_cols),
            "train_rows": split,
            "test_rows": len(df) - split,
            "models": metrics,
            "best_model": min(metrics, key=lambda m: metrics[m]["rmse"]),
            "feature_importance": importances,
        })
    except Exception as exc:
        logger.warning("Forecasting pipeline error (non-fatal): %s", exc)
        return {"skipped": True, "skip_reason": str(exc)}
    return result


# ═══════════════════════════════════════════════════════════════════════════
# FORWARD FORECAST
# Projects the aggregated business measure beyond the last observed period
# (recursive gradient boosting on lag/seasonal/calendar features). Feeds the
# dashed extension of the dashboard timeseries chart.
# ═══════════════════════════════════════════════════════════════════════════

_FC_HORIZON = {"D": 28, "W": 12, "MS": 6}
_FC_SEASON  = {"D": 7,  "W": 4,  "MS": 12}


def _run_forward_forecast(df: pd.DataFrame, roles: ColumnRoles | None = None,
                          scale: float = 1.0) -> dict:
    try:
        roles = roles or ColumnRoles.detect(df)
        # The series is already scaled to full-dataset units by
        # _aggregate_timeseries, so the projection comes out in them too.
        agg = _aggregate_timeseries(df, roles, scale)
        if agg is None:
            return {}
        g, freq, fmt, name = agg
        season, horizon = _FC_SEASON[freq], _FC_HORIZON[freq]
        if len(g) < max(3 * season, 20):
            return {}

        times = list(g.index)
        vals = [float(v) for v in g.values]
        lags = [1, 2, 3, season]
        start = max(lags)

        future_idx = pd.date_range(times[-1], periods=horizon + 1, freq=freq)[1:]
        times_all = times + list(future_idx)

        def make_row(i: int, series: List[float]):
            ts = times_all[i]
            row = [series[i - l] for l in lags]
            row.append(sum(series[i - 3:i]) / 3)          # short rolling mean
            row += [ts.dayofweek, ts.month, ts.day, i]    # calendar + trend
            return row

        X = [make_row(i, vals) for i in range(start, len(vals))]
        y = vals[start:]
        model = HistGradientBoostingRegressor(random_state=42)
        model.fit(X, y)

        series = vals[:]
        preds: List[float] = []
        nonneg = min(vals) >= 0
        for _ in range(horizon):
            p = float(model.predict([make_row(len(series), series)])[0])
            if nonneg:
                p = max(p, 0.0)
            preds.append(round(p, 2))
            series.append(p)

        logger.info("Forward forecast: %d %s periods of '%s'.", horizon, freq, name)
        return {
            "title": f"{name} forecast{_sampled_note(scale)}",
            "labels": [d.strftime(fmt) for d in future_idx],
            "values": preds,
            "model": "Gradient Boosting (histogram-based)",
            "horizon": horizon,
            "freq": freq,
        }
    except Exception as exc:
        logger.warning("Forward forecast failed (non-fatal): %s", exc)
        return {}


def _forward_to_text(fc: dict) -> str:
    if not fc.get("values"):
        return ""
    vals = fc["values"]
    return (
        f"Forward forecast ({fc['horizon']} x {fc['freq']} periods, {fc['model']}): "
        f"'{fc['title']}' from {vals[0]} to {vals[-1]} "
        f"(mean {round(sum(vals) / len(vals), 2)})."
    )


# ═══════════════════════════════════════════════════════════════════════════
# CUSTOMER SEGMENTATION (methodology 3.4.0)
# K-Means on RFM features (fallback: all numeric features), k selected by
# the Silhouette Coefficient. Python computes the cluster profiles; the
# LLM interprets and labels them.
# ═══════════════════════════════════════════════════════════════════════════

def _build_rfm(df: pd.DataFrame, roles: ColumnRoles | None = None):
    """Return an RFM dataframe indexed by customer, or None if the dataset
    has no detectable customer / date / monetary columns."""
    roles = roles or ColumnRoles.detect(df)

    cust_col = policy.pick_customer_column(df.columns)
    if cust_col is None or roles.time_col is None:
        return None

    numeric_cols = df.select_dtypes(include="number").columns.tolist()
    choice = policy.pick_monetary(numeric_cols)
    if choice is None:
        return None
    if choice[0] == "direct":
        monetary = df[choice[1]]
    else:
        monetary = (pd.to_numeric(df[choice[1]], errors="coerce")
                    * pd.to_numeric(df[choice[2]], errors="coerce"))

    work = pd.DataFrame({
        "customer": df[cust_col],
        "date": roles.parsed_time,
        "monetary": pd.to_numeric(monetary, errors="coerce"),
    }).dropna()
    if work.empty or work["customer"].nunique() < policy.MIN_RFM_CUSTOMERS:
        return None

    now = work["date"].max()
    rfm = work.groupby("customer").agg(
        recency=("date", lambda s: (now - s.max()).days),
        frequency=("date", "count"),
        monetary=("monetary", "sum"),
    )
    return rfm


def _run_segmentation(df: pd.DataFrame, roles: ColumnRoles | None = None) -> dict:
    result: dict = {"skipped": False, "skip_reason": None}
    try:
        roles = roles or ColumnRoles.detect(df)

        # RFM is built on the whole frame, never on a row sample. Every RFM
        # feature aggregates over a customer's full history, so dropping rows
        # first would deflate frequency and monetary by the sampling fraction
        # and bias recency late - the segments would be wrong, not just noisy.
        # The aggregate collapses to one row per customer, so it is already
        # small; if a dataset somehow carries more customers than the cap, we
        # sample customers (whole histories intact), never transactions.
        rfm = _build_rfm(df, roles)
        if rfm is not None:
            if len(rfm) > ML_MAX_ROWS:
                rfm = rfm.sample(ML_MAX_ROWS, random_state=42)
                logger.info("Segmentation: sampled to %d customers.", ML_MAX_ROWS)
            # Signed log transform (3.3.0): frequency/monetary are heavily
            # skewed - without this a single whale customer becomes its own
            # cluster and inflates the silhouette score. Clustering uses the
            # transformed values; profiles report the raw ones.
            features = rfm.copy()
            for col in ("frequency", "monetary"):
                features[col] = np.sign(features[col]) * np.log1p(features[col].abs())
            profile_frame = rfm
            method = "K-Means on RFM (Recency/Frequency/Monetary, log-scaled)"
        else:
            # No RFM: rows are the entities, so row-sampling is the right cap.
            source = df.sample(ML_MAX_ROWS, random_state=42) if len(df) > ML_MAX_ROWS else df
            numeric = source.select_dtypes(include="number").dropna(axis=1)
            # Drop near-unique columns (IDs) that would dominate distance
            numeric = numeric.loc[:, [
                not policy.is_identifier(numeric[c].nunique(), len(numeric))
                for c in numeric.columns
            ]]
            if numeric.shape[1] < 2 or len(numeric) < policy.MIN_CLUSTER_ROWS:
                return {"skipped": True,
                        "skip_reason": "No RFM columns detected and not enough numeric features for clustering."}
            features = numeric.iloc[:, :policy.MAX_CLUSTER_FEATURES]
            method = "K-Means on numeric features (no customer/date columns found)"
            profile_frame = features

        X = StandardScaler().fit_transform(features)

        best_k, best_score, best_labels = None, -1.0, None
        for k in policy.k_candidates(len(features)):
            # n_init restarts K-Means and keeps the best inertia; the Spark
            # path loops seeds by hand to match, since MLlib has no equivalent.
            km = KMeans(n_clusters=k, n_init=policy.KMEANS_RESTARTS, random_state=42)
            labels = km.fit_predict(X)
            score = silhouette_score(X, labels,
                                     sample_size=min(5000, len(X)), random_state=42)
            logger.info("K-Means k=%d silhouette=%.4f", k, score)
            if score > best_score:
                best_k, best_score, best_labels = k, score, labels

        profiles = []
        grouped = profile_frame.assign(_seg=best_labels).groupby("_seg")
        for seg, grp in grouped:
            profiles.append({
                "segment": int(seg),
                "size": int(len(grp)),
                "profile": {c: round(float(grp[c].mean()), 2) for c in profile_frame.columns},
            })

        result.update({
            "method": method,
            "k": int(best_k),
            "silhouette": round(float(best_score), 4),
            "n_entities": int(len(features)),
            "segments": profiles,
        })
    except Exception as exc:
        logger.warning("Segmentation pipeline error (non-fatal): %s", exc)
        return {"skipped": True, "skip_reason": str(exc)}
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Text renderers - computed results -> prompt text for the LLM
# ═══════════════════════════════════════════════════════════════════════════

def _forecasting_to_text(fc: dict) -> str:
    if fc.get("skipped"):
        return f"Forecasting was skipped: {fc.get('skip_reason')}"
    lines = [
        f"Target: '{fc['target_column']}' predicted from {fc['n_features']} features "
        f"({fc['train_rows']} training rows, {fc['test_rows']} test rows, "
        f"chronological split{' on ' + repr(fc['time_column']) if fc.get('time_column') else ''})."
    ]
    for name, m in fc.get("models", {}).items():
        lines.append(f"{name}: RMSE={m['rmse']}, MAE={m['mae']}, R2={m['r2']}")
    lines.append(f"Best model by RMSE: {fc.get('best_model')}")
    if fc.get("feature_importance"):
        top = list(fc["feature_importance"].items())[:5]
        lines.append("Top predictive features: " + ", ".join(f"{k} ({v})" for k, v in top))
    return "\n".join(lines)


def _segmentation_to_text(seg: dict) -> str:
    if seg.get("skipped"):
        return f"Segmentation was skipped: {seg.get('skip_reason')}"
    lines = [
        f"{seg['method']}: {seg['n_entities']} entities grouped into k={seg['k']} "
        f"segments (Silhouette Coefficient = {seg['silhouette']})."
    ]
    for p in seg.get("segments", []):
        profile = ", ".join(f"{k}={v}" for k, v in p["profile"].items())
        lines.append(f"Segment {p['segment']} (n={p['size']}): {profile}")
    return "\n".join(lines)


def _make_json_safe(obj):
    if isinstance(obj, dict):
        return {k: _make_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_make_json_safe(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


def _format_numeric_value(value: Any, precision: int = 3) -> str | None:
    """Return a formatted numeric string, or None when the value is missing."""
    if value is None:
        return None
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric_value):
        return None
    return f"{numeric_value:.{precision}f}"


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n[... truncated ...]"
