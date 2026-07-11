"""Deterministic document resolver (Phase 43).

Resolves a user's document reference to stable, OWNED document ids. Ownership is
authoritative: the caller passes ``thread_documents`` — the documents that belong
to (user_id, thread_id) as read from Mongo — and the resolver never resolves to
anything outside that set. Filenames are used only for matching/display; stable
document ids are used for retrieval.

Resolution priority (deterministic evidence first, LLM never used here):
  1. UI-selected document ids (validated ⊆ owned set)
  2. exact filename match (unique)
  3. unique normalized-filename match
  4. unique partial / title match
  5. a single owned document, or the recently-referenced document
  6. the last-uploaded document (only when the reference is a bare "all/none")
  7. otherwise → ambiguous (safe candidate list, no retrieval performed)

Pure and config-free: no database, no settings, no LLM.
"""

from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

# Vague references that name *a* document without identifying which one.
_VAGUE_REFERENCES = (
    "this document", "that document", "the document", "this file", "that file",
    "the file", "this pdf", "the pdf", "this report", "the report", "the doc",
    "this doc", "the attachment", "attached", "uploaded",
)


class DocumentResolutionStatus(str, Enum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"
    UNAUTHORIZED = "unauthorized"
    NO_DOCUMENTS = "no_documents"


class DocumentCandidate(BaseModel):
    """A SAFE candidate for a document picker — never any document content."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    filename: str
    created_at: str | None = None


class DocumentResolution(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: DocumentResolutionStatus
    document_ids: list[str] = Field(default_factory=list)
    resolution_source: str = ""
    candidates: list[DocumentCandidate] = Field(default_factory=list)
    clarification_prompt: str | None = None

    @property
    def resolved(self) -> bool:
        return self.status == DocumentResolutionStatus.RESOLVED


def _normalize(name: str) -> str:
    stem = re.sub(r"\.[A-Za-z0-9]{1,6}$", "", name or "")  # strip extension
    return re.sub(r"[^a-z0-9]+", " ", stem.lower()).strip()


def _as_candidate(doc: dict) -> DocumentCandidate:
    created = doc.get("created_at")
    return DocumentCandidate(
        document_id=str(doc.get("document_id") or doc.get("_id") or ""),
        filename=str(doc.get("filename") or "document"),
        created_at=str(created) if created is not None else None,
    )


def _doc_id(doc: dict) -> str:
    return str(doc.get("document_id") or doc.get("_id") or "")


def resolve_documents(
    *,
    user_request: str,
    thread_documents: list[dict],
    references: list[str] | None = None,
    selected_document_ids: list[str] | None = None,
    recent_document_id: str | None = None,
    wants_all: bool = False,
) -> DocumentResolution:
    """Resolve a document reference against the OWNED thread document set."""
    owned = [d for d in thread_documents or [] if _doc_id(d)]
    owned_ids = {_doc_id(d): d for d in owned}
    references = [r for r in (references or []) if r and r.strip()]
    selected = [s for s in (selected_document_ids or []) if s]

    if not owned:
        return DocumentResolution(
            status=DocumentResolutionStatus.NO_DOCUMENTS,
            clarification_prompt="There are no documents in this conversation yet. Upload one to ask about it.",
        )

    candidates = [_as_candidate(d) for d in owned]

    # 1. UI selection — validated against the owned set (never trusted blindly).
    if selected:
        unknown = [s for s in selected if s not in owned_ids]
        if unknown:
            return DocumentResolution(
                status=DocumentResolutionStatus.UNAUTHORIZED,
                resolution_source="ui_selection",
                clarification_prompt="One or more selected documents are not part of this conversation.",
            )
        return DocumentResolution(
            status=DocumentResolutionStatus.RESOLVED,
            document_ids=list(dict.fromkeys(selected)),
            resolution_source="ui_selection",
        )

    # 2. Explicit "all documents in this thread".
    if wants_all:
        return DocumentResolution(
            status=DocumentResolutionStatus.RESOLVED,
            document_ids=[_doc_id(d) for d in owned],
            resolution_source="all_thread_documents",
        )

    named = [r for r in references if r.lower() not in _VAGUE_REFERENCES]
    vague = [r for r in references if r.lower() in _VAGUE_REFERENCES]

    # 3–4. Named reference → filename matching.
    if named:
        for ref in named:
            exact = [d for d in owned if str(d.get("filename", "")).lower() == ref.lower()]
            if len(exact) == 1:
                return DocumentResolution(
                    status=DocumentResolutionStatus.RESOLVED,
                    document_ids=[_doc_id(exact[0])], resolution_source="exact_filename",
                )
            if len(exact) > 1:
                return _ambiguous([_as_candidate(d) for d in exact], ref)

            nref = _normalize(ref)
            norm = [d for d in owned if _normalize(str(d.get("filename", ""))) == nref]
            if len(norm) == 1:
                return DocumentResolution(
                    status=DocumentResolutionStatus.RESOLVED,
                    document_ids=[_doc_id(norm[0])], resolution_source="normalized_filename",
                )
            if len(norm) > 1:
                return _ambiguous([_as_candidate(d) for d in norm], ref)

            partial = [
                d for d in owned
                if nref and nref in _normalize(str(d.get("filename", "")))
            ]
            if len(partial) == 1:
                return DocumentResolution(
                    status=DocumentResolutionStatus.RESOLVED,
                    document_ids=[_doc_id(partial[0])], resolution_source="partial_filename",
                )
            if len(partial) > 1:
                return _ambiguous([_as_candidate(d) for d in partial], ref)
        # A specific document was named but no owned document matches it.
        return DocumentResolution(
            status=DocumentResolutionStatus.NOT_FOUND,
            resolution_source="named_reference",
            clarification_prompt=f"No document matching {named[0]!r} is in this conversation.",
        )

    # 5. Vague reference (or a bare document intent) → single doc / recent / ambiguous.
    if len(owned) == 1:
        return DocumentResolution(
            status=DocumentResolutionStatus.RESOLVED,
            document_ids=[_doc_id(owned[0])], resolution_source="only_document",
        )
    if recent_document_id and recent_document_id in owned_ids:
        return DocumentResolution(
            status=DocumentResolutionStatus.RESOLVED,
            document_ids=[recent_document_id], resolution_source="recent_document",
        )
    # 7. Ambiguous — hand the safe candidate list to a picker; no retrieval yet.
    ref_label = vague[0] if vague else "the document"
    return _ambiguous(candidates, ref_label)


def _ambiguous(candidates: list[DocumentCandidate], reference: str) -> DocumentResolution:
    return DocumentResolution(
        status=DocumentResolutionStatus.AMBIGUOUS,
        candidates=candidates,
        resolution_source="ambiguous",
        clarification_prompt=(
            f"Which document do you mean by {reference!r}? "
            f"There are {len(candidates)} in this conversation — please pick one or more."
        ),
    )
