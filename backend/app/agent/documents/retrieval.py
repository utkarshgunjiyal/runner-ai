"""Scoped document retrieval wiring (Phase 43).

Finally implements the real query→vector→hits path for document retrieval in the
V2 runtime (the Phase-13 DocumentAdapter left this as a TODO). Composes the V1.5
embedding + vector-store services into a callable used by the ScopeGate and the
internal document capability.

Config-free at import: V1.5 services are imported lazily inside the factory, so
this module imports with only pydantic present. The returned callable filters
strictly by ``user_id`` and the caller-provided (Mongo-validated) document id set
— it NEVER trusts a raw client-supplied scope.
"""

from __future__ import annotations


def build_scoped_document_retriever(*, embed_fn=None, search_fn=None):
    """Return ``async retrieve(query, user_id, document_ids, pages, top_k) -> hits``.

    ``embed_fn`` / ``search_fn`` are injectable for tests. In production they lazy-
    resolve to ``embedding_service.get_embedding_provider().embed`` and
    ``vector_store_service.search_scoped``.
    """

    async def _embed(query: str) -> list[float]:
        nonlocal embed_fn
        if embed_fn is None:
            from app.services.embedding_service import get_embedding_provider

            provider = get_embedding_provider()
            embed_fn = provider.embed
        vectors = await embed_fn([query])
        return vectors[0] if vectors else []

    async def _search(**kwargs):
        nonlocal search_fn
        if search_fn is None:
            from app.services.vector_store_service import search_scoped

            search_fn = search_scoped
        return await search_fn(**kwargs)

    async def retrieve(
        *,
        query: str,
        user_id: str,
        document_ids: list[str] | None = None,
        pages: list[int] | None = None,
        top_k: int = 8,
    ) -> list[dict]:
        if not query or not user_id:
            return []
        vector = await _embed(query)
        if not vector:
            return []
        return await _search(
            query_vector=vector,
            user_id=user_id,
            top_k=top_k,
            document_ids=list(document_ids) if document_ids else None,
            pages=list(pages) if pages else None,
        )

    return retrieve
