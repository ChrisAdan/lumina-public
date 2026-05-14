from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, model_validator


class PersonChunk(BaseModel):
    person: str
    display_name: str
    source_path: str
    chunk_idx: int
    content: str
    score: Optional[float] = None


class PeopleQuery(BaseModel):
    q: Optional[str] = Field(default=None, min_length=1)
    person: Optional[str] = None
    top_k: int = Field(default=4, ge=1, le=10)

    @model_validator(mode="after")
    def _require_q_or_person(self):
        if not (self.q or self.person):
            raise ValueError("q or person is required")
        return self
