"""Pydantic schemas for the inference vertical (Ollama call telemetry)."""
from typing import Optional

from pydantic import BaseModel


class OllamaInferenceLog(BaseModel):
    """One row in ollama_inference_logs. Logging-only — there is no read schema."""
    model: str
    request_id: Optional[str] = None
    vertical: Optional[str] = None
    tool_context: Optional[str] = None
    prompt_preview: Optional[str] = None  # truncated to 200 chars by the repo
    cache_hit: bool = False
    triage_bypassed: bool = False
    load_duration_ms: Optional[int] = None
    prompt_eval_count: Optional[int] = None
    prompt_eval_duration_ms: Optional[int] = None
    eval_count: Optional[int] = None
    eval_duration_ms: Optional[int] = None
    total_duration_ms: Optional[int] = None
    tokens_per_second: Optional[float] = None
    done_reason: Optional[str] = None
    success: bool = True
    error_message: Optional[str] = None
