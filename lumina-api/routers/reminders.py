"""
routers/reminders.py — REST endpoints for the reminders vertical.
Tools bypass these routes and call repos/scheduler directly.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

router = APIRouter(prefix="/reminders", tags=["reminders"])


class ReminderOut(BaseModel):
    id: int
    message: str
    fire_at: Optional[datetime]
    cron_expr: Optional[str]
    repeat: bool
    topic: str
    enabled: bool
    last_fired_at: Optional[datetime]
    created_at: datetime

    model_config = {"from_attributes": True}


@router.get("/", response_model=list[ReminderOut])
def list_reminders():
    from repos import reminders as repo
    return [ReminderOut.model_validate(r) for r in repo.list_active()]


@router.delete("/{reminder_id}", response_model=ReminderOut)
def cancel_reminder(reminder_id: int):
    from repos import reminders as repo
    from scheduler import cancel_reminder_job
    row = repo.cancel(reminder_id)
    if not row:
        raise HTTPException(status_code=404, detail="Reminder not found")
    cancel_reminder_job(reminder_id)
    return ReminderOut.model_validate(row)
