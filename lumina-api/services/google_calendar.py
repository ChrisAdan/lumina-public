"""
services/google_calendar.py

Fetch events from the primary Google Calendar, upsert into Postgres via
`repos.calendar`, embed into the ChromaDB 'calendar' collection.

Called by:
  - APScheduler CRON (every 30 min)
  - POST /calendar/sync (manual trigger)
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from googleapiclient.discovery import build

from configs.app import CALENDAR_LOOKAHEAD_DAYS
from db.chroma import get_chroma_client
from repos import calendar as calendar_repo
from schemas.calendar import CalendarAttendee, CalendarEventUpsert
from services.calendars_md import CalendarTarget, load_calendar_targets
from services.google_auth import get_credentials

logger = logging.getLogger(__name__)

CHROMA_COLLECTION = "calendar"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_google_datetime(dt_field: dict) -> tuple[datetime | None, bool]:
    """Parse a Google Calendar dateTime or date field. Returns (datetime_utc, is_all_day)."""
    if "dateTime" in dt_field:
        return datetime.fromisoformat(dt_field["dateTime"]).astimezone(timezone.utc), False
    if "date" in dt_field:
        d = datetime.fromisoformat(dt_field["date"]).replace(tzinfo=timezone.utc)
        return d, True
    return None, False


def _build_chroma_document(raw: dict, start_time: datetime | None) -> str:
    parts = [raw.get("summary", "Untitled event")]
    if start_time:
        parts.append(start_time.strftime("%A %d %B %Y %H:%M UTC"))
    if loc := raw.get("location"):
        parts.append(f"Location: {loc}")
    if desc := raw.get("description"):
        parts.append(desc[:500])
    return " — ".join(parts)


def _to_upsert(raw: dict, target: CalendarTarget) -> tuple[CalendarEventUpsert, datetime | None]:
    start_time, all_day = _parse_google_datetime(raw.get("start", {}))
    end_time, _ = _parse_google_datetime(raw.get("end", {}))
    attendees = [
        CalendarAttendee(email=a.get("email"), name=a.get("displayName"))
        for a in raw.get("attendees", [])
    ]
    payload = CalendarEventUpsert(
        google_event_id=raw["id"],
        calendar_id=target.calendar_id,
        calendar_name=target.name,
        summary=raw.get("summary"),
        description=raw.get("description"),
        location=raw.get("location"),
        start_time=start_time,
        end_time=end_time,
        all_day=all_day,
        attendees=attendees if attendees else None,
        status=raw.get("status"),
        html_link=raw.get("htmlLink"),
    )
    return payload, start_time


# ── Core sync ────────────────────────────────────────────────────────────────

def fetch_events_for(
    service: Any,
    calendar_id: str,
    lookahead_days: int = CALENDAR_LOOKAHEAD_DAYS,
) -> list[dict]:
    """Call the Calendar API for one calendar and return raw event dicts."""
    now = datetime.now(timezone.utc)
    time_max = now + timedelta(days=lookahead_days)
    result = (
        service.events()
        .list(
            calendarId=calendar_id,
            timeMin=now.isoformat(),
            timeMax=time_max.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=250,
        )
        .execute()
    )
    return result.get("items", [])


def _chroma_id(payload: CalendarEventUpsert) -> str:
    """Composite Chroma ID — the same Google event can legitimately appear in
    two synced calendars (you invited to someone's event), so the cache key
    needs the source calendar to avoid one calendar's copy clobbering the
    other's metadata."""
    return f"{payload.calendar_id or 'unknown'}::{payload.google_event_id}"


def sync_calendar() -> dict[str, Any]:
    """
    Full sync pipeline: read Calendars.md → for each target calendar
    fetch → upsert via repo → embed Chroma → mark embedded.

    Called by CRON and the manual /calendar/sync endpoint.
    """
    targets = load_calendar_targets()
    if not targets:
        logger.info("Calendar sync — no targets configured (Calendars.md missing or empty)")
        return {"calendars": 0, "fetched": 0, "upserted": 0, "embedded": 0, "errored": 0}

    logger.info("Calendar sync started — %d target(s): %s",
                len(targets), [t.name for t in targets])

    creds = get_credentials()
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    chroma = get_chroma_client().get_or_create_collection(CHROMA_COLLECTION)

    fetched = 0
    upserted = 0
    embedded = 0
    errored = 0

    for target in targets:
        try:
            raw_events = fetch_events_for(service, target.calendar_id)
        except Exception as exc:
            errored += 1
            logger.error("Failed to fetch events for %s (%s): %s",
                         target.name, target.calendar_id, exc)
            continue

        fetched += len(raw_events)
        logger.info("Fetched %d events from %s (%s)",
                    len(raw_events), target.name, target.calendar_id)

        for raw in raw_events:
            try:
                payload, start_time = _to_upsert(raw, target)
                calendar_repo.upsert(payload)
                upserted += 1

                cid = _chroma_id(payload)
                chroma.upsert(
                    ids=[cid],
                    documents=[_build_chroma_document(raw, start_time)],
                    metadatas=[{
                        "google_event_id": payload.google_event_id,
                        "calendar_id": payload.calendar_id or "",
                        "calendar_name": payload.calendar_name or "",
                        "summary": payload.summary or "",
                        "start_time": start_time.isoformat() if start_time else "",
                        "location": payload.location or "",
                    }],
                )
                calendar_repo.mark_embedded(payload.google_event_id, payload.calendar_id)
                embedded += 1
            except Exception as exc:
                errored += 1
                logger.error("Failed to sync event %s from %s: %s",
                             raw.get("id"), target.name, exc)

    summary = {
        "calendars": len(targets),
        "fetched": fetched,
        "upserted": upserted,
        "embedded": embedded,
        "errored": errored,
    }
    logger.info("Calendar sync complete: %s", summary)
    return summary
