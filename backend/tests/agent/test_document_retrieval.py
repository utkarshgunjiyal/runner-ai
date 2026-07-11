"""Phase 43 — scoped document retriever wiring. Config-free (fakes injected)."""

import asyncio

from app.agent.documents import build_scoped_document_retriever


def run(coro):
    return asyncio.run(coro)


def test_retriever_embeds_then_searches_with_scope():
    calls = {}

    async def embed(texts):
        calls["embedded"] = list(texts)
        return [[0.1, 0.2, 0.3]]

    async def search(*, query_vector, user_id, top_k, document_ids, pages):
        calls["search"] = {
            "user_id": user_id, "top_k": top_k,
            "document_ids": document_ids, "pages": pages, "vector": query_vector,
        }
        return [{"text": "hit", "document_id": document_ids[0], "page": 1, "score": 0.9}]

    retrieve = build_scoped_document_retriever(embed_fn=embed, search_fn=search)
    hits = run(retrieve(query="pricing", user_id="u", document_ids=["d1", "d2"], pages=[2], top_k=5))

    assert calls["embedded"] == ["pricing"]
    assert calls["search"]["user_id"] == "u"
    assert calls["search"]["document_ids"] == ["d1", "d2"]
    assert calls["search"]["pages"] == [2]
    assert calls["search"]["vector"] == [0.1, 0.2, 0.3]
    assert hits[0]["text"] == "hit"


def test_retriever_guards_empty_query_and_user():
    async def embed(texts):
        raise AssertionError("should not embed")

    async def search(**kwargs):
        raise AssertionError("should not search")

    retrieve = build_scoped_document_retriever(embed_fn=embed, search_fn=search)
    assert run(retrieve(query="", user_id="u")) == []
    assert run(retrieve(query="x", user_id="")) == []


def test_no_document_ids_passes_none_scope():
    seen = {}

    async def embed(texts):
        return [[1.0]]

    async def search(*, query_vector, user_id, top_k, document_ids, pages):
        seen["document_ids"] = document_ids
        seen["pages"] = pages
        return []

    retrieve = build_scoped_document_retriever(embed_fn=embed, search_fn=search)
    run(retrieve(query="q", user_id="u"))
    assert seen["document_ids"] is None
    assert seen["pages"] is None
