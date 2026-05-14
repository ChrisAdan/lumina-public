"""
repos/reminders.py — CRUD for the reminders table.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text

from db.postgres import engine


def create(
    message: str,
    fire_at: Optional[datetime] = None,
    cron_expr: Optional[str] = None,
    repeat: bool = False,
    topic: str = "lumina-alerts",
) -> dict:
    with engine.begin() as conn:
        row = conn.execute(
            text("""
                INSERT INTO reminders (message, fire_at, cron_expr, repeat, topic)
                VALUES (:message, :fire_at, :cron_expr, :repeat, :topic)
                RETURNING *
            """),
            {
                "message": message,
                "fire_at": fire_at,
                "cron_expr": cron_expr,
                "repeat": repeat,
                "topic": topic,
            },
        ).mappings().first()
    return dict(row)


def list_active() -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT * FROM reminders
                WHERE enabled = TRUE
                ORDER BY COALESCE(fire_at, NOW()) ASC
            """)
        ).mappings().all()
    return [dict(r) for r in rows]


def list_all_recurring() -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT * FROM reminders
                WHERE repeat = TRUE AND enabled = TRUE AND cron_expr IS NOT NULL
            """)
        ).mappings().all()
    return [dict(r) for r in rows]


def get_due_oneshots() -> list[dict]:
    now = datetime.now(timezone.utc)
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT * FROM reminders
                WHERE fire_at IS NOT NULL
                  AND fire_at <= :now
                  AND enabled = TRUE
                  AND last_fired_at IS NULL
            """),
            {"now": now},
        ).mappings().all()
    return [dict(r) for r in rows]


def mark_fired(reminder_id: int) -> None:
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        conn.execute(
            text("""
                UPDATE reminders
                SET last_fired_at = :now,
                    enabled = CASE WHEN repeat = FALSE THEN FALSE ELSE enabled END
                WHERE id = :id
            """),
            {"now": now, "id": reminder_id},
        )


def cancel(reminder_id: int) -> dict | None:
    with engine.begin() as conn:
        row = conn.execute(
            text("""
                UPDATE reminders SET enabled = FALSE
                WHERE id = :id
                RETURNING *
            """),
            {"id": reminder_id},
        ).mappings().first()
    return dict(row) if row else None
