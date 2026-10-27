"""
Pydantic schemas - shared request/response models for all endpoints.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


# Generic wrapper
class BaseResponse(BaseModel):
    status: str = Field(..., examples=["success"])
    data: Any = Field(default=None)


# Data Analysis
class DataSummary(BaseModel):
    rows:             int
    columns:          int
    column_names:     List[str]
    dtypes:           Dict[str, str]
    null_counts:      Dict[str, int]
    numeric_stats:    Dict[str, Any] = Field(default_factory=dict)
    top_correlations: List[Dict[str, Any]] = Field(default_factory=list)
    preview_rows:     List[Any] = Field(default_factory=list)
    # Computed distribution for the UI doughnut: label -> count
    distribution:       Dict[str, int] = Field(default_factory=dict)
    distribution_label: Optional[str] = None
    # Computed chart data: {"title": str, "labels": [...], "values": [...]}
    timeseries:      Dict[str, Any] = Field(default_factory=dict)
    category_totals: Dict[str, Any] = Field(default_factory=dict)
    # Forward forecast extending the timeseries: {"title", "labels",
    # "values", "model", "horizon", "freq"} - empty when not applicable
    forecast:        Dict[str, Any] = Field(default_factory=dict)


class DataAnalysisData(BaseModel):
    filename:             str
    summary:              DataSummary
    insights:             List[str] = Field(default_factory=list)
    recommended_questions: List[str] = Field(default_factory=list)
    # ── NEW: ML model results ──────────────────────────────────────────────
    ml_results:           Optional[Dict[str, Any]] = None
    # False when the LLM could not be reached (computed results still valid)
    llm_available:        bool = True

# Data Chat
class DataChatRequest(BaseModel):
    question: str
    context: str = Field(default="")

class DataChatResponse(BaseResponse):
    data: Dict[str, str]


# LLM (internal)
class LLMResponse(BaseModel):
    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
