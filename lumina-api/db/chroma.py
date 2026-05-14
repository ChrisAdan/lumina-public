import os
import chromadb
from chromadb.config import Settings

CHROMA_URL = os.getenv("CHROMA_URL", "http://chroma:8000")


def get_chroma_client() -> chromadb.HttpClient:
    """Return a ChromaDB HTTP client pointed at the chroma container."""
    host, port = CHROMA_URL.replace("http://", "").split(":")
    return chromadb.HttpClient(
        host=host,
        port=int(port),
        settings=Settings(anonymized_telemetry=False),
    )


def get_or_create_collection(name: str) -> chromadb.Collection:
    """Get or create a named collection."""
    client = get_chroma_client()
    return client.get_or_create_collection(
        name=name,
        metadata={"hnsw:space": "cosine"},
    )
