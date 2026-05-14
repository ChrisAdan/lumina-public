"""Repo for observability — read-only queries against ollama_inference_logs."""
from typing import Optional

from sqlalchemy import text

from db.postgres import engine
from schemas.observability import OllamaLogOut

COLD_LOAD_THRESHOLD_MS = 500  # load_duration_ms above this = cold load event


def list_logs(
    *,
    tool_context: Optional[str] = None,
    model: Optional[str] = None,
    success: Optional[bool] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[OllamaLogOut]:
    sql = """
        SELECT *
        FROM ollama_inference_logs
        WHERE (:tool_context IS NULL OR tool_context = :tool_context)
          AND (:model        IS NULL OR model = :model)
          AND (:success      IS NULL OR success = :success)
        ORDER BY logged_at DESC
        LIMIT :limit OFFSET :offset
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(sql),
            {
                "tool_context": tool_context,
                "model": model,
                "success": success,
                "limit": limit,
                "offset": offset,
            },
        ).mappings().all()
    return [OllamaLogOut.model_validate(dict(r)) for r in rows]


def recent_logs(n: int = 20) -> list[OllamaLogOut]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT * FROM ollama_inference_logs ORDER BY logged_at DESC LIMIT :n"),
            {"n": n},
        ).mappings().all()
    return [OllamaLogOut.model_validate(dict(r)) for r in rows]


def summary(hours: int = 24) -> dict:
    """Aggregate stats — returned as dict because the shape is one-off."""
    with engine.connect() as conn:
        totals = conn.execute(
            text(
                """
                SELECT
                    COUNT(*)                                                AS total_calls,
                    COUNT(*) FILTER (WHERE success = FALSE)                 AS error_count,
                    ROUND(AVG(tokens_per_second)::numeric, 2)               AS avg_tokens_per_second,
                    ROUND(MIN(tokens_per_second)::numeric, 2)               AS min_tokens_per_second,
                    ROUND(MAX(tokens_per_second)::numeric, 2)               AS max_tokens_per_second,
                    ROUND(AVG(total_duration_ms)::numeric, 0)               AS avg_total_duration_ms,
                    ROUND(AVG(load_duration_ms)::numeric, 0)                AS avg_load_duration_ms,
                    ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP
                        (ORDER BY total_duration_ms)::numeric, 0)           AS p95_total_duration_ms,
                    COUNT(*) FILTER (WHERE load_duration_ms > :threshold)   AS cold_load_count,
                    ROUND(
                        100.0 * COUNT(*) FILTER (WHERE load_duration_ms > :threshold)
                        / NULLIF(COUNT(*), 0), 1
                    )                                                        AS cold_load_rate_pct
                FROM ollama_inference_logs
                WHERE logged_at >= NOW() - INTERVAL '1 hour' * :hours
                """
            ),
            {"hours": hours, "threshold": COLD_LOAD_THRESHOLD_MS},
        ).mappings().first()

        by_context = conn.execute(
            text(
                """
                SELECT
                    tool_context,
                    COUNT(*)                                  AS calls,
                    ROUND(AVG(tokens_per_second)::numeric, 2) AS avg_tps,
                    ROUND(AVG(total_duration_ms)::numeric, 0) AS avg_duration_ms
                FROM ollama_inference_logs
                WHERE logged_at >= NOW() - INTERVAL '1 hour' * :hours
                GROUP BY tool_context
                ORDER BY calls DESC
                """
            ),
            {"hours": hours},
        ).mappings().all()

    return {
        "window_hours": hours,
        "cold_load_threshold_ms": COLD_LOAD_THRESHOLD_MS,
        "totals": dict(totals) if totals else {},
        "by_tool_context": [dict(r) for r in by_context],
    }
