"""Pydantic schemas for the observability vertical (read-only)."""
from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict


class OllamaLogOut(BaseModel):
    id: int
    logged_at: datetime
    request_id: Optional[str] = None
    model: str
    vertical: Optional[str] = None
    tool_context: Optional[str] = None
    prompt_preview: Optional[str] = None
    cache_hit: bool = False
    triage_bypassed: bool = False
    load_duration_ms: Optional[int] = None
    prompt_eval_count: Optional[int] = None
    prompt_eval_duration_ms: Optional[int] = None
    eval_count: Optional[int] = None
    eval_duration_ms: Optional[int] = None
    total_duration_ms: Optional[int] = None
    tokens_per_second: Optional[Decimal] = None
    done_reason: Optional[str] = None
    success: bool = True
    error_message: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)
