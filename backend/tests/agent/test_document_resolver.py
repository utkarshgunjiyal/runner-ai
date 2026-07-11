"""Phase 43 — deterministic document resolver. Config-free."""

from app.agent.documents import (
    DocumentResolutionStatus,
    resolve_documents,
)

DOCS = [
    {"document_id": "d1", "filename": "Q3 Report.pdf", "created_at": "2026-01-01"},
    {"document_id": "d2", "filename": "Q4 Report.pdf", "created_at": "2026-02-01"},
    {"document_id": "d3", "filename": "Onboarding.pdf", "created_at": "2026-03-01"},
]


def test_no_documents():
    r = resolve_documents(user_request="the report", thread_documents=[])
    assert r.status == DocumentResolutionStatus.NO_DOCUMENTS


def test_ui_selection_wins():
    r = resolve_documents(
        user_request="what does it say?", thread_documents=DOCS,
        selected_document_ids=["d2"],
    )
    assert r.status == DocumentResolutionStatus.RESOLVED
    assert r.document_ids == ["d2"]
    assert r.resolution_source == "ui_selection"


def test_cross_thread_selected_id_rejected_as_unauthorized():
    r = resolve_documents(
        user_request="x", thread_documents=DOCS, selected_document_ids=["d1", "NOT_MINE"],
    )
    assert r.status == DocumentResolutionStatus.UNAUTHORIZED


def test_wants_all_resolves_to_every_thread_document():
    r = resolve_documents(user_request="summarize everything", thread_documents=DOCS, wants_all=True)
    assert r.status == DocumentResolutionStatus.RESOLVED
    assert set(r.document_ids) == {"d1", "d2", "d3"}
    assert r.resolution_source == "all_thread_documents"


def test_exact_filename_match():
    r = resolve_documents(user_request="x", thread_documents=DOCS, references=["Onboarding.pdf"])
    assert r.status == DocumentResolutionStatus.RESOLVED
    assert r.document_ids == ["d3"]
    assert r.resolution_source == "exact_filename"


def test_normalized_filename_match():
    r = resolve_documents(user_request="x", thread_documents=DOCS, references=["onboarding"])
    assert r.status == DocumentResolutionStatus.RESOLVED
    assert r.document_ids == ["d3"]
    assert r.resolution_source == "normalized_filename"


def test_partial_title_match_unique():
    r = resolve_documents(user_request="x", thread_documents=DOCS, references=["q4"])
    assert r.status == DocumentResolutionStatus.RESOLVED
    assert r.document_ids == ["d2"]


def test_named_but_not_found():
    r = resolve_documents(user_request="x", thread_documents=DOCS, references=["nonexistent.pdf"])
    assert r.status == DocumentResolutionStatus.NOT_FOUND


def test_ambiguous_partial_reference():
    # "report" matches both Q3 and Q4 → ambiguous with a safe candidate list.
    r = resolve_documents(user_request="the report", thread_documents=DOCS, references=["report"])
    assert r.status == DocumentResolutionStatus.AMBIGUOUS
    assert {c.document_id for c in r.candidates} == {"d1", "d2"}
    assert r.clarification_prompt


def test_vague_reference_with_multiple_docs_is_ambiguous():
    r = resolve_documents(
        user_request="summarize the document", thread_documents=DOCS, references=["the document"],
    )
    assert r.status == DocumentResolutionStatus.AMBIGUOUS
    assert len(r.candidates) == 3
    # candidates are SAFE — only id/filename/created_at
    assert set(r.candidates[0].model_dump()) == {"document_id", "filename", "created_at"}


def test_single_document_resolves_without_reference():
    r = resolve_documents(user_request="the document", thread_documents=[DOCS[0]], references=["the document"])
    assert r.status == DocumentResolutionStatus.RESOLVED
    assert r.resolution_source == "only_document"


def test_recent_document_used_for_vague_reference():
    r = resolve_documents(
        user_request="the document", thread_documents=DOCS, references=["the document"],
        recent_document_id="d2",
    )
    assert r.status == DocumentResolutionStatus.RESOLVED
    assert r.document_ids == ["d2"]
    assert r.resolution_source == "recent_document"
