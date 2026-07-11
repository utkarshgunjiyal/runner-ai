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

from itertools import zip_longest

# Balanced comparison retrieval defaults (Phase 44). Configurable constants — not
# hardcoded throughout. Each selected document contributes up to
# PER_DOCUMENT_CHUNK_QUOTA chunks before a global FINAL_CHUNK_BUDGET is enforced,
# so one document cannot consume the whole top-K.
PER_DOCUMENT_CHUNK_QUOTA = 5
FINAL_CHUNK_BUDGET = 16


def _dedup_key(hit: dict) -> tuple:
    return (str(hit.get("document_id")), hit.get("page"), (hit.get("text") or "")[:160])


async def balanced_per_document_retrieve(
    *,
    retriever_fn,
    query: str,
    user_id: str,
    document_ids: list[str],
    pages: list[int] | None = None,
    per_document: int = PER_DOCUMENT_CHUNK_QUOTA,
    final_budget: int = FINAL_CHUNK_BUDGET,
) -> list[dict]:
    """Retrieve independently per document (a quota each), then round-robin merge
    with de-duplication and a final budget — so multi-document comparison keeps
    balanced, source-labelled evidence and no single document dominates."""
    per_doc: list[list[dict]] = []
    for doc_id in document_ids:
        hits = await retriever_fn(
            query=query, user_id=user_id, document_ids=[doc_id], pages=pages, top_k=per_document
        )
        per_doc.append(list(hits or [])[:per_document])

    merged: list[dict] = []
    seen: set = set()
    # Round-robin across documents so every document contributes before any
    # document contributes a second chunk.
    for group in zip_longest(*per_doc):
        for hit in group:
            if hit is None:
                continue
            key = _dedup_key(hit)
            if key in seen:
                continue
            seen.add(key)
            merged.append(hit)
            if len(merged) >= final_budget:
                return merged
    return merged


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
