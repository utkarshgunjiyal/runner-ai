"""Phase 44 — deterministic BM25 lexical reranking (defect 5). Config-free."""

from app.agent.retriever.lexical import bm25_scores, rerank_hits, tokenize


def test_tokenize_keeps_technical_terms():
    assert "fastapi" in tokenize("I used FastAPI and Python")
    # dotted/plus/hash terms survive as single tokens
    assert "c++" in tokenize("wrote C++ code") or "c" in tokenize("wrote C++ code")


def test_bm25_ranks_matching_text_higher():
    texts = [
        "Led a team and mentored engineers across the organization.",  # leadership
        "Built REST APIs in Python and FastAPI, with SQL and AWS.",     # technical
    ]
    scores = bm25_scores("python fastapi sql skills", texts)
    assert scores[1] > scores[0]


def test_rerank_promotes_technical_chunk_over_leadership():
    hits = [
        {"text": "Led a team and mentored engineers; strong leadership.", "document_id": "d", "page": 1, "score": 0.9},
        {"text": "Python, FastAPI, SQL, AWS, React — hands-on engineering.", "document_id": "d", "page": 2, "score": 0.1},
    ]
    reranked = rerank_hits("what are the python and fastapi technical skills?", hits)
    assert reranked[0]["page"] == 2  # technical chunk first despite lower dense score


def test_exact_terms_lift_ranking():
    hits = [
        {"text": "general background and interests", "document_id": "d", "page": 1, "score": 0.5},
        {"text": "experience with LangGraph and Qdrant", "document_id": "d", "page": 2, "score": 0.5},
    ]
    reranked = rerank_hits("LangGraph Qdrant", hits)
    assert reranked[0]["page"] == 2


def test_single_hit_is_returned_unchanged():
    hits = [{"text": "only one", "document_id": "d", "page": 1, "score": 0.5}]
    assert rerank_hits("q", hits) == hits
