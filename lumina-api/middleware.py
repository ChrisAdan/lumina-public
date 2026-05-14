"""
middleware.py
FastAPI middleware for Lumina.
Updated: 20260423130000
Fix: AuditMiddleware session handling — SessionLocal() is not a context manager;
     replaced `with factory() as db` with explicit open/close in try/finally.

Two middlewares — register both in main.py:

    from middleware import RequestIDMiddleware, AuditMiddleware
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(AuditMiddleware)

Order matters: RequestIDMiddleware first so AuditMiddleware can read the header.
"""
from __future__ import annotations

import time
import uuid
import logging
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from sqlalchemy import text

logger = logging.getLogger("lumina")

# Verticals inferred from the first path segment after /
_VERTICAL_MAP = {
    "recipes":       "food",
    "groceries":     "food",
    "menus":         "food",
    "fitness":       "fitness",
    "expenses":      "finance",
    "weather":       "weather",
    "search":        "search",
    "plants":        "home",
    "trips":         "travel",
    "subscriptions": "finance",
    "entertainment": "entertainment",
    "health":        "system",
    "briefing":      "system",
    "v1":            "system",
    "observability": "system",
    "library":       "knowledge",
}


def _infer_vertical(path: str) -> str:
    parts = path.strip("/").split("/")
    return _VERTICAL_MAP.get(parts[0], "other") if parts else "other"


def _infer_query_type(method: str, path: str) -> str:
    parts = path.strip("/").split("/")
    base = parts[0] if parts else "unknown"
    return f"{method.lower()}_{base}"


# ── Request ID middleware ─────────────────────────────────────────────────────

class RequestIDMiddleware(BaseHTTPMiddleware):
    """
    Stamps every request with a UUID in X-Request-ID.
    If the caller supplies their own X-Request-ID header it is forwarded as-is.
    The ID is also attached to request.state so downstream code can reference it.

    Useful for correlating Ollama tool call sequences in logs.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


# ── Audit middleware ──────────────────────────────────────────────────────────

class AuditMiddleware(BaseHTTPMiddleware):
    """
    Writes one row to query_audit for every API request.
    Skips health checks and static/docs paths to avoid noise.

    Requires the app to expose a db session factory on app.state.db_session_factory.
    Set this in main.py lifespan:
        app.state.db_session_factory = SessionLocal
    """

    SKIP_PREFIXES = {"/health", "/docs", "/redoc", "/openapi.json", "/favicon"}

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path

        # Skip noise paths
        if any(path.startswith(p) for p in self.SKIP_PREFIXES):
            return await call_next(request)

        start = time.perf_counter()
        success = True
        status_code = 200

        try:
            response = await call_next(request)
            status_code = response.status_code
            success = status_code < 500
            return response
        except Exception:
            success = False
            raise
        finally:
            duration_ms = int((time.perf_counter() - start) * 1000)
            vertical = _infer_vertical(path)
            query_type = _infer_query_type(request.method, path)
            request_id = getattr(request.state, "request_id", None)

            # Fire-and-forget DB write — don't let audit failures affect responses
            try:
                factory = getattr(request.app.state, "db_session_factory", None)
                if factory:
                    db = factory()
                    try:
                        db.execute(
                            text("""
                                INSERT INTO query_audit
                                    (query_type, vertical, duration_ms, success)
                                VALUES
                                    (:query_type, :vertical, :duration_ms, :success)
                            """),
                            {
                                "query_type": query_type,
                                "vertical":   vertical,
                                "duration_ms": duration_ms,
                                "success":    success,
                            },
                        )
                        db.commit()
                    finally:
                        db.close()
            except Exception as exc:
                logger.warning("audit write failed: %s", exc)

            logger.info(
                "%s %s %s %dms req=%s",
                request.method, path, status_code, duration_ms, request_id,
            )