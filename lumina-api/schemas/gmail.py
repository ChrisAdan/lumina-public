"""Pydantic schemas for the Gmail sync vertical."""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class EmailIndexBase(BaseModel):
    message_id: str
    thread_id: Optional[str] = None
    subject: Optional[str] = None
    sender: Optional[str] = None
    received_at: Optional[datetime] = None
    snippet: Optional[str] = None


class EmailIndexUpsert(EmailIndexBase):
    """Repo input. Sets embedded_at = NOW() on insert; ON CONFLICT DO NOTHING."""


class EmailIndexOut(EmailIndexBase):
    id: int
    embedded_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)
