"""
EPUB / EPUB3 ingestion pipeline for the document library.

EPUBs are reflowable XHTML in a zip — there are no real page numbers. The
natural citation unit is the chapter, so chunks here carry
`cite="ch. {N}: {title}"` while PDFs carry `cite="p. {N}"`. Both feed into
the same chroma `documents` collection and the same `documents` postgres row.

Pipeline mirrors services.pdf_ingest:
    extract_chapters → chunk_section (per chapter, never crosses bounds)
                     → embed via chroma's default model → upsert
                     → upsert documents row, update chunk_count
"""
from __future__ import annotations

import logging
import zipfile
from pathlib import Path

import ebooklib
from bs4 import BeautifulSoup
from ebooklib import epub

from repos import documents as documents_repo
from schemas.documents import DocumentCreate
from services.pdf_ingest import (
    EMBED_BATCH,
    _batched,
    _collection,
    _wipe_doc,
    chunk_section,
    file_sha256,
)

logger = logging.getLogger("lumina.epub_ingest")


# ── Chapter extraction ───────────────────────────────────────────────────────

def _toc_title_map(book: epub.EpubBook) -> dict[str, str]:
    """Flatten book.toc (which may nest Sections) into {href_basename: title}."""
    out: dict[str, str] = {}

    def walk(items):
        for item in items:
            if isinstance(item, tuple):
                section, children = item
                if getattr(section, "href", None):
                    out[section.href.split("#", 1)[0]] = section.title
                walk(children)
            else:
                if getattr(item, "href", None):
                    out[item.href.split("#", 1)[0]] = item.title

    walk(book.toc or [])
    return out


def _chapter_title(soup: BeautifulSoup, fallback: str) -> str:
    """Prefer first <h1>/<h2>/<h3>, then <title>, else fallback."""
    for tag in ("h1", "h2", "h3"):
        node = soup.find(tag)
        if node and node.get_text(strip=True):
            return node.get_text(strip=True)
    if soup.title and soup.title.get_text(strip=True):
        return soup.title.get_text(strip=True)
    return fallback


def _extract_chapters_zip_fallback(path: Path) -> list[tuple[str, str]]:
    """Read XHTML/HTML files directly from the zip — used when ebooklib crashes on malformed EPUBs."""
    chapters: list[tuple[str, str]] = []
    with zipfile.ZipFile(str(path)) as zf:
        names = sorted(n for n in zf.namelist() if n.lower().endswith((".xhtml", ".html", ".htm")))
        for idx, name in enumerate(names, start=1):
            try:
                raw = zf.read(name)
            except KeyError:
                continue
            soup = BeautifulSoup(raw, "lxml")
            text = soup.get_text(separator="\n", strip=True)
            if not text:
                continue
            title = _chapter_title(soup, f"Chapter {idx}")
            chapters.append((title, text))
    return chapters


def extract_chapters(path: str | Path) -> list[tuple[str, str]]:
    """Return [(title, text), ...] in spine (reading) order. Skips empty docs."""
    try:
        book = epub.read_epub(str(path))
    except (KeyError, AttributeError) as exc:
        logger.warning("ebooklib parse error on %s (%s); falling back to zip extraction", path, exc)
        return _extract_chapters_zip_fallback(Path(path))

    toc_titles = _toc_title_map(book)

    chapters: list[tuple[str, str]] = []
    spine_idx = 0
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        content = item.get_content()
        if not content:
            continue
        soup = BeautifulSoup(content, "lxml")
        text = soup.get_text(separator="\n", strip=True)
        if not text:
            continue
        spine_idx += 1
        href_key = (item.get_name() or "").split("#", 1)[0]
        title = toc_titles.get(href_key) or _chapter_title(soup, f"Chapter {spine_idx}")
        chapters.append((title, text))
    return chapters


# ── Public entry point ──────────────────────────────────────────────────────

def ingest_epub(path: str | Path, doc_id: str, title: str) -> dict:
    """Full pipeline. Returns a summary dict for logging / CLI output."""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"EPUB not found: {p}")

    sha = file_sha256(p)
    chapters = extract_chapters(p)
    chapter_count = len(chapters)

    chunks: list[dict] = []
    for ch_idx, (ch_title, ch_text) in enumerate(chapters, start=1):
        chunks.extend(
            chunk_section(
                ch_text,
                section_idx=ch_idx,
                cite=f"ch. {ch_idx}: {ch_title}",
                doc_id=doc_id,
                doc_title=title,
            )
        )

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
            page_count=chapter_count,
            chunk_count=len(chunks),
            metadata={"format": "epub", "chapter_count": chapter_count},
        )
    )

    summary = {
        "doc_id": doc_id,
        "title": title,
        "source_path": str(p),
        "format": "epub",
        "chapter_count": chapter_count,
        "chunk_count": len(chunks),
        "sha256": sha,
    }
    logger.info(
        "ingested %s: %d chapters, %d chunks", doc_id, chapter_count, len(chunks)
    )
    return summary
