"""Vector store (Qdrant) — chunk embeddings: indexing (Phase 1) + search (Phase 2)."""

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


# ---------------------------------------------------------------------------
# Retrieval (Phase 2)
# ---------------------------------------------------------------------------

def _build_filter(
    user_id: str,
    document_id: str | None = None,
    page: int | None = None,
) -> models.Filter:
    must = [
        models.FieldCondition(key="user_id", match=models.MatchValue(value=user_id)),
    ]
    if document_id is not None:
        must.append(
            models.FieldCondition(
                key="document_id", match=models.MatchValue(value=document_id)
            )
        )
    if page is not None:
        must.append(
            models.FieldCondition(key="page", match=models.MatchValue(value=page))
        )
    return models.Filter(must=must)


def _to_hit(payload: dict, score: float | None) -> dict:
    return {
        "text": payload.get("text", ""),
        "page": payload.get("page"),
        "document_id": payload.get("document_id"),
        "chunk_index": payload.get("chunk_index"),
        "score": score,
    }


async def search(
    query_vector: list[float],
    user_id: str,
    top_k: int,
    document_id: str | None = None,
    page: int | None = None,
) -> list[dict]:
    """Semantic search, filtered by user (and optionally document/page).

    Returns hits with text, page, document_id, chunk_index, score — ordered by
    descending similarity. Safe on an empty/missing collection (returns []).
    """
    if top_k <= 0:
        return []
    client = get_client()
    await ensure_collection()

    response = await client.query_points(
        collection_name=settings.qdrant_collection,
        query=query_vector,
        query_filter=_build_filter(user_id, document_id, page),
        limit=top_k,
        with_payload=True,
    )
    return [_to_hit(point.payload, point.score) for point in response.points]


async def list_page_chunks(
    user_id: str,
    document_id: str | None,
    page: int,
    limit: int = 50,
) -> list[dict]:
    """Deterministically fetch all chunks on a page, ordered by chunk_index.

    Used for page-scoped retrieval (no query vector needed).
    """
    client = get_client()
    await ensure_collection()

    points, _ = await client.scroll(
        collection_name=settings.qdrant_collection,
        scroll_filter=_build_filter(user_id, document_id, page),
        limit=limit,
        with_payload=True,
    )
    hits = [_to_hit(point.payload, None) for point in points]
    hits.sort(key=lambda hit: (hit["chunk_index"] is None, hit["chunk_index"]))
    return hits
