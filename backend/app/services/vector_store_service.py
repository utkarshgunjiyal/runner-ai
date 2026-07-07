"""Vector store (Qdrant) — stores chunk embeddings for retrieval (Phase 2).

Phase 1 only writes vectors; retrieval is wired up in Phase 2.
"""

import uuid

from qdrant_client import AsyncQdrantClient, models

from app.config import settings
from app.logging_config import get_logger

logger = get_logger("vector_store")

# Stable namespace so point ids are deterministic -> re-ingesting a document
# overwrites its existing chunks instead of duplicating them.
_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")

_client: AsyncQdrantClient | None = None


def get_client() -> AsyncQdrantClient:
    global _client
    if _client is None:
        _client = AsyncQdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key,
        )
    return _client


async def ensure_collection() -> None:
    client = get_client()
    if not await client.collection_exists(settings.qdrant_collection):
        await client.create_collection(
            collection_name=settings.qdrant_collection,
            vectors_config=models.VectorParams(
                size=settings.embedding_dim,
                distance=models.Distance.COSINE,
            ),
        )
        logger.info(
            "vector_store.collection_created",
            extra={"collection": settings.qdrant_collection, "dim": settings.embedding_dim},
        )


def _point_id(document_id: str, chunk_index: int) -> str:
    return str(uuid.uuid5(_NAMESPACE, f"{document_id}:{chunk_index}"))


async def upsert_chunks(
    user_id: str,
    document_id: str,
    chunks: list[dict],
    vectors: list[list[float]],
) -> int:
    """Index chunk embeddings with retrieval payload. Returns count indexed."""
    client = get_client()
    await ensure_collection()

    points = [
        models.PointStruct(
            id=_point_id(document_id, chunk["chunk_index"]),
            vector=vector,
            payload={
                "user_id": user_id,
                "document_id": document_id,
                "page": chunk["page"],
                "chunk_index": chunk["chunk_index"],
                "text": chunk["text"],
            },
        )
        for chunk, vector in zip(chunks, vectors)
    ]

    if points:
        await client.upsert(collection_name=settings.qdrant_collection, points=points)
    return len(points)
