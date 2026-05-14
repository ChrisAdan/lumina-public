"""Gmail tools — search, browse, and send email via the authenticated account."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from configs.app import USER_NAME
from db.chroma import get_chroma_client
from repos import gmail as gmail_repo
from services.google_gmail import send_email
from services.tools._base import ToolSpec

_DIVIDER = "─" * 52


def _apply_email_template(body: str) -> str:
    """Wrap outgoing email body with the standard Lumina header and signature."""
    sender = USER_NAME or "the household"
    return (
        f"Note: This is an automated message sent by Lumina on behalf of {sender}.\n"
        f"{_DIVIDER}\n\n"
        f"{body.strip()}\n\n"
        f"{_DIVIDER}\n"
        f"— Lumina\n"
        f"  Personal household AI\n"
        f"  Sent on behalf of {sender}"
    )

logger = logging.getLogger(__name__)

CHROMA_COLLECTION = "emails"


async def _gmail_search(q: str, n: int = 5) -> dict:
    try:
        client = get_chroma_client()
        collection = client.get_or_create_collection(CHROMA_COLLECTION)
        results = collection.query(
            query_texts=[q],
            n_results=min(n, 10),
            include=["documents", "metadatas", "distances"],
        )
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        dists = results.get("distances", [[]])[0]
        hits = [
            {
                "subject": m.get("subject"),
                "sender": m.get("sender"),
                "received_at": m.get("received_at"),
                "relevance": round(1 - d, 3),
                "excerpt": doc[:400],
            }
            for doc, m, d in zip(docs, metas, dists)
        ]
        return {"query": q, "count": len(hits), "results": hits}
    except Exception as exc:
        logger.error("gmail_search failed: %s", exc)
        return {"error": str(exc)}


async def _gmail_index(days: int = 7) -> dict:
    try:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        rows = gmail_repo.list_recent(since, limit=20)
        return {
            "window_days": days,
            "count": len(rows),
            "emails": [
                {
                    "subject": r.subject,
                    "sender": r.sender,
                    "received_at": r.received_at.isoformat() if r.received_at else None,
                    "snippet": r.snippet,
                }
                for r in rows
            ],
        }
    except Exception as exc:
        logger.error("gmail_index failed: %s", exc)
        return {"error": str(exc)}


_PLACEHOLDER_DOMAINS = {"example.com", "example.org", "example.net", "test.com",
                        "placeholder.com", "domain.com", "yourname.com", "email.com"}

async def _gmail_send(to: str, subject: str, body: str, cc: str | None = None, **_kwargs) -> dict:
    # Block placeholder/hallucinated domains
    domain = to.split("@")[-1].lower() if "@" in to else ""
    if domain in _PLACEHOLDER_DOMAINS:
        return {"error": f"'{to}' looks like a placeholder address. Call people_lookup to get the real email address, then retry gmail_send."}
    # Guard against body_chars or other non-body kwargs
    if not body or (isinstance(body, str) and body.strip().isdigit()):
        return {"error": "body must be the actual email text, not a character count. Call gmail_send again with the full message text in the body field."}
    try:
        return send_email(to=to, subject=subject, body=_apply_email_template(body), cc=cc)
    except Exception as exc:
        logger.error("gmail_send failed: %s", exc)
        return {"error": str(exc)}


TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="gmail_search",
        description=(
            "Semantic search over the user's synced Gmail inbox. "
            "Use for questions like 'what did the contractor say about the bathroom?' "
            "or 'find emails about my Amazon order'. Searches embedded content, not live API."
        ),
        parameters={
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "Natural language search query"},
                "n": {"type": "integer", "description": "Number of results (1–10, default 5)", "default": 5},
            },
            "required": ["q"],
        },
        handler=_gmail_search,
    ),
    ToolSpec(
        name="gmail_index",
        description=(
            "List the most recent emails from the user's synced Gmail inbox. "
            "Use to answer 'what emails have I received recently?' or browse recent messages."
        ),
        parameters={
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Lookback window in days (1–30, default 7)", "default": 7},
            },
            "required": [],
        },
        handler=_gmail_index,
    ),
    ToolSpec(
        name="gmail_send",
        description=(
            "Send a plain-text email from the user's Gmail account. "
            "CRITICAL RULES: "
            "(1) NEVER guess, invent, or construct an email address. "
            "If the user did not provide the address explicitly in this message, "
            "call people_lookup first and use only the email field from the result. "
            "(2) `body` must be the full text of the message — not a length, not a summary, the actual words. "
            "(3) Requires explicit user confirmation — show To, Subject, and full body text, "
            "then wait for 'yes'/'ok'/'send it' before calling this tool."
        ),
        parameters={
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address — must come from people_lookup or be stated explicitly by the user"},
                "subject": {"type": "string", "description": "Email subject line"},
                "body": {"type": "string", "description": "The full plain-text body of the email — the actual words, not a character count or summary"},
                "cc": {"type": "string", "description": "CC address (optional)"},
            },
            "required": ["to", "subject", "body"],
        },
        handler=_gmail_send,
    ),
]
