"""Deterministic lexical (BM25) reranking (Phase 44). Pure, config-free.

The default embedding provider is a hash stub with no real semantics, so dense
scores alone rank biographical/leadership chunks as highly as query-relevant
ones. This adds a BM25 lexical signal over the CHUNK TEXT (never the filename or
metadata) so query terms — Python, FastAPI, SQL, AWS, React, … — lift the chunks
that actually contain them. Dense and lexical both contribute; the reranker sees
query + chunk text only. No LLM, no model server, no new vector DB.
"""

from __future__ import annotations

import math
import re

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[.+#][a-z0-9]+)*")


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


def bm25_scores(query: str, texts: list[str], *, k1: float = 1.5, b: float = 0.75) -> list[float]:
    """BM25 score of each text against the query, over the given corpus."""
    q_terms = [t for t in dict.fromkeys(tokenize(query))]  # unique, order-stable
    docs = [tokenize(t) for t in texts]
    n = len(docs)
    if n == 0 or not q_terms:
        return [0.0] * n
    avgdl = sum(len(d) for d in docs) / n or 1.0
    # document frequency per query term
    df = {t: sum(1 for d in docs if t in d) for t in q_terms}
    scores: list[float] = []
    for d in docs:
        dl = len(d) or 1
        score = 0.0
        for t in q_terms:
            f = d.count(t)
            if f == 0:
                continue
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            score += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / avgdl))
        scores.append(score)
    return scores


def _normalize(values: list[float]) -> list[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi <= lo:
        return [0.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def rerank_hits(
    query: str,
    hits: list[dict],
    *,
    text_key: str = "text",
    lexical_weight: float = 0.7,
) -> list[dict]:
    """Return hits reordered by a blend of the (normalized) dense score and BM25
    over the chunk text. Lexical is weighted heavily because the default dense
    provider is a non-semantic stub. Each hit's ``score`` is set to the blend so
    downstream ordering/citation reflects relevance. Stable and deterministic."""
    if len(hits) <= 1:
        return list(hits)
    texts = [str(h.get(text_key, "")) for h in hits]
    lex = _normalize(bm25_scores(query, texts))
    dense = _normalize([float(h.get("score") or 0.0) for h in hits])
    blended = [
        (lexical_weight * lex[i] + (1 - lexical_weight) * dense[i]) for i in range(len(hits))
    ]
    order = sorted(range(len(hits)), key=lambda i: (-blended[i], i))
    out = []
    for rank, i in enumerate(order):
        hit = dict(hits[i])
        hit["score"] = round(blended[i], 6)
        out.append(hit)
    return out
