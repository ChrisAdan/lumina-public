"""Repo for `email_index`. Pydantic in, Pydantic out."""
from datetime import datetime
from typing import Optional

from sqlalchemy import text

from db.postgres import engine
from schemas.gmail import EmailIndexOut, EmailIndexUpsert


def list_recent(since: datetime, *, limit: int = 100) -> list[EmailIndexOut]:
    sql = """
        SELECT * FROM email_index
        WHERE received_at >= :since
        ORDER BY received_at DESC
        LIMIT :limit
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql), {"since": since, "limit": limit}).mappings().all()
    return [EmailIndexOut.model_validate(dict(r)) for r in rows]


def already_indexed(message_ids: list[str]) -> set[str]:
    """Return the subset of message_ids already present in email_index."""
    if not message_ids:
        return set()
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT message_id FROM email_index WHERE message_id = ANY(:ids)"),
            {"ids": message_ids},
        ).all()
    return {r[0] for r in rows}


def insert(payload: EmailIndexUpsert) -> Optional[EmailIndexOut]:
    """Insert a new email row. ON CONFLICT DO NOTHING — returns None on conflict."""
    sql = """
        INSERT INTO email_index
            (message_id, thread_id, subject, sender, received_at, snippet, embedded_at)
        VALUES
            (:message_id, :thread_id, :subject, :sender, :received_at, :snippet, NOW())
        ON CONFLICT (message_id) DO NOTHING
        RETURNING *
    """
    with engine.begin() as conn:
        row = conn.execute(
            text(sql),
            {
                "message_id": payload.message_id,
                "thread_id": payload.thread_id,
                "subject": payload.subject,
                "sender": payload.sender,
                "received_at": payload.received_at,
                "snippet": payload.snippet,
            },
        ).mappings().first()
    return EmailIndexOut.model_validate(dict(row)) if row else None
