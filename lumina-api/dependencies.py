"""
dependencies.py
Shared FastAPI dependencies used across all routers.

Import from here rather than duplicating in each router:
    from dependencies import confirm_gate, Pagination, paginate
"""
from __future__ import annotations

from dataclasses import dataclass
from fastapi import HTTPException, Query


# ── Confirm gate ──────────────────────────────────────────────────────────────

class UnconfirmedPreview(Exception):
    """Raised internally when confirm=False; caught by confirm_gate."""
    def __init__(self, preview: object):
        self.preview = preview


def confirm_gate(confirm: bool = Query(default=False)) -> bool:
    """
    Dependency that standardises the confirm=true write pattern.

    Usage in a router:
        @router.post("/things")
        def create_thing(payload: ThingCreate, confirmed: bool = Depends(confirm_gate)):
            if not confirmed:
                return {"preview": payload.model_dump(), "confirmed": False,
                        "message": "Pass confirm=true to commit."}
            # ... do the write
    """
    return confirm


def preview_response(data: object) -> dict:
    """
    Wrap any preview payload in a standard envelope.
    Ollama sees a consistent shape regardless of which endpoint it hits.
    """
    return {
        "confirmed": False,
        "message": "Preview only — pass confirm=true to commit this write.",
        "preview": data,
    }


# ── Pagination ────────────────────────────────────────────────────────────────

@dataclass
class Pagination:
    limit: int
    offset: int


def paginate(
    limit: int = Query(default=20, ge=1, le=200, description="Max rows to return"),
    offset: int = Query(default=0, ge=0, description="Row offset for pagination"),
) -> Pagination:
    """
    Standard limit/offset pagination dependency.

    Usage:
        @router.get("/things")
        def list_things(page: Pagination = Depends(paginate), db = Depends(get_db)):
            rows = db.execute(text("SELECT * FROM things LIMIT :l OFFSET :o"),
                              {"l": page.limit, "o": page.offset}).fetchall()
    """
    return Pagination(limit=limit, offset=offset)
