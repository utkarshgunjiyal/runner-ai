"""Phase 44 — balanced per-document retrieval (defects 2, 4). Config-free."""

import asyncio

from app.agent.documents import balanced_per_document_retrieve


def run(coro):
    return asyncio.run(coro)


def _make_retriever(per_doc_hits):
    calls = []

    async def retriever_fn(*, query, user_id, document_ids, pages, top_k):
        calls.append({"document_ids": list(document_ids), "top_k": top_k})
        doc = document_ids[0]
        return [dict(h) for h in per_doc_hits.get(doc, [])][:top_k]

    return retriever_fn, calls


def test_each_document_contributes_and_none_dominates():
    hits = {
        "A": [{"text": f"a{i}", "document_id": "A", "page": i, "filename": "A.pdf", "score": 0.9} for i in range(10)],
        "B": [{"text": f"b{i}", "document_id": "B", "page": i, "filename": "B.pdf", "score": 0.1} for i in range(2)],
    }
    fn, calls = _make_retriever(hits)
    merged = run(balanced_per_document_retrieve(
        retriever_fn=fn, query="compare", user_id="u", document_ids=["A", "B"],
    ))
    docs = {h["document_id"] for h in merged}
    assert docs == {"A", "B"}  # both contribute
    # A cannot swallow everything — B's chunks appear early (round-robin).
    assert merged[1]["document_id"] == "B"
    # one call per document, each with its own quota
    assert [c["document_ids"] for c in calls] == [["A"], ["B"]]


def test_filenames_and_pages_survive_to_merged():
    hits = {
        "A": [{"text": "a", "document_id": "A", "page": 3, "filename": "resume.pdf", "score": 0.5}],
        "B": [{"text": "b", "document_id": "B", "page": 7, "filename": "application.pdf", "score": 0.5}],
    }
    fn, _ = _make_retriever(hits)
    merged = run(balanced_per_document_retrieve(retriever_fn=fn, query="q", user_id="u", document_ids=["A", "B"]))
    by_doc = {h["document_id"]: h for h in merged}
    assert by_doc["A"]["filename"] == "resume.pdf" and by_doc["A"]["page"] == 3
    assert by_doc["B"]["filename"] == "application.pdf" and by_doc["B"]["page"] == 7


def test_deduplication_of_identical_chunks():
    dup = {"text": "same chunk text", "document_id": "A", "page": 1, "filename": "A.pdf", "score": 0.9}
    hits = {"A": [dup, dict(dup)], "B": [{"text": "b", "document_id": "B", "page": 1, "filename": "B.pdf", "score": 0.5}]}
    fn, _ = _make_retriever(hits)
    merged = run(balanced_per_document_retrieve(retriever_fn=fn, query="q", user_id="u", document_ids=["A", "B"]))
    a_chunks = [h for h in merged if h["document_id"] == "A"]
    assert len(a_chunks) == 1  # duplicate collapsed


def test_final_budget_enforced():
    hits = {d: [{"text": f"{d}{i}", "document_id": d, "page": i, "filename": f"{d}.pdf", "score": 0.5} for i in range(10)]
            for d in ("A", "B", "C")}
    fn, _ = _make_retriever(hits)
    merged = run(balanced_per_document_retrieve(
        retriever_fn=fn, query="q", user_id="u", document_ids=["A", "B", "C"], final_budget=6,
    ))
    assert len(merged) == 6
