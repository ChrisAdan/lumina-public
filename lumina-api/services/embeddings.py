"""
services/embeddings.py
Embed-on-write service for ChromaDB collections.

Called from routers after a successful DB write to keep ChromaDB in sync.
Failures are logged but never bubble up — a missing embedding is recoverable,
a failed write is not.

Collections:
    recipes  — title + ingredients summary + tags
    notes    — title + content

Usage (in a router, after db.commit()):
    from services.embeddings import embed_recipe, embed_note
    await embed_recipe(recipe_id, title, ingredients, tags)
    await embed_note(note_id, title, content)
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import chromadb
import httpx
import numpy as np
from chromadb.config import Settings

from configs.app import CHROMA_HOST, CHROMA_PORT, OLLAMA_URL

logger = logging.getLogger("lumina.embeddings")

# ── Ollama embedding function ─────────────────────────────────────────────────

OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")


class OllamaEmbeddingFunction:
    """ChromaDB-compatible embedding function backed by Ollama's /api/embed.

    Passed to get_or_create_collection so ChromaDB calls it client-side for
    both upsert (on documents=) and query (on query_texts=), then ships the
    pre-computed vectors to the Chroma server. Switching embed models requires
    wiping and re-ingesting all affected collections.
    """

    def __init__(self, model: str = OLLAMA_EMBED_MODEL, base_url: str = OLLAMA_URL):
        self._model = model
        self._url = f"{base_url}/api/embed"

    def __call__(self, input: list[str]) -> list[np.ndarray]:
        resp = httpx.post(
            self._url,
            json={"model": self._model, "input": input},
            timeout=120.0,
        )
        resp.raise_for_status()
        # chromadb.api.types calls .tolist() on each embedding, so we must
        # return numpy arrays rather than plain Python lists.
        return [np.array(e, dtype=np.float32) for e in resp.json()["embeddings"]]


_embed_fn: OllamaEmbeddingFunction | None = None


def _ollama_ef() -> OllamaEmbeddingFunction:
    """Singleton embedding function — one instance reused across all collections."""
    global _embed_fn
    if _embed_fn is None:
        _embed_fn = OllamaEmbeddingFunction()
    return _embed_fn


# ── Chroma client + collection factory ───────────────────────────────────────

def _client() -> chromadb.HttpClient:
    return chromadb.HttpClient(
        host=CHROMA_HOST,
        port=int(CHROMA_PORT),
        settings=Settings(anonymized_telemetry=False),
    )


def _get_or_create(client: chromadb.HttpClient, name: str):
    return client.get_or_create_collection(
        name=name,
        embedding_function=_ollama_ef(),
        metadata={"hnsw:space": "cosine"},
    )


# ── Recipes ───────────────────────────────────────────────────────────────────

def embed_recipe(
    recipe_id: int,
    title: str,
    ingredients: Any,          # list[dict] or raw JSONB
    tags: list[str] | None,
    instructions: str | None = None,
) -> None:
    """
    Embed a recipe into the 'recipes' ChromaDB collection.
    The document string is: title + ingredient names + tags.
    Instructions deliberately excluded — too long, hurts retrieval precision.
    """
    try:
        client = _client()
        collection = _get_or_create(client, "recipes")

        # Extract ingredient names from JSONB list
        if isinstance(ingredients, list):
            ingredient_names = [
                i.get("name", "") for i in ingredients if isinstance(i, dict)
            ]
        elif isinstance(ingredients, str):
            try:
                parsed = json.loads(ingredients)
                ingredient_names = [i.get("name", "") for i in parsed if isinstance(i, dict)]
            except Exception:
                ingredient_names = []
        else:
            ingredient_names = []

        tag_str = " ".join(tags or [])
        ingredients_str = " ".join(filter(None, ingredient_names))
        document = f"{title}. Ingredients: {ingredients_str}. Tags: {tag_str}".strip()

        collection.upsert(
            ids=[str(recipe_id)],
            documents=[document],
            metadatas=[{"recipe_id": recipe_id, "title": title}],
        )
        logger.info("embedded recipe %d: %s", recipe_id, title)

    except Exception as exc:
        logger.warning("failed to embed recipe %d: %s", recipe_id, exc)


def delete_recipe_embedding(recipe_id: int) -> None:
    try:
        client = _client()
        collection = _get_or_create(client, "recipes")
        collection.delete(ids=[str(recipe_id)])
        logger.info("deleted recipe embedding %d", recipe_id)
    except Exception as exc:
        logger.warning("failed to delete recipe embedding %d: %s", recipe_id, exc)


# ── Notes ─────────────────────────────────────────────────────────────────────

def embed_note(
    note_id: int,
    title: str | None,
    content: str | None,
    tags: list[str] | None = None,
) -> None:
    """
    Embed a note into the 'notes' ChromaDB collection.
    Document is full title + content (notes are typically short).
    """
    try:
        client = _client()
        collection = _get_or_create(client, "notes")

        parts = filter(None, [title, content, " ".join(tags or [])])
        document = " ".join(parts).strip()

        if not document:
            logger.warning("skipping empty note embedding for note %d", note_id)
            return

        collection.upsert(
            ids=[str(note_id)],
            documents=[document],
            metadatas=[{"note_id": note_id, "title": title or ""}],
        )
        logger.info("embedded note %d", note_id)

    except Exception as exc:
        logger.warning("failed to embed note %d: %s", note_id, exc)


def delete_note_embedding(note_id: int) -> None:
    try:
        client = _client()
        collection = _get_or_create(client, "notes")
        collection.delete(ids=[str(note_id)])
    except Exception as exc:
        logger.warning("failed to delete note embedding %d: %s", note_id, exc)


# ── Semantic search helpers ───────────────────────────────────────────────────

def search_recipes(query: str, n_results: int = 5) -> list[dict]:
    """
    Semantic search over the recipes collection.
    Returns list of {recipe_id, title, distance}.
    """
    try:
        client = _client()
        collection = _get_or_create(client, "recipes")
        results = collection.query(query_texts=[query], n_results=n_results)

        output = []
        for i, doc_id in enumerate(results["ids"][0]):
            meta = results["metadatas"][0][i]
            dist = results["distances"][0][i]
            output.append({
                "recipe_id": meta.get("recipe_id"),
                "title":     meta.get("title"),
                "distance":  round(dist, 4),
            })
        return output

    except Exception as exc:
        logger.warning("recipe semantic search failed: %s", exc)
        return []


def search_notes(query: str, n_results: int = 5) -> list[dict]:
    """
    Semantic search over the notes collection.
    Returns list of {note_id, title, distance}.
    """
    try:
        client = _client()
        collection = _get_or_create(client, "notes")
        results = collection.query(query_texts=[query], n_results=n_results)

        output = []
        for i, doc_id in enumerate(results["ids"][0]):
            meta = results["metadatas"][0][i]
            dist = results["distances"][0][i]
            output.append({
                "note_id": meta.get("note_id"),
                "title":   meta.get("title"),
                "distance": round(dist, 4),
            })
        return output

    except Exception as exc:
        logger.warning("note semantic search failed: %s", exc)
        return []
