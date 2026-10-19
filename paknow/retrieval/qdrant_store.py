"""Qdrant client: embedded local mode (default) or remote server.

Text collection carries a dense vector (BGE-M3) and a named sparse vector
(lexical). Visual collection carries ColPali page multi-vectors (added later).
"""
from __future__ import annotations

from functools import lru_cache

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    SparseVectorParams,
    VectorParams,
)

from paknow.config import settings

COLLECTION_TEXT = "evidence_text"
COLLECTION_VISUAL = "page_visual"
DENSE_DIM = 1024  # BGE-M3


@lru_cache(maxsize=1)
def get_client() -> QdrantClient:
    """Singleton client. Qdrant local mode allows only one instance per path."""
    if settings.qdrant_url:
        return QdrantClient(url=settings.qdrant_url)
    return QdrantClient(path=str(settings.objects_dir / "qdrant"))


def ensure_collections(client: QdrantClient | None = None) -> None:
    client = client or get_client()
    if not client.collection_exists(COLLECTION_TEXT):
        client.create_collection(
            collection_name=COLLECTION_TEXT,
            vectors_config={"dense": VectorParams(size=DENSE_DIM, distance=Distance.COSINE)},
            sparse_vectors_config={"sparse": SparseVectorParams()},
        )
    if not client.collection_exists(COLLECTION_VISUAL):
        # multi-vector pages: one vector per patch; dims set at ColPali load
        client.create_collection(
            collection_name=COLLECTION_VISUAL,
            vectors_config={},
        )
