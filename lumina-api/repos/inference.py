"""Repo for `ollama_inference_logs`. Write-only chokepoint for inference telemetry."""
import logging

from sqlalchemy import text

from db.postgres import engine
from schemas.inference import OllamaInferenceLog

logger = logging.getLogger(__name__)


def log_call(payload: OllamaInferenceLog) -> None:
    """
    Insert one row into ollama_inference_logs. Best-effort: never raises.
    Logging failure must not break the calling inference request.
    """
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO ollama_inference_logs (
                        request_id, model, vertical, tool_context, prompt_preview,
                        cache_hit, triage_bypassed,
                        load_duration_ms, prompt_eval_count, prompt_eval_duration_ms,
                        eval_count, eval_duration_ms, total_duration_ms,
                        tokens_per_second, done_reason, success, error_message
                    ) VALUES (
                        :request_id, :model, :vertical, :tool_context, :prompt_preview,
                        :cache_hit, :triage_bypassed,
                        :load_duration_ms, :prompt_eval_count, :prompt_eval_duration_ms,
                        :eval_count, :eval_duration_ms, :total_duration_ms,
                        :tokens_per_second, :done_reason, :success, :error_message
                    )
                    """
                ),
                {
                    **payload.model_dump(),
                    "prompt_preview": (payload.prompt_preview or "")[:200],
                },
            )
    except Exception as exc:
        logger.warning("ollama_inference_logs write failed: %s", exc)
