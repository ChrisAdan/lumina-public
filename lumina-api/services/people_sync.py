"""
services/people_sync.py
Syncs people markdown files from the Obsidian vault into ChromaDB collection
`people`. Intended for CRON plus manual refresh via the /people router.
"""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

from db.chroma import get_chroma_client
from services.people_context import COLLECTION, known_people, people_dir

logger = logging.getLogger("lumina.people_sync")

CHUNK_SIZE = 400

_WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")


def _unwikilink(text: str) -> str:
    return _WIKILINK.sub(lambda m: m.group(1), text).strip()


def _strip_relationships_block(content: str) -> str:
    """Remove the - Relationships: nested block from raw content.

    The prose chunk already encodes this information unambiguously; leaving
    the raw nested structure in other chunks causes the model to misread
    role labels (e.g. 'Child → Mother' misread as the person being a mother).
    """
    lines = content.splitlines(keepends=True)
    out: list[str] = []
    in_rel = False
    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip("\t "))
        if not in_rel:
            if stripped == "- Relationships:" or stripped == "- Relationships":
                in_rel = True
                continue
            out.append(line)
        else:
            # Exit when we hit another top-level list item (indent 0, starts with -)
            if indent == 0 and stripped.startswith("-"):
                in_rel = False
                out.append(line)
    return "".join(out)


def _relationship_prose(content: str, display_name: str) -> str | None:
    """Parse the Obsidian nested relationship list and return unambiguous prose.

    The Obsidian format uses section headers (Spouse, Child, Sibling) that
    describe the person's role — e.g. 'Child' means they are *someone else's* child,
    with their parents listed beneath. The raw markdown is ambiguous to LLMs;
    this function converts it to a flat sentence so retrieved chunks are clear.
    """
    lines = content.splitlines()
    in_rel = False
    section: str | None = None
    parts: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("-"):
            if in_rel and not stripped:
                break
            continue

        indent = len(line) - len(line.lstrip("\t "))
        item = stripped.lstrip("- ").strip()

        if indent == 0 and item.rstrip(":").lower() == "relationships":
            in_rel = True
            continue

        if not in_rel:
            continue

        if indent == 1:
            section = item.rstrip(":").strip()
            continue

        if indent == 2 and section and ":" in item:
            role_raw, rest = item.split(":", 1)
            role = role_raw.strip()
            person = _unwikilink(rest)
            sec = section.lower()

            if sec == "self":
                continue
            if sec == "spouse":
                parts.append(f"married to {person}")
            elif sec == "child":
                # person is the child; listed people are their parents/in-laws
                r = role.lower()
                if r == "mother":
                    parts.append(f"mother is {person}")
                elif r == "father":
                    parts.append(f"father is {person}")
                elif r == "mother-in-law":
                    parts.append(f"mother-in-law is {person}")
                elif r == "father-in-law":
                    parts.append(f"father-in-law is {person}")
                else:
                    parts.append(f"{role} {person}")
            elif sec == "sibling":
                parts.append(f"{role.lower()} {person}")
            else:
                parts.append(f"{role} {person}")

    if not parts:
        return None
    return f"{display_name}: {'; '.join(parts)}."


def _chunk_text(text: str, size: int = CHUNK_SIZE) -> list[str]:
    """Split on paragraph breaks, fall back to fixed-size slices."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if not current:
            current = paragraph
            continue
        if len(current) + 2 + len(paragraph) <= size:
            current = f"{current}\n\n{paragraph}"
            continue
        chunks.append(current.strip())
        current = paragraph
    if current:
        chunks.append(current.strip())
    if chunks:
        return chunks
    text = text.strip()
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def _file_hash(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def sync_people() -> dict:
    """
    Upsert all people files into ChromaDB and delete orphaned embeddings.
    Returns a structured summary for routers / scheduler logs.
    """
    base = people_dir()
    client = get_chroma_client()
    collection = client.get_or_create_collection(COLLECTION)

    if not base.exists():
        summary = {
            "ingested": 0,
            "updated": 0,
            "skipped": 0,
            "deleted": 0,
            "errored": 0,
            "people_count": 0,
            "people_dir": str(base),
        }
        logger.warning("[people_sync] people dir missing, skipping: %s", base)
        return summary

    known = known_people()
    current_people = {p["id"]: p for p in known}
    ingested = updated = skipped = errored = 0

    for person in known:
        path = Path(person["source_path"])
        person_id = person["id"]
        display_name = person["name"]
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            file_hash = _file_hash(path)

            existing = collection.get(
                where={"$and": [{"person": person_id}, {"file_hash": file_hash}]},
                limit=1,
            )
            if existing["ids"]:
                skipped += 1
                continue

            prior = collection.get(where={"person": person_id})
            had_existing = bool(prior["ids"])
            if had_existing:
                collection.delete(ids=prior["ids"])

            rel_prose = _relationship_prose(content, display_name)
            stripped_content = _strip_relationships_block(content) if rel_prose else content
            raw_chunks = _chunk_text(stripped_content)
            chunks = ([rel_prose] + raw_chunks) if rel_prose else raw_chunks
            ids: list[str] = []
            docs: list[str] = []
            metas: list[dict] = []
            for i, chunk in enumerate(chunks):
                ids.append(f"{person_id}_{i}_{file_hash[:8]}")
                docs.append(chunk)
                metas.append(
                    {
                        "person": person_id,
                        "display_name": display_name,
                        "file_hash": file_hash,
                        "chunk": i,
                        "source": str(path),
                    }
                )

            collection.upsert(ids=ids, documents=docs, metadatas=metas)
            if had_existing:
                updated += 1
            else:
                ingested += 1
            logger.info("[people_sync] embedded %s: %d chunk(s)", person_id, len(chunks))
        except Exception as exc:
            errored += 1
            logger.error("[people_sync] failed %s: %s", person_id, exc)

    deleted = 0
    existing = collection.get()
    ids = existing.get("ids") or []
    metas = existing.get("metadatas") or []
    orphan_ids = [
        chunk_id
        for chunk_id, meta in zip(ids, metas)
        if (meta or {}).get("person") not in current_people
    ]
    if orphan_ids:
        collection.delete(ids=orphan_ids)
        deleted = len(orphan_ids)
        logger.info("[people_sync] deleted %d orphan chunk(s)", deleted)

    summary = {
        "ingested": ingested,
        "updated": updated,
        "skipped": skipped,
        "deleted": deleted,
        "errored": errored,
        "people_count": len(current_people),
        "people_dir": str(base),
    }
    logger.info("[people_sync] %s", summary)
    return summary
