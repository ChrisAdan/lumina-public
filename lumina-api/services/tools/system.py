"""System introspection tools — lets Lumina report on its own performance."""
from __future__ import annotations

import httpx

from services.tool_cache import get_cache as _get_cache
from services.tools._base import ToolSpec


async def _system_stats() -> dict:
    cache = _get_cache()
    stats = cache.stats()
    return {
        "tool_cache": {
            "hits": stats["hits"],
            "misses": stats["misses"],
            "hit_rate_pct": round(stats["hit_rate"] * 100, 1),
            "live_entries": stats["live_entries"],
        },
        "note": "Cache resets on container restart. Hit rate is meaningful after ~10+ tool calls.",
    }


async def _inference_stats(hours: int = 24) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "http://localhost:8000/observability/ollama/summary",
                params={"hours": hours},
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        return {"error": str(exc)}


TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="system_stats",
        description=(
            "Report Lumina's internal performance stats: tool cache hit rate, "
            "live cache entries. Use when the user asks how the system is performing, "
            "whether caching is working, or wants a status report."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        handler=_system_stats,
    ),
    ToolSpec(
        name="inference_stats",
        description=(
            "Report LLM inference performance: average tokens per second, p95 latency, "
            "cold load rate. Use when the user asks how fast responses are, "
            "about token speed, generation latency, or model throughput."
        ),
        parameters={
            "type": "object",
            "properties": {
                "hours": {
                    "type": "integer",
                    "description": "Lookback window in hours (default 24)",
                    "default": 24,
                },
            },
            "required": [],
        },
        handler=_inference_stats,
    ),
]
