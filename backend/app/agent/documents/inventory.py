"""Deterministic document-inventory formatting (Phase 46.1).

Answers "what documents are uploaded?" by listing the ACTIVE thread's own
document records — never document-content retrieval, embeddings, chunk search, or
an LLM. Pure and config-free: it takes already-fetched, ownership-scoped document
records (the same records the UI selector shows) and renders a safe, structured,
user-facing listing.

Safety: filename + a friendly status label only. Never a document UUID, storage
key, chunk id, evidence id (E#), or any raw repository object.
"""

from __future__ import annotations

# Internal repository status → clear, user-facing label. Unknown statuses fall
# back to a title-cased form so a new backend status never leaks as a raw token.
_STATUS_LABELS = {
    "completed": "Ready",
    "ready": "Ready",
    "indexed": "Ready",
    "pending": "Pending",
    "queued": "Pending",
    "uploaded": "Pending",
    "processing": "Indexing",
    "indexing": "Indexing",
    "extracting": "Indexing",
    "embedding": "Indexing",
    "failed": "Failed",
    "error": "Failed",
}

_EMPTY_MESSAGE = (
    "There are currently no uploaded documents in this conversation.\n\n"
    "Upload a PDF to ask questions about it."
)


def status_label(status: str | None) -> str:
    """Map an internal document status to a safe, user-facing label."""
    key = str(status or "").strip().lower()
    if key in _STATUS_LABELS:
        return _STATUS_LABELS[key]
    return key.replace("_", " ").title() if key else "Unknown"


def format_document_inventory(documents: list[dict]) -> str:
    """Render a deterministic inventory listing from thread document records.

    ``documents`` is an ordered list of dicts with at least ``filename`` and
    ``status`` (the safe fields already exposed to the UI). Returns the exact
    user-facing text — filename + status label only, one per line."""
    items = list(documents or [])
    if not items:
        return _EMPTY_MESSAGE

    count = len(items)
    noun = "document" if count == 1 else "documents"
    lines = [f"{count} {noun} {'is' if count == 1 else 'are'} available in this conversation:", ""]
    for doc in items:
        filename = str(doc.get("filename") or "document")
        lines.append(f"- {filename} — {status_label(doc.get('status'))}")
    return "\n".join(lines)
