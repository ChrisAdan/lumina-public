"""
Repo for the `documents` table. Pydantic in, Pydantic out.

Per-chunk text + embeddings are NOT in postgres — they live in the ChromaDB
`documents` collection, written by services.pdf_ingest. This repo is the
catalog (one row per ingested source).
"""
import json
from typing import Optional

from sqlalchemy import text

from db.postgres import engine
from schemas.documents import DocumentCreate, DocumentOut


def upsert(payload: DocumentCreate) -> DocumentOut:
    """Insert or replace a document row by id. Idempotent re-ingest path."""
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                INSERT INTO documents (
                    id, title, source_path, sha256, page_count, chunk_count, metadata
                )
                VALUES (
                    :id, :title, :source_path, :sha256, :page_count, :chunk_count,
                    CAST(:metadata AS JSONB)
                )
                ON CONFLICT (id) DO UPDATE SET
                    title       = EXCLUDED.title,
                    source_path = EXCLUDED.source_path,
                    sha256      = EXCLUDED.sha256,
                    page_count  = EXCLUDED.page_count,
                    chunk_count = EXCLUDED.chunk_count,
                    metadata    = EXCLUDED.metadata,
                    updated_at  = NOW()
                RETURNING *
                """
            ),
            {
                "id": payload.id,
                "title": payload.title,
                "source_path": payload.source_path,
                "sha256": payload.sha256,
                "page_count": payload.page_count,
                "chunk_count": payload.chunk_count,
                "metadata": json.dumps(payload.metadata or {}),
            },
        ).mappings().first()
    return DocumentOut.model_validate(dict(row))


def get(doc_id: str) -> Optional[DocumentOut]:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT * FROM documents WHERE id = :id"),
            {"id": doc_id},
        ).mappings().first()
    return DocumentOut.model_validate(dict(row)) if row else None


def get_by_sha256(sha: str) -> Optional[DocumentOut]:
    """Used by the directory-scan ingest path to skip files whose content is
    already in the library, regardless of filename or doc_id."""
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT * FROM documents WHERE sha256 = :sha LIMIT 1"),
            {"sha": sha},
        ).mappings().first()
    return DocumentOut.model_validate(dict(row)) if row else None


def list_all() -> list[DocumentOut]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT * FROM documents ORDER BY ingested_at DESC")
        ).mappings().all()
    return [DocumentOut.model_validate(dict(r)) for r in rows]


def update_chunk_count(doc_id: str, chunk_count: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE documents SET chunk_count = :n, updated_at = NOW() "
                "WHERE id = :id"
            ),
            {"id": doc_id, "n": chunk_count},
        )


def delete(doc_id: str) -> bool:
    """Returns True if a row was deleted. Caller is responsible for removing
    the corresponding chunks from the chroma `documents` collection."""
    with engine.begin() as conn:
        result = conn.execute(
            text("DELETE FROM documents WHERE id = :id"),
            {"id": doc_id},
        )
    return (result.rowcount or 0) > 0
