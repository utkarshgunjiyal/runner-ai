"""Phase 44.1 — Source-Aware Comparison Output.

The demo's deterministic fallback provider used to blend two documents into one
opaque paragraph ("Based on the available context ... Citations: E1, E2"). These
tests lock in the fix: when the builder marks a prompt as a comparison, the
provider must emit a source-separated answer — a labelled section per document,
explicit Similarities / Differences, and filename+page citations — with no
cross-document blending, and cover every selected document even when one has no
evidence.

Config-free: FinalPrompts are assembled by FinalContextBuilder (Phase 16) from
hand-built RunContexts; generation uses the deterministic fake provider. No
Mongo/Qdrant/Redis, no application settings, no real LLM.
"""

import asyncio

from app.agent.context.final_builder import FinalContextBuilder
from app.agent.llm.final_provider import DeterministicFinalProvider
from app.agent.runtime.context import EvidenceItem, RunContext


def run(coro):
    return asyncio.run(coro)


def _comparison_context(*, evidence, documents, request="Compare the technical skills in these two documents."):
    rc = RunContext.create(request, user_id="u", thread_id="t1")
    for e in evidence:
        rc.append_evidence(e)
    rc.metadata["interpretation"] = {"intents": ["document_comparison"]}
    rc.metadata["document_scope"] = {
        "status": "resolved",
        "resolved_document_ids": [d["document_id"] for d in documents],
        "documents": documents,
    }
    return rc


def _build(rc):
    return FinalContextBuilder().build(rc)


# --------------------------------------------------------------------------- #
# Builder marks the prompt as a comparison and carries the resolved documents.
# --------------------------------------------------------------------------- #

def test_builder_flags_comparison_and_carries_documents():
    rc = _comparison_context(
        evidence=[
            EvidenceItem(source="document:resumeresume.pdf", content="Python, FastAPI, MongoDB.",
                         score=0.9, metadata={"filename": "resumeresume.pdf", "page": 1, "document_id": "d1"}),
            EvidenceItem(source="document:my_final_resume.pdf", content="Go, Kubernetes, Postgres.",
                         score=0.8, metadata={"filename": "my_final_resume.pdf", "page": 2, "document_id": "d2"}),
        ],
        documents=[
            {"document_id": "d1", "filename": "resumeresume.pdf"},
            {"document_id": "d2", "filename": "my_final_resume.pdf"},
        ],
    )
    prompt = _build(rc)
    assert prompt.metadata["is_comparison"] is True
    assert [d["filename"] for d in prompt.metadata["comparison_documents"]] == [
        "resumeresume.pdf",
        "my_final_resume.pdf",
    ]


# --------------------------------------------------------------------------- #
# The deterministic provider synthesizes a source-separated comparison.
# --------------------------------------------------------------------------- #

def test_deterministic_provider_emits_source_separated_comparison():
    rc = _comparison_context(
        evidence=[
            EvidenceItem(source="document:resumeresume.pdf", content="Skilled in Python, FastAPI and MongoDB.",
                         score=0.9, metadata={"filename": "resumeresume.pdf", "page": 1, "document_id": "d1"}),
            EvidenceItem(source="document:my_final_resume.pdf", content="Skilled in Python, Kubernetes and Postgres.",
                         score=0.8, metadata={"filename": "my_final_resume.pdf", "page": 3, "document_id": "d2"}),
        ],
        documents=[
            {"document_id": "d1", "filename": "resumeresume.pdf"},
            {"document_id": "d2", "filename": "my_final_resume.pdf"},
        ],
    )
    answer = run(DeterministicFinalProvider().generate(_build(rc)))
    text = answer.text

    # Not the old blended dump.
    assert not text.startswith("Based on the available context")

    # A labelled section per document, in resolved order.
    assert "Document 1 — resumeresume.pdf" in text
    assert "Document 2 — my_final_resume.pdf" in text
    assert text.index("Document 1 — resumeresume.pdf") < text.index("Document 2 — my_final_resume.pdf")

    # Explicit Similarities and Differences sections.
    assert "Similarities" in text
    assert "Differences" in text

    # Source-aware citations: filename + page, not bare E# ids.
    assert "resumeresume.pdf p.1" in text
    assert "my_final_resume.pdf p.3" in text
    assert "Sources" in text


