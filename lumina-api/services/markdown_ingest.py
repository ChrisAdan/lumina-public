"""
Markdown ingestion pipeline for the document library.

Targets the user's Obsidian vault: short, free-form notes with sparse
headings, `#tags`, and `[[wiki-links]]`. Each `.md` file becomes one
document. Sections (split on `#`/`##`/`###` headings, never crossing a
heading boundary) are the chunk unit, mirroring how PDF chunks never
cross a page boundary and EPUB chunks never cross a chapter boundary.

Citation conventions:

  Topical notes      `(notes/<vertical>/<stem> § <heading>)`
                     `(notes/<stem>)` if no headings present
  Daily notes        `(<YYYY-MM-DD>)` — terse, since the date IS the title

Vertical is derived from the first directory under the vault root:
`Lumina/Ideas.md` → vertical=lumina, `Mentorship.md` → vertical=root,
`Dailies/2026-04-28.md` → vertical=daily.

Pipeline mirrors services.pdf_ingest / services.epub_ingest:
    extract_sections → chunk_section (per section, never crosses bounds)
                     → embed via chroma's default model → upsert
                     → upsert documents row, update chunk_count
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

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

logger = logging.getLogger("lumina.markdown_ingest")

DAILY_DIR_NAME = "Dailies"
ROOT_VERTICAL = "root"
DAILY_VERTICAL = "daily"

# YAML frontmatter is captured as metadata, not embedded as content. Bounded
# match — only at file start, and only the first --- block.
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_DAILY_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ── Path → identity ──────────────────────────────────────────────────────────

def derive_identity(path: Path, vault_root: Path) -> tuple[str, str, str]:
    """Return (doc_id, title, vertical) for a markdown file in the vault.

    Examples:
        Lumina/Ideas.md          → ("note_lumina_ideas", "Lumina / Ideas", "lumina")
        Mentorship.md            → ("note_mentorship",  "Mentorship",      "root")
        Dailies/2026-04-28.md    → ("daily_2026_04_28", "2026-04-28",      "daily")
    """
    rel = path.relative_to(vault_root)
    parts = rel.with_suffix("").parts  # drop `.md`

    if len(parts) >= 2 and parts[0] == DAILY_DIR_NAME and _DAILY_NAME_RE.match(parts[-1]):
        date_slug = parts[-1].replace("-", "_")
        return (f"daily_{date_slug}", parts[-1], DAILY_VERTICAL)

    if len(parts) == 1:
        stem = parts[0]
        slug = re.sub(r"[^a-z0-9]+", "_", stem.lower()).strip("_") or "note"
        return (f"note_{slug}", stem, ROOT_VERTICAL)

    vertical = parts[0].lower()
    stem_path = "/".join(parts)
    slug = re.sub(r"[^a-z0-9]+", "_", "_".join(parts).lower()).strip("_")
    title = " / ".join(parts)
    return (f"note_{slug}", title, vertical)


# ── Section extraction ──────────────────────────────────────────────────────

def _strip_frontmatter(text: str) -> tuple[str, dict]:
    """Pull leading YAML frontmatter into a dict; return remainder + dict.

    Intentionally permissive — Obsidian writes valid YAML but we don't import
    pyyaml just for this. Treats each line as `key: value`; multi-line values
    (rare in Obsidian) collapse to the last value seen for that key.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return text, {}
    body = text[m.end():]
    fm: dict = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    return body, fm


def extract_sections(text: str) -> list[tuple[str, str]]:
    """Split markdown body into (heading, body) tuples.

    A note with no headings returns `[("", entire_body)]` — one section, no
    heading-prefixed cite. Headings carry the heading text only (no `#` chars).
    Heading nesting is flattened — `## Foo` and `### Bar` are siblings here;
    Obsidian notes are short enough that hierarchical chunking adds noise.
    """
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        body = text.strip()
        return [("", body)] if body else []

    sections: list[tuple[str, str]] = []

    # Capture preamble (before first heading) if non-empty.
    pre = text[: matches[0].start()].strip()
    if pre:
        sections.append(("", pre))

    for i, m in enumerate(matches):
        heading = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if body or heading:
            sections.append((heading, body))

    return sections


# ── Public entry point ──────────────────────────────────────────────────────

def ingest_markdown(
    path: str | Path,
    vault_root: str | Path,
    doc_id: Optional[str] = None,
    title: Optional[str] = None,
) -> dict:
    """Full pipeline for a single markdown file. Returns a summary dict.

    `doc_id` and `title` are auto-derived from the vault-relative path when
    omitted (the scheduler-driven path always omits them).
    """
    p = Path(path).expanduser().resolve()
    root = Path(vault_root).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"markdown not found: {p}")
    if not str(p).startswith(str(root)):
        raise ValueError(f"path {p} is not inside vault root {root}")

    auto_id, auto_title, vertical = derive_identity(p, root)
    doc_id = doc_id or auto_id
    title = title or auto_title

    sha = file_sha256(p)
    raw = p.read_text(encoding="utf-8", errors="replace")
    body, frontmatter = _strip_frontmatter(raw)
    sections = extract_sections(body)
    section_count = len(sections)

    is_daily = vertical == DAILY_VERTICAL
    chunks: list[dict] = []
    for sec_idx, (heading, sec_text) in enumerate(sections, start=1):
        if is_daily:
            cite = title  # the date itself
        else:
            base = f"notes/{title}".replace(" / ", "/")
            cite = f"{base} § {heading}" if heading else base
        chunks.extend(
            chunk_section(
                sec_text,
                section_idx=sec_idx,
                cite=cite,
                doc_id=doc_id,
                doc_title=title,
            )
        )

    _wipe_doc(doc_id)

    if chunks:
        coll = _collection()
        for batch in _batched(chunks, EMBED_BATCH):
            coll.upsert(
                ids=[c["id"] for c in batch],
                documents=[c["document"] for c in batch],
                metadatas=[
                    {**c["metadata"], "vertical": vertical, "format": "md"}
                    for c in batch
                ],
            )

    documents_repo.upsert(
        DocumentCreate(
            id=doc_id,
            title=title,
            source_path=str(p),
            sha256=sha,
            page_count=section_count,
            chunk_count=len(chunks),
            metadata={
                "format": "md",
                "vertical": vertical,
                "section_count": section_count,
                "frontmatter": frontmatter,
            },
        )
    )

    summary = {
        "doc_id": doc_id,
        "title": title,
        "source_path": str(p),
        "format": "md",
        "vertical": vertical,
        "section_count": section_count,
        "chunk_count": len(chunks),
        "sha256": sha,
    }
    logger.info(
        "ingested %s: %d sections, %d chunks (%s)",
        doc_id, section_count, len(chunks), vertical,
    )
    return summary
