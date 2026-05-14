"""
Google Calendar router.

GET  /calendar/upcoming  — next N events (Lumina tool-call target)
POST /calendar/sync      — manual sync trigger
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Query

from configs.app import CALENDAR_LOOKAHEAD_DAYS, LOCAL_TIMEZONE, USER_NAME
from repos import calendar as calendar_repo
from services.google_calendar import sync_calendar
from utils.calendar_format import format_local, other_attendees

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/calendar", tags=["calendar"])


def _serialize(e) -> dict[str, Any]:
    return {
        "id": e.google_event_id,
        "calendar_id": e.calendar_id,
        "calendar_name": e.calendar_name,
        "summary": e.summary,
        "description": e.description,
        "location": e.location,
        "start_time": e.start_time.isoformat() if e.start_time else None,
        "end_time": e.end_time.isoformat() if e.end_time else None,
        "start_local": format_local(e.start_time),
        "end_local": format_local(e.end_time),
        "all_day": e.all_day,
        "attendees": [a.model_dump() for a in (e.attendees or [])],
        "attendees_other": other_attendees(e.attendees),
        "status": e.status,
        "html_link": e.html_link,
    }


def _summary(e) -> dict[str, Any]:
    return {
        "calendar": e.calendar_name,
        "summary": e.summary,
        "start": e.start_time.isoformat() if e.start_time else None,
        "end": e.end_time.isoformat() if e.end_time else None,
        "start_local": format_local(e.start_time),
        "end_local": format_local(e.end_time),
        "location": e.location,
        "attendees_other": other_attendees(e.attendees),
    }


@router.get("/upcoming")
def get_upcoming_events(
    days: int = Query(default=CALENDAR_LOOKAHEAD_DAYS, ge=1, le=60),
    calendar: str | None = Query(default=None, description="filter by calendar display name (case-insensitive)"),
    summary: bool = Query(default=False, description="terse projection for LLM tool consumption"),
) -> dict[str, Any]:
    """
    Return upcoming calendar events from the local DB cache (no Google API call).
    This is the endpoint Lumina calls at conversation start / briefing time.
    """
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=days)
    events = calendar_repo.list_upcoming(now, cutoff, limit=50, calendar_name=calendar)

    if summary:
        return {
            "window_days": days,
            "timezone": LOCAL_TIMEZONE,
            "user_name": USER_NAME,
            "count": len(events),
            "events": [_summary(e) for e in events],
        }

    return {
        "window_days": days,
        "timezone": LOCAL_TIMEZONE,
        "user_name": USER_NAME,
        "count": len(events),
        "events": [_serialize(e) for e in events],
    }


@router.get("/search")
def search_events(
    q: str = Query(..., min_length=1, description="substring match across summary/location/description"),
    days: int = Query(default=CALENDAR_LOOKAHEAD_DAYS, ge=1, le=60),
    summary: bool = Query(default=False),
) -> dict[str, Any]:
    """Substring search over the cached event window."""
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=days)
    events = calendar_repo.search(now, cutoff, q, limit=25)
    if summary:
        return {
            "query": q,
            "window_days": days,
            "timezone": LOCAL_TIMEZONE,
            "user_name": USER_NAME,
            "count": len(events),
            "events": [_summary(e) for e in events],
        }
    return {
        "query": q,
        "window_days": days,
        "timezone": LOCAL_TIMEZONE,
        "user_name": USER_NAME,
        "count": len(events),
        "events": [_serialize(e) for e in events],
    }


@router.post("/sync")
def trigger_calendar_sync() -> dict[str, Any]:
    """
    Manually trigger a Google Calendar sync.
    Same pipeline as the CRON job: fetch → upsert Postgres → embed Chroma.
    """
    try:
        result = sync_calendar()
        return {"status": "ok", **result}
    except Exception as exc:
        logger.exception("Manual calendar sync failed")
        return {"status": "error", "detail": str(exc)}