def test_shared_and_unique_terms_are_separated():
    rc = _comparison_context(
        evidence=[
            EvidenceItem(source="document:a.pdf", content="python fastapi mongodb",
                         score=0.9, metadata={"filename": "a.pdf", "page": 1, "document_id": "d1"}),
            EvidenceItem(source="document:b.pdf", content="python kubernetes postgres",
                         score=0.8, metadata={"filename": "b.pdf", "page": 1, "document_id": "d2"}),
        ],
        documents=[
            {"document_id": "d1", "filename": "a.pdf"},
            {"document_id": "d2", "filename": "b.pdf"},
        ],
    )
    text = run(DeterministicFinalProvider().generate(_build(rc))).text
    # "python" is shared; the framework/db terms are document-unique.
    sims = text.split("Similarities", 1)[1].split("Differences", 1)[0]
    diffs = text.split("Differences", 1)[1].split("Sources", 1)[0]
    assert "python" in sims
    assert "fastapi" in diffs and "kubernetes" in diffs
    assert "python" not in diffs


# --------------------------------------------------------------------------- #
# Balanced synthesis: a selected document with no evidence is still represented.
# --------------------------------------------------------------------------- #

def test_document_without_evidence_is_still_covered():
    rc = _comparison_context(
        evidence=[
            EvidenceItem(source="document:resumeresume.pdf", content="Python and FastAPI.",
                         score=0.9, metadata={"filename": "resumeresume.pdf", "page": 1, "document_id": "d1"}),
        ],
        documents=[
            {"document_id": "d1", "filename": "resumeresume.pdf"},
            {"document_id": "d2", "filename": "my_final_resume.pdf"},
        ],
    )
    text = run(DeterministicFinalProvider().generate(_build(rc))).text
    assert "Document 2 — my_final_resume.pdf" in text
    assert "No relevant evidence was found in my_final_resume.pdf." in text


# --------------------------------------------------------------------------- #
# Integration: the exact reported demo input flows through composition.
# --------------------------------------------------------------------------- #

def test_reported_demo_input_produces_structured_comparison():
    rc = _comparison_context(
        evidence=[
            EvidenceItem(source="document:resumeresume.pdf", content="Backend: Python, FastAPI, Redis.",
                         score=0.95, metadata={"filename": "resumeresume.pdf", "page": 1, "document_id": "d1"}),
            EvidenceItem(source="document:resumeresume.pdf", content="Data: MongoDB, Qdrant.",
                         score=0.90, metadata={"filename": "resumeresume.pdf", "page": 2, "document_id": "d1"}),
            EvidenceItem(source="document:my_final_resume.pdf", content="Cloud: AWS, Kubernetes, Terraform.",
                         score=0.88, metadata={"filename": "my_final_resume.pdf", "page": 1, "document_id": "d2"}),
        ],
        documents=[
            {"document_id": "d1", "filename": "resumeresume.pdf"},
            {"document_id": "d2", "filename": "my_final_resume.pdf"},
        ],
    )
    text = run(DeterministicFinalProvider().generate(_build(rc))).text

    for expected in (
        "resumeresume.pdf",
        "my_final_resume.pdf",
        "Similarities",
        "Differences",
    ):
        assert expected in text, expected

    # Both documents contribute evidence — neither dominates and none is dropped.
    assert "Backend: Python, FastAPI, Redis." in text
    assert "Cloud: AWS, Kubernetes, Terraform." in text
    # Streaming and non-streaming stay byte-identical for the comparison path.
    provider = DeterministicFinalProvider()

    async def _stream():
        return "".join([c async for c in provider.generate_stream(_build(rc))])

    assert run(_stream()) == run(provider.generate(_build(rc))).text
