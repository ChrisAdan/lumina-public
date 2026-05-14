"""
Library router — endpoints over the ingested document corpus.

Endpoints:
  GET  /library/list           catalog of ingested documents
  POST /library/query          semantic search over chunks; returns excerpts + page numbers

Both endpoints support `?summary=true` to return a terse one-line string,
intended for LLM tool consumption (cheap to reason about).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse

from repos import documents as documents_repo
from schemas.documents import DocumentChunk, DocumentQuery
from services.pdf_ingest import COLLECTION_NAME
from services.embeddings import _client, _ollama_ef
from services.vault_sync import sync_vault

router = APIRouter(prefix="/library", tags=["library"])

logger = logging.getLogger("lumina.library")

# Cosine distance threshold. ChromaDB returns distance (0=identical, 1=orthogonal);
# chunks above this value are too dissimilar to be useful and are dropped so the
# model doesn't get misled by off-topic excerpts. Tunable via env var.
_SCORE_THRESHOLD = float(os.getenv("LUMINA_DOC_SCORE_THRESHOLD", "0.70"))


# ── Catalog ──────────────────────────────────────────────────────────────────

@router.get("/list")
def list_documents(summary: bool = Query(False)):
    docs = documents_repo.list_all()

    if summary:
        if not docs:
            return PlainTextResponse("0 docs in library.")
        head = ", ".join(f"{d.title} ({d.page_count}p, {d.chunk_count} chunks)" for d in docs[:3])
        more = f" +{len(docs) - 3} more" if len(docs) > 3 else ""
        return PlainTextResponse(f"{len(docs)} doc{'s' if len(docs) != 1 else ''}: {head}{more}.")

    return [d.model_dump(mode="json") for d in docs]


# ── Query ────────────────────────────────────────────────────────────────────

@router.post("/query")
def query_documents(payload: DocumentQuery, summary: bool = Query(False)):
    coll = _client().get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=_ollama_ef(),
    )

    where: Optional[dict] = None
    if payload.doc_id:
        if documents_repo.get(payload.doc_id) is not None:
            where = {"doc_id": payload.doc_id}
        else:
            logger.warning(
                "library/query: unknown doc_id %r, falling back to unfiltered search",
                payload.doc_id,
            )
    raw = coll.query(
        query_texts=[payload.q],
        n_results=payload.top_k,
        where=where,
    )

    ids = (raw.get("ids") or [[]])[0]
    docs = (raw.get("documents") or [[]])[0]
    metas = (raw.get("metadatas") or [[]])[0]
    distances = (raw.get("distances") or [[]])[0]

    hits: list[DocumentChunk] = []
    for i, _id in enumerate(ids):
        meta = metas[i] or {}
        page = int(meta.get("page", 0))
        # Back-compat: chunks ingested before `cite` was added default to "p. N".
        cite = meta.get("cite") or (f"p. {page}" if page else "")
        hits.append(
            DocumentChunk(
                doc_id=meta.get("doc_id", ""),
                doc_title=meta.get("doc_title", ""),
                page=page,
                cite=cite,
                chunk_idx=int(meta.get("chunk_idx", 0)),
                content=docs[i] or "",
                score=round(float(distances[i]), 4) if i < len(distances) else None,
            )
        )

    before = len(hits)
    hits = [h for h in hits if h.score is None or h.score <= _SCORE_THRESHOLD]
    if len(hits) < before:
        logger.debug("library/query: dropped %d low-relevance chunks (threshold=%.2f)", before - len(hits), _SCORE_THRESHOLD)

    if summary:
        if not hits:
            return PlainTextResponse(f"0 hits for: {payload.q!r}")
        # Group hits by doc title so the summary stays readable across multi-doc queries.
        by_title: dict[str, list[str]] = {}
        for h in hits:
            by_title.setdefault(h.doc_title or "untitled", []).append(h.cite or f"p. {h.page}")
        parts = [
            f"{title} ({', '.join(cites)})"
            for title, cites in by_title.items()
        ]
        return PlainTextResponse(f"{len(hits)} hit{'s' if len(hits) != 1 else ''} in " + "; ".join(parts) + ".")

    return [h.model_dump() for h in hits]


# ── Vault sync ──────────────────────────────────────────────────────────────

@router.post("/sync")
def sync_library_vault():
    """Walk the Obsidian vault and re-embed any changed/new markdown notes,
    then delete embeddings for orphaned files. Called by the 15-min APScheduler
    job; safe to call manually for instant freshness.
    """
    return sync_vault()
