"""
Pydantic schemas for the documents (library) vertical.

Boundary contract for every read/write to the `documents` table and the
ChromaDB `documents` collection. Same pattern as schemas/recipes.py:

  *Create — what the repo / ingest pipeline accepts.
  *Out    — what we return.
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ── Documents (postgres row, one per ingested source) ────────────────────────

class DocumentBase(BaseModel):
    id: str = Field(..., description="stable slug, e.g. 'supernote_nomad_v1'")
    title: str
    source_path: str
    sha256: str
    page_count: int = Field(..., ge=0)
    chunk_count: int = Field(0, ge=0)
    metadata: dict = Field(default_factory=dict)


class DocumentCreate(DocumentBase):
    """Repo input. Same shape as Base — no server-controlled fields here today."""
    pass


class DocumentOut(DocumentBase):
    ingested_at: datetime
    updated_at: datetime
    # Derived from metadata so the LLM can label EPUB chapters as "chapters"
    # instead of mislabeling them "pages". `page_count` keeps its column name
    # but is really `chapter_count` for EPUBs.
    format: str = "pdf"
    unit_label: str = "pages"

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="after")
    def _derive_format(self):
        fmt = (self.metadata or {}).get("format", "pdf")
        self.format = fmt
        self.unit_label = {
            "epub": "chapters",
            "md":   "sections",
        }.get(fmt, "pages")
        return self


# ── Chunks (ChromaDB rows; not persisted to postgres) ────────────────────────

class DocumentChunk(BaseModel):
    """One retrieval hit from the documents collection."""
    doc_id: str
    doc_title: str
    page: int  # PDFs: page number; EPUBs: chapter index. Use `cite` for display.
    cite: str = ""  # human-readable citation, e.g. "p. 47" or "ch. 3: Onboarding"
    chunk_idx: int
    content: str
    score: Optional[float] = None  # cosine distance from chroma; lower = better


# ── Query body ───────────────────────────────────────────────────────────────

class DocumentQuery(BaseModel):
    q: str = Field(..., min_length=1)
    doc_id: Optional[str] = None
    top_k: int = Field(4, ge=1, le=20)
