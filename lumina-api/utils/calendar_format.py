"""Shared formatting helpers for calendar events (router + LLM tool consumers)."""
from __future__ import annotations

from configs.app import LOCAL_TIMEZONE, USER_EMAIL


def format_local(dt) -> str | None:
    """UTC-aware datetime → string in LOCAL_TIMEZONE (e.g. 'Fri May 1, 2026 7:00 PM EDT')."""
    if dt is None:
        return None
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo(LOCAL_TIMEZONE)).strftime("%a %b %-d, %Y %-I:%M %p %Z")
    except Exception:
        return dt.isoformat()


def other_attendees(attendees) -> list[dict]:
    """Strip the user's own attendee entry so callers see only who the user is meeting with.

    No-op when USER_EMAIL is unset — without an anchor we can't tell which entry is the user.
    """
    if not attendees:
        return []
    out = []
    for a in attendees:
        email = (getattr(a, "email", None) or "").lower()
        if USER_EMAIL and email == USER_EMAIL.lower():
            continue
        out.append({"name": getattr(a, "name", None), "email": getattr(a, "email", None)})
    return out
