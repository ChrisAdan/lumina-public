"""
services/people_context.py
Helpers for the people synapse vertical:

- discover known people from the Obsidian vault
- query the ChromaDB `people` collection
- build compact runtime context blocks for chat turns
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from configs.app import LUMINA_OBSIDIAN_VAULT_PATH
from db.chroma import get_chroma_client

COLLECTION = "people"
MAX_CHUNKS = 3

_WORD_BOUNDARY = r"(?<![A-Za-z0-9]){pat}(?![A-Za-z0-9])"


def _humanize_person_id(person_id: str) -> str:
    return re.sub(r"[_\-]+", " ", person_id).strip()


def _normalize_token(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _candidate_people_dirs() -> list[Path]:
    root = Path(LUMINA_OBSIDIAN_VAULT_PATH).expanduser()
    return [
        root / "synapses" / "people",
        root / "Synapses" / "People",
        root / "Lumina" / "Synapses" / "People",
        root / "Lumina" / "synapses" / "people",
    ]


def people_dir() -> Path:
    for path in _candidate_people_dirs():
        if path.exists():
            return path
    return _candidate_people_dirs()[0]


def known_people() -> list[dict[str, str]]:
    base = people_dir()
    if not base.exists():
        return []

    people: list[dict[str, str]] = []
    for md_file in sorted(base.glob("*.md")):
        if md_file.name.startswith("_"):
            continue
        people.append(
            {
                "id": md_file.stem.lower(),
                "name": _humanize_person_id(md_file.stem),
                "source_path": str(md_file),
            }
        )
    return people


def resolve_person_id(person: str | None) -> str | None:
    if not person:
        return None
    needle = _normalize_token(person)
    if not needle:
        return None
    for known in known_people():
        if needle in {_normalize_token(known["id"]), _normalize_token(known["name"])}:
            return known["id"]
    return person.strip().lower()


def extract_names(message: str) -> list[str]:
    """Return known person ids found in the message, most-specific first."""
    normalized_message = _normalize_token(message)
    if not normalized_message:
        return []

    matches: list[tuple[int, str]] = []
    for person in known_people():
        name_pat = re.escape(_normalize_token(person["name"]))
        if not name_pat:
            continue
        if re.search(_WORD_BOUNDARY.format(pat=name_pat), normalized_message):
            matches.append((len(name_pat), person["id"]))

    matches.sort(reverse=True)
    seen: set[str] = set()
    ordered: list[str] = []
    for _, person_id in matches:
        if person_id in seen:
            continue
        seen.add(person_id)
        ordered.append(person_id)
    return ordered


def _query_collection(query_text: str, *, person: str | None, top_k: int) -> list[dict[str, Any]]:
    client = get_chroma_client()
    collection = client.get_or_create_collection(COLLECTION)
    where = {"person": person.lower()} if person else None
    raw = collection.query(
        query_texts=[query_text],
        n_results=max(1, min(int(top_k), 10)),
        where=where,
    )

    ids = (raw.get("ids") or [[]])[0]
    docs = (raw.get("documents") or [[]])[0]
    metas = (raw.get("metadatas") or [[]])[0]
    distances = (raw.get("distances") or [[]])[0]

    hits: list[dict[str, Any]] = []
    for i, _ in enumerate(ids):
        meta = metas[i] or {}
        person_id = meta.get("person", "")
        hits.append(
            {
                "person": person_id,
                "display_name": meta.get("display_name") or _humanize_person_id(person_id),
                "source_path": meta.get("source", ""),
                "chunk_idx": int(meta.get("chunk", 0)),
                "content": docs[i] or "",
                "score": round(float(distances[i]), 4) if i < len(distances) else None,
            }
        )
    return hits


def query_people(q: str | None = None, *, person: str | None = None, top_k: int = 4) -> dict[str, Any]:
    query_text = (q or person or "").strip()
    if not query_text:
        return {"query": q or "", "person": person, "hits": [], "error": "q or person is required"}

    normalized_person = resolve_person_id(person)
    hits = _query_collection(query_text, person=normalized_person, top_k=top_k)
    return {
        "query": query_text,
        "person": normalized_person,
        "count": len(hits),
        "hits": hits,
    }


def get_people_context(message: str) -> str | None:
    """
    If any known people are mentioned, retrieve relevant synapse chunks and
    return a compact runtime context block for prompt injection.
    """
    names = extract_names(message)
    if not names:
        return None

    blocks: list[str] = []
    for person_id in names:
        result = query_people(message, person=person_id, top_k=MAX_CHUNKS)
        hits = result.get("hits") or []
        if not hits:
            continue
        display_name = hits[0].get("display_name") or _humanize_person_id(person_id)
        body = "\n".join(f"- {hit['content']}" for hit in hits if hit.get("content"))
        if body:
            blocks.append(f"[Known context — {display_name}]\n{body}")

    if not blocks:
        return None
    return "## Known people context\n\n" + "\n\n".join(blocks)
