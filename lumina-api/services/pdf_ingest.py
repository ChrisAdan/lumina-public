"""
PDF ingestion pipeline for the document library.

Pipeline:
    extract_pages → chunk_page (one page at a time, never crosses page bounds)
                  → embed via chroma's default model → upsert
                  → write/upsert documents row, update chunk_count

Idempotency: same `doc_id` deletes prior chunks from chroma and rewrites the
postgres row. Re-ingesting the same PDF is safe.

Citations are page-bounded by construction: chunks never cross page boundaries,
so the model can quote `(p. N)` without ambiguity. Overlap is intra-page only.
"""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Iterator

from pypdf import PdfReader

from repos import documents as documents_repo
from schemas.documents import DocumentCreate
from services.embeddings import _client, _ollama_ef

logger = logging.getLogger("lumina.pdf_ingest")

COLLECTION_NAME = "documents"

# Tuned for ~250-token chunks at ~4 char/token with ~38-token intra-page overlap.
# Smaller chunks embed a single focused idea, improving retrieval precision.
DEFAULT_MAX_CHARS = 1000
DEFAULT_OVERLAP = 150

# Batch size for chroma upserts. Local embedding model is fast but per-call
# overhead is non-trivial; batches of 64 keep memory bounded and progress legible.
EMBED_BATCH = 64

_PARA_SPLIT_RE = re.compile(r"\n\s*\n+")
_WS_RE = re.compile(r"[ \t]+")


# ── Extraction ───────────────────────────────────────────────────────────────

def extract_pages(path: str | Path) -> list[str]:
    """One string per page. Empty pages are kept as '' so page numbers stay
    1-indexed and aligned with the PDF's own page numbering."""
    reader = PdfReader(str(path))
    pages: list[str] = []
    for page in reader.pages:
        text = page.extract_text() or ""
        text = _WS_RE.sub(" ", text).strip()
        pages.append(text)
    return pages


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


# ── Chunking (paragraph-aware, never crosses page bounds) ────────────────────

def chunk_section(
    text: str,
    *,
    section_idx: int,
    cite: str,
    doc_id: str,
    doc_title: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap: int = DEFAULT_OVERLAP,
) -> list[dict]:
    """Split one section's text (a PDF page or an EPUB chapter) into chunks.
    Returns dicts ready for chroma upsert: {id, document, metadata}.

    `section_idx` is stored in the `page` metadata field (page number for PDFs,
    chapter index for EPUBs). `cite` is the human-readable citation the LLM
    quotes verbatim, e.g. "p. 47" or "ch. 3: Onboarding".

    Overlap is intra-section (carries the tail of one chunk into the start of
    the next within the same section) — never spans section boundaries, so the
    LLM's citation is unambiguous.
    """
    if not text:
        return []

    # Paragraph-aware: prefer splitting on blank lines; fall back to length.
    paragraphs = [p.strip() for p in _PARA_SPLIT_RE.split(text) if p.strip()]
    if not paragraphs:
        paragraphs = [text]

    chunks: list[str] = []
    buf = ""
    for para in paragraphs:
        if len(buf) + len(para) + 1 <= max_chars:
            buf = (buf + "\n\n" + para) if buf else para
        else:
            if buf:
                chunks.append(buf)
            # Paragraph itself bigger than max_chars — hard-split it.
            if len(para) > max_chars:
                step = max_chars - overlap
                for i in range(0, len(para), step):
                    chunks.append(para[i : i + max_chars])
                buf = ""
            else:
                # Carry overlap from the previous chunk's tail into the new buf.
                tail = buf[-overlap:] if buf and overlap > 0 else ""
                buf = (tail + "\n\n" + para).strip() if tail else para
    if buf:
        chunks.append(buf)

    return [
        {
            "id": f"{doc_id}::{section_idx}::{i}",
            "document": chunk,
            "metadata": {
                "doc_id": doc_id,
                "doc_title": doc_title,
                "page": section_idx,
                "cite": cite,
                "chunk_idx": i,
                "char_count": len(chunk),
            },
        }
        for i, chunk in enumerate(chunks)
    ]


# ── Chroma I/O ───────────────────────────────────────────────────────────────

def _collection():
    return _client().get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=_ollama_ef(),
        metadata={"hnsw:space": "cosine"},
    )


def _wipe_doc(doc_id: str) -> None:
    """Remove any existing chunks for this doc before re-ingest."""
    coll = _collection()
    try:
        coll.delete(where={"doc_id": doc_id})
    except Exception as exc:
        logger.warning("chroma wipe failed for %s: %s", doc_id, exc)


def _batched(seq: list[dict], n: int) -> Iterator[list[dict]]:
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


# ── Public entry point ──────────────────────────────────────────────────────

def ingest_pdf(path: str | Path, doc_id: str, title: str) -> dict:
    """Full pipeline. Returns a summary dict for logging / CLI output."""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"PDF not found: {p}")

    sha = file_sha256(p)
    pages = extract_pages(p)
    page_count = len(pages)

    chunks: list[dict] = []
    for page_num, page_text in enumerate(pages, start=1):
        chunks.extend(
            chunk_section(
                page_text,
                section_idx=page_num,
                cite=f"p. {page_num}",
                doc_id=doc_id,
                doc_title=title,
            )
        )

    # Idempotent: wipe prior chunks for this doc_id before writing.
    _wipe_doc(doc_id)

    coll = _collection()
    for batch in _batched(chunks, EMBED_BATCH):
        coll.upsert(
            ids=[c["id"] for c in batch],
            documents=[c["document"] for c in batch],
            metadatas=[c["metadata"] for c in batch],
        )

    documents_repo.upsert(
        DocumentCreate(
            id=doc_id,
            title=title,
            source_path=str(p),
            sha256=sha,
            page_count=page_count,
            chunk_count=len(chunks),
            metadata={"format": "pdf"},
        )
    )

    summary = {
        "doc_id": doc_id,
        "title": title,
        "source_path": str(p),
        "page_count": page_count,
        "chunk_count": len(chunks),
        "sha256": sha,
    }
    logger.info(
        "ingested %s: %d pages, %d chunks", doc_id, page_count, len(chunks)
    )
    return summary
