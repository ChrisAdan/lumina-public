"""
Semantic tool selection — ChromaDB cosine search over tool descriptions.

Replaces BM25-lite. Tool descriptions are embedded into a 'tools' ChromaDB
collection at startup via `inference.tool_registry.embed_tools`. At request
time the user message is the query; ChromaDB returns the most semantically
relevant tool IDs without a keyword-match requirement.

Selection rules:
  1. ALWAYS_ON tools are always included regardless of score.
  2. triage_hints (from Phase 14.1 classifier) are always included.
  3. Remaining budget (k - |forced|) filled by ChromaDB cosine rank descending.
  4. Falls back to full tool list if ChromaDB is unavailable or returns nothing.
"""
from __future__ import annotations

import logging

import chromadb
from chromadb.config import Settings

from configs.app import CHROMA_HOST, CHROMA_PORT

log = logging.getLogger(__name__)

ALWAYS_ON: frozenset[str] = frozenset({"web_search", "fetch_url", "file_list", "file_read", "file_write"})
DEFAULT_K: int = 12

_all_ids: list[str] = []
_collection = None


def _get_collection():
    global _collection
    if _collection is None:
        client = chromadb.HttpClient(
            host=CHROMA_HOST,
            port=int(CHROMA_PORT),
            settings=Settings(anonymized_telemetry=False),
        )
        _collection = client.get_or_create_collection(
            name="tools",
            metadata={"hnsw:space": "cosine"},
        )
    return _collection


def build_index(tools: list) -> None:
    """Store the ordered tool ID list. Called at startup after embed_tools()."""
    global _all_ids
    _all_ids = [t.name for t in tools]


def select_tools(
    user_message: str,
    triage_hints: list[str],
    k: int = DEFAULT_K,
    exclude: set[str] | None = None,
) -> list[str]:
    """Return up to k tool IDs relevant to this request.

    `exclude` is a hard blocklist — those tools will not appear in the result
    regardless of hints or semantic score. Falls back to the full tool list
    (minus excluded) if ChromaDB is unavailable.
    """
    excluded: set[str] = exclude or set()
    all_set = set(_all_ids) - excluded
    forced = list(dict.fromkeys(             # preserve order, deduplicate
        tid for tid in list(ALWAYS_ON) + list(triage_hints)
        if tid in all_set
    ))
    remaining_k = k - len(forced)

    if remaining_k <= 0 or not _all_ids:
        return forced

    if not user_message.strip():
        # No query signal — fill remaining budget from registration order
        candidates = [tid for tid in _all_ids if tid not in set(forced) and tid not in excluded]
        return forced + candidates[:remaining_k]

    try:
        collection = _get_collection()
        count = collection.count()
        if count == 0:
            print("---- TOOL_SELECT 'tools' collection empty, returning full list")
            return [t for t in _all_ids if t not in excluded]

        # Query for more than we need so we can filter out forced/excluded tools
        n_query = min(remaining_k + len(forced), count)
        results = collection.query(query_texts=[user_message], n_results=n_query)
        retrieved = results["ids"][0] if results["ids"] else []

        forced_set = set(forced)
        semantic = [tid for tid in retrieved if tid not in forced_set and tid not in excluded][:remaining_k]
        return forced + semantic

    except Exception as e:
        print(f"---- TOOL_SELECT ChromaDB error, falling back to full list: {e}")
        return [t for t in _all_ids if t not in excluded]
