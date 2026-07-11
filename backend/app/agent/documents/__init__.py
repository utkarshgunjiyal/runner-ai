"""Document scoping for the V2 runtime (Phase 43).

- ``resolver`` — deterministic, ownership-validating resolution of a natural-
  language document reference to stable document ids (or an ambiguity that the
  runtime turns into a genuine WAITING_FOR_USER clarification).
- ``retrieval`` — the real scoped retrieval seam (embedding + vector store) that
  finally wires document chunk retrieval into the runtime, filtered by user and
  the thread's validated document set.

Config-free at import: V1.5 services are imported lazily inside the retrieval
factory. The resolver is pure (owned document metadata is passed in).
"""

from app.agent.documents.resolver import (
    DocumentCandidate,
    DocumentResolution,
    DocumentResolutionStatus,
    resolve_documents,
)
from app.agent.documents.inventory import format_document_inventory, status_label
from app.agent.documents.retrieval import (
    PER_DOCUMENT_CHUNK_QUOTA,
    balanced_per_document_retrieve,
    build_scoped_document_retriever,
)

__all__ = [
    "DocumentCandidate",
    "DocumentResolution",
    "DocumentResolutionStatus",
    "resolve_documents",
    "build_scoped_document_retriever",
    "balanced_per_document_retrieve",
    "PER_DOCUMENT_CHUNK_QUOTA",
    "format_document_inventory",
    "status_label",
]
