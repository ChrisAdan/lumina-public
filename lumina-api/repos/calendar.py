"""Repo for `calendar_events`. Pydantic in, Pydantic out."""
import json
from datetime import datetime

from sqlalchemy import text

from db.postgres import engine
from schemas.calendar import CalendarEventOut, CalendarEventUpsert


def list_upcoming(
    now: datetime,
    cutoff: datetime,
    *,
    limit: int = 50,
    calendar_name: str | None = None,
) -> list[CalendarEventOut]:
    sql = """
        SELECT *
        FROM calendar_events
        WHERE start_time >= :now
          AND start_time <= :cutoff
          AND (status IS NULL OR status != 'cancelled')
          AND (:calendar_name IS NULL OR calendar_name ILIKE :calendar_name)
        ORDER BY start_time ASC
        LIMIT :limit
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(sql),
            {
                "now": now,
                "cutoff": cutoff,
                "limit": limit,
                "calendar_name": calendar_name,
            },
        ).mappings().all()
    return [CalendarEventOut.model_validate(dict(r)) for r in rows]


def search(
    now: datetime,
    cutoff: datetime,
    query: str,
    *,
    limit: int = 25,
) -> list[CalendarEventOut]:
    """Substring match across summary / location / description for events in
    the [now, cutoff] window. Cancelled events are excluded."""
    pattern = f"%{query}%"
    sql = """
        SELECT *
        FROM calendar_events
        WHERE start_time >= :now
          AND start_time <= :cutoff
          AND (status IS NULL OR status != 'cancelled')
          AND (
            summary ILIKE :q
            OR location ILIKE :q
            OR description ILIKE :q
          )
        ORDER BY start_time ASC
        LIMIT :limit
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(sql),
            {"now": now, "cutoff": cutoff, "q": pattern, "limit": limit},
        ).mappings().all()
    return [CalendarEventOut.model_validate(dict(r)) for r in rows]


def upsert(payload: CalendarEventUpsert) -> CalendarEventOut:
    """Insert or update a single event by (google_event_id, calendar_id).
    Resets embedded_at on update."""
    attendees_json = (
        json.dumps([a.model_dump() for a in payload.attendees])
        if payload.attendees is not None
        else None
    )
    sql = """
        INSERT INTO calendar_events
            (google_event_id, calendar_id, calendar_name,
             summary, description, location,
             start_time, end_time, all_day, attendees, status,
             html_link, synced_at, embedded_at)
        VALUES
            (:google_event_id, :calendar_id, :calendar_name,
             :summary, :description, :location,
             :start_time, :end_time, :all_day, CAST(:attendees AS JSONB),
             :status, :html_link, NOW(), NULL)
        ON CONFLICT (google_event_id, calendar_id) DO UPDATE SET
            calendar_name = EXCLUDED.calendar_name,
            summary       = EXCLUDED.summary,
            description   = EXCLUDED.description,
            location      = EXCLUDED.location,
            start_time    = EXCLUDED.start_time,
            end_time      = EXCLUDED.end_time,
            all_day       = EXCLUDED.all_day,
            attendees     = EXCLUDED.attendees,
            status        = EXCLUDED.status,
            html_link     = EXCLUDED.html_link,
            synced_at     = NOW(),
            embedded_at   = NULL
        RETURNING *
    """
    with engine.begin() as conn:
        row = conn.execute(
            text(sql),
            {
                "google_event_id": payload.google_event_id,
                "calendar_id": payload.calendar_id,
                "calendar_name": payload.calendar_name,
                "summary": payload.summary,
                "description": payload.description,
                "location": payload.location,
                "start_time": payload.start_time,
                "end_time": payload.end_time,
                "all_day": payload.all_day,
                "attendees": attendees_json,
                "status": payload.status,
                "html_link": payload.html_link,
            },
        ).mappings().first()
    return CalendarEventOut.model_validate(dict(row))


def mark_embedded(google_event_id: str, calendar_id: str | None = None) -> bool:
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "UPDATE calendar_events SET embedded_at = NOW() "
                "WHERE google_event_id = :id "
                "  AND (:cal_id IS NULL OR calendar_id = :cal_id) "
                "RETURNING id"
            ),
            {"id": google_event_id, "cal_id": calendar_id},
        ).first()
    return row is not None
