"""Phase 46.1 — deterministic document-inventory detection + formatting.

Pure and config-free (no DB, no LLM). Covers the detector's positive/negative
recognition, intent classification, and the safe user-facing formatting.
"""

from app.agent.documents.inventory import format_document_inventory, status_label
from app.agent.interpret import (
    DocumentScope,
    Intent,
    interpret_request,
    is_document_inventory_request,
)

INVENTORY_VARIANTS = [
    "What documents are uploaded?",
    "Which PDFs do I have?",
    "List my uploaded documents.",
    "Show uploaded files.",
    "How many documents are attached?",
    "What files are in this conversation?",
    "Do I have any documents uploaded?",
    "Which documents are available in this thread?",
]

# Content/management requests that must NOT be treated as inventory.
NEGATIVE_VARIANTS = [
    "Summarize resume.pdf",
    "Compare these documents",
    "What does the document say about Python?",
    "Delete all documents",
    "Upload a document",
    "select documents",
    "search inside documents",
    "summarize the report",
    "what is the invoice total",
]


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

def test_all_inventory_variants_detected():
    for text in INVENTORY_VARIANTS:
        assert is_document_inventory_request(text) is True, text


def test_negative_variants_not_detected():
    for text in NEGATIVE_VARIANTS:
        assert is_document_inventory_request(text) is False, text


def test_inventory_classified_with_no_document_scope():
    # Inventory must classify with DOCUMENT_INVENTORY intent and NO document scope,
    # so the scope gate never resolves/retrieves document chunks for it.
    for text in INVENTORY_VARIANTS:
        interp = interpret_request(text)
        assert interp.primary_intent == Intent.DOCUMENT_INVENTORY, text
        assert interp.document_scope == DocumentScope.NONE, text
        assert interp.needs_documents is False, text


def test_ui_selection_overrides_inventory_text():
    # An explicit UI document selection is a deliberate content scope and wins.
    interp = interpret_request("what documents are uploaded?", selected_document_ids=["d1"])
    assert interp.primary_intent != Intent.DOCUMENT_INVENTORY
    assert interp.document_scope == DocumentScope.SINGLE_DOCUMENT


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #

def test_empty_inventory_message():
    text = format_document_inventory([])
    assert "no uploaded documents in this conversation" in text
    assert "Upload a PDF" in text


def test_single_document_listing():
    text = format_document_inventory([{"filename": "resume.pdf", "status": "completed"}])
    assert text.startswith("1 document is available in this conversation:")
    assert "- resume.pdf — Ready" in text


def test_multiple_document_listing_counts_and_lists_each_once():
    docs = [
        {"filename": "resume.pdf", "status": "completed"},
        {"filename": "report.pdf", "status": "processing"},
        {"filename": "invoice.pdf", "status": "failed"},
    ]
    text = format_document_inventory(docs)
    assert text.startswith("3 documents are available in this conversation:")
    assert "- resume.pdf — Ready" in text
    assert "- report.pdf — Indexing" in text
    assert "- invoice.pdf — Failed" in text
    # Each filename appears exactly once.
    assert text.count("resume.pdf") == 1


def test_status_labels_map_to_safe_names():
    assert status_label("completed") == "Ready"
    assert status_label("pending") == "Pending"
    assert status_label("queued") == "Pending"
    assert status_label("processing") == "Indexing"
    assert status_label("indexing") == "Indexing"
    assert status_label("failed") == "Failed"
    assert status_label("error") == "Failed"
    # Unknown status never leaks as a raw token.
    assert status_label("some_new_state") == "Some New State"
    assert status_label(None) == "Unknown"


def test_formatting_exposes_no_internal_ids():
    docs = [{"document_id": "651f...", "filename": "resume.pdf", "status": "completed",
             "object_key": "s3://bucket/key", "chunk_ids": ["c1"]}]
    text = format_document_inventory(docs)
    assert "651f" not in text
    assert "s3://" not in text
    assert "c1" not in text
    assert "E1" not in text
