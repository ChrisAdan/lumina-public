"""
routers/health.py
Deep health check for the full Lumina stack.

GET /health        — checks all services, returns structured JSON
GET /health/quick  — postgres only, fast liveness probe

Response shape (all routes):
{
    "status": "ok" | "degraded" | "down",
    "services": {
        "postgres":  {"ok": bool, "detail": str | null},
        "chromadb":  {"ok": bool, "detail": str | null},
        "searxng":   {"ok": bool, "detail": str | null},
        "ollama":    {"ok": bool, "detail": str | null},
    },
    "request_id": str
}
"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from db.postgres import get_db
from configs.app import CHROMA_HOST, CHROMA_PORT, SEARXNG_URL, OLLAMA_URL
from services.tool_cache import get_cache as _get_tool_cache

router = APIRouter(prefix="/health", tags=["health"])

_CHROMA_URL = f"http://{CHROMA_HOST}:{CHROMA_PORT}/api/v2/heartbeat"
_OLLAMA_URL = f"{OLLAMA_URL}/api/tags"
_SEARXNG_URL = f"{SEARXNG_URL}/healthz"


async def _ping(url: str, timeout: float = 3.0) -> tuple[bool, str | None]:
    """Returns (ok, error_detail)."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url)
            if r.status_code < 500:
                return True, None
            return False, f"HTTP {r.status_code}"
    except Exception as exc:
        return False, str(exc)


def _check_postgres(db: Session) -> tuple[bool, str | None]:
    try:
        db.execute(text("SELECT 1"))
        return True, None
    except Exception as exc:
        return False, str(exc)


def _build_response(services: dict, request_id: str | None) -> dict:
    all_ok = all(v["ok"] for v in services.values())
    any_ok = any(v["ok"] for v in services.values())
    status = "ok" if all_ok else ("degraded" if any_ok else "down")
    return {"status": status, "services": services, "request_id": request_id}


@router.get("/")
async def health(request: Request, db: Session = Depends(get_db)):
    """Full stack health check — hits Postgres, ChromaDB, SearXNG, and Ollama."""
    request_id = getattr(request.state, "request_id", None)

    pg_ok, pg_err = _check_postgres(db)
    chroma_ok, chroma_err = await _ping(_CHROMA_URL)
    searxng_ok, searxng_err = await _ping(_SEARXNG_URL)
    ollama_ok, ollama_err = await _ping(_OLLAMA_URL)

    services = {
        "postgres": {"ok": pg_ok, "detail": pg_err},
        "chromadb": {"ok": chroma_ok, "detail": chroma_err},
        "searxng":  {"ok": searxng_ok, "detail": searxng_err},
        "ollama":   {"ok": ollama_ok, "detail": ollama_err},
    }
    result = _build_response(services, request_id)
    result["tool_cache"] = _get_tool_cache().stats()
    return result


@router.get("/quick")
def health_quick(request: Request, db: Session = Depends(get_db)):
    """Fast liveness probe — Postgres only. Use for container health checks."""
    request_id = getattr(request.state, "request_id", None)
    pg_ok, pg_err = _check_postgres(db)
    return _build_response(
        {"postgres": {"ok": pg_ok, "detail": pg_err}},
        request_id,
    )
