"""Pydantic schemas for the Google Calendar sync vertical."""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class CalendarAttendee(BaseModel):
    email: Optional[str] = None
    name: Optional[str] = None


class CalendarEventBase(BaseModel):
    google_event_id: str
    calendar_id: Optional[str] = None
    calendar_name: Optional[str] = None
    summary: Optional[str] = None
    description: Optional[str] = None
    location: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    all_day: bool = False
    attendees: Optional[list[CalendarAttendee]] = None
    status: Optional[str] = None
    html_link: Optional[str] = None


class CalendarEventUpsert(CalendarEventBase):
    """Repo input for sync writes."""


class CalendarEventOut(CalendarEventBase):
    id: int
    synced_at: Optional[datetime] = None
    embedded_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)
