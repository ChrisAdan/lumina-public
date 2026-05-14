"""
Gmail router.

POST /gmail/sync   — manual sync trigger (CRON calls same service fn)
GET  /gmail/search — semantic search over embedded emails via ChromaDB
GET  /gmail/index  — recent entries in email_index (for inspection / debug)
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Body, Query
from pydantic import BaseModel, EmailStr

from db.chroma import get_chroma_client
from repos import gmail as gmail_repo
from services.google_gmail import send_email, sync_gmail

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/gmail", tags=["gmail"])

CHROMA_COLLECTION = "emails"


class SendRequest(BaseModel):
    to: str
    subject: str
    body: str
    cc: str | None = None


@router.post("/send")
def send_gmail(req: SendRequest) -> dict[str, Any]:
    """Send a plain-text email via the authenticated Gmail account."""
    try:
        result = send_email(to=req.to, subject=req.subject, body=req.body, cc=req.cc)
        return {"status": "sent", **result}
    except Exception as exc:
        logger.exception("Gmail send failed")
        return {"status": "error", "detail": str(exc)}


@router.post("/sync")
def trigger_gmail_sync() -> dict[str, Any]:
    """
    Manually trigger a Gmail sync.
    Same pipeline as the daily CRON: fetch → dedup → embed → index.
    Can take 30–120s depending on inbox volume.
    """
    try:
        result = sync_gmail()
        return {"status": "ok", **result}
    except Exception as exc:
        logger.exception("Manual Gmail sync failed")
        return {"status": "error", "detail": str(exc)}


@router.get("/search")
def search_emails(
    q: str = Query(..., min_length=2, description="Natural language search query"),
    n: int = Query(default=5, ge=1, le=20, description="Number of results to return"),
) -> dict[str, Any]:
    """
    Semantic search over embedded emails in ChromaDB.
    This is what Lumina calls when asked questions like
    'what did the contractor say about the bathroom?'
    """
    try:
        client = get_chroma_client()
        collection = client.get_or_create_collection(CHROMA_COLLECTION)

        results = collection.query(
            query_texts=[q],
            n_results=n,
            include=["documents", "metadatas", "distances"],
        )

        documents = results.get("documents", [[]])[0]
        metadatas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]

        hits = [
            {
                "message_id": meta.get("message_id"),
                "subject": meta.get("subject"),
                "sender": meta.get("sender"),
                "received_at": meta.get("received_at"),
                "relevance_score": round(1 - dist, 4),
                "excerpt": doc[:300],
            }
            for doc, meta, dist in zip(documents, metadatas, distances)
        ]
        return {"query": q, "count": len(hits), "results": hits}
    except Exception as exc:
        logger.exception("Gmail search failed")
        return {"status": "error", "detail": str(exc)}


@router.get("/index")
def get_email_index(days: int = Query(default=7, ge=1, le=90)) -> dict[str, Any]:
    """
    Return recent entries from email_index for inspection.
    Useful for verifying sync is working and checking what's been embedded.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = gmail_repo.list_recent(since, limit=100)
    return {
        "window_days": days,
        "count": len(rows),
        "emails": [
            {
                "message_id": r.message_id,
                "thread_id": r.thread_id,
                "subject": r.subject,
                "sender": r.sender,
                "received_at": r.received_at.isoformat() if r.received_at else None,
                "snippet": r.snippet,
                "embedded_at": r.embedded_at.isoformat() if r.embedded_at else None,
            }
            for r in rows
        ],
    }
