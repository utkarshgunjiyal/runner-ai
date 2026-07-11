"""Scope Gate (Phase 43) — the runtime's early document-scope decision.

Given a RunContext, it interprets the request, resolves the referenced documents
against the OWNED thread document set, and either:
  - asks the orchestrator to CLARIFY (ambiguous / unauthorized reference) → a
    genuine WAITING_FOR_USER with a safe candidate list, or
  - PROCEEDs, having attached the resolved documents' chunk evidence to the
    RunContext so the final answer is grounded in exactly the resolved scope.

Config-free: all V1.5 access is via injected callables (thread documents, the
scoped retriever, the recent-document lookup). Default runtime has no scope gate
(byte-identical). Ownership is enforced here (the resolver validates the set);
the client's selected ids are only hints, revalidated every time.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.agent.documents.resolver import DocumentResolutionStatus, resolve_documents
from app.agent.interpret import interpret_request
from app.agent.interpret.capability_gate import disallowed_capability_ids
from app.agent.runtime.context import EvidenceItem, RunContext
from app.logging_config import get_logger

logger = get_logger("scope_gate")

# The pending action a document-ambiguity pause carries (drives the UI picker).
SELECT_DOCUMENT_ACTION = "select_document"


class ScopeDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: str  # "proceed" | "clarify"
    pending_reason: str | None = None
    candidates: list[dict] = Field(default_factory=list)
    resolved_document_ids: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


def _selected_from_resume(run_context: RunContext) -> list[str]:
    """Extract user-selected document ids from a resume resolution (safe parse)."""
    resume = run_context.metadata.get("resume") or {}
    value = resume.get("value")
    if isinstance(value, list):
        return [str(v) for v in value if v]
    if isinstance(value, dict):
        ids = value.get("document_ids") or value.get("selected_document_ids")
        if isinstance(ids, list):
            return [str(v) for v in ids if v]
    if isinstance(value, str) and value.strip():
        # A single id (not free text) — the picker sends ids, not prose.
        return [value.strip()]
    meta = resume.get("metadata") or {}
    ids = meta.get("selected_document_ids")
    if isinstance(ids, list):
        return [str(v) for v in ids if v]
    return []


class ScopeGate:
    def __init__(
        self,
        *,
        thread_documents_fn,
        document_retriever_fn,
        recent_document_fn=None,
        connectors_fn=None,
        top_k: int = 8,
    ) -> None:
        self._thread_documents_fn = thread_documents_fn
        self._document_retriever_fn = document_retriever_fn
        self._recent_document_fn = recent_document_fn
        # Optional: fetch the user's connectors so the eligibility retriever can
        # drop unavailable capabilities. Stored as SAFE public views only.
        self._connectors_fn = connectors_fn
        self._top_k = top_k

    async def evaluate(self, run_context: RunContext, *, is_resume: bool = False) -> ScopeDecision:
        user_id = run_context.user_id
        thread_id = run_context.thread_id
        meta = run_context.metadata

        # Populate the per-run connector snapshot (safe public views) up front so
        # capability eligibility can be applied during retrieval.
        if self._connectors_fn is not None and "connectors" not in meta:
            connectors = await self._connectors_fn(user_id) or []
            meta["connectors"] = [
                c.public_view() if hasattr(c, "public_view") else dict(c) for c in connectors
            ]

        selected = list(meta.get("selected_document_ids") or [])
        if is_resume:
            resumed = _selected_from_resume(run_context)
            if resumed:
                selected = resumed
        pages = [int(p) for p in (meta.get("selected_page_numbers") or []) if str(p).isdigit()]
        explicit_mode = meta.get("explicit_context_mode")

        thread_documents: list[dict] = []
        if thread_id:
            thread_documents = list(await self._thread_documents_fn(user_id, thread_id) or [])

        interpretation = interpret_request(
            run_context.user_request,
            selected_document_ids=selected,
            page_numbers=pages,
            has_thread_documents=bool(thread_documents),
            explicit_context_mode=explicit_mode,
        )
        meta["interpretation"] = interpretation.safe_summary()
        # Intent-based capability gating (Phase 44): keep page/preference tools out
        # of the planner's candidate set unless the intent is explicit.
        meta["excluded_capability_ids"] = sorted(disallowed_capability_ids(interpretation))

        # Not a document-dependent request → nothing for this gate to do.
        if not interpretation.needs_documents and not selected:
            return ScopeDecision(action="proceed", metadata={"document_scope": "none"})

        # A document is wanted but the request is not in a thread → cannot scope
        # to owned documents; proceed without document evidence (safe).
        if not thread_id or not thread_documents:
            meta["document_scope"] = {"status": "no_thread_documents", "resolved": 0}
            return ScopeDecision(action="proceed", metadata={"document_scope": "no_thread_documents"})

        recent_document_id = None
        if self._recent_document_fn is not None:
            recent_document_id = await self._recent_document_fn(user_id, thread_id)

        wants_all = interpretation.document_scope.value == "all_thread_documents"
        resolution = resolve_documents(
            user_request=run_context.user_request,
            thread_documents=thread_documents,
            references=interpretation.raw_document_references,
            selected_document_ids=selected,
            recent_document_id=recent_document_id,
            wants_all=wants_all,
        )

        if resolution.status in (DocumentResolutionStatus.AMBIGUOUS, DocumentResolutionStatus.UNAUTHORIZED):
            candidates = [c.model_dump() for c in resolution.candidates]
            meta["document_scope"] = {
                "status": resolution.status.value,
                "candidate_count": len(candidates),
            }
            logger.info(
                "scope_gate.clarify",
                extra={
                    "thread_id": bool(thread_id),
                    "status": resolution.status.value,
                    "candidate_count": len(candidates),
                },
            )
            return ScopeDecision(
                action="clarify",
                pending_reason=resolution.clarification_prompt,
                candidates=candidates,
                metadata={"document_scope": resolution.status.value},
            )

        # RESOLVED (or NOT_FOUND / NO_DOCUMENTS → proceed without evidence).
        resolved_ids = list(resolution.document_ids)
        evidence_count = 0
        if resolved_ids:
            pages = interpretation.page_numbers or None
            if len(resolved_ids) > 1:
                # Comparison / multi-document: balanced per-document retrieval so
                # every selected document contributes and none dominates.
                from app.agent.documents import balanced_per_document_retrieve

                hits = await balanced_per_document_retrieve(
                    retriever_fn=self._document_retriever_fn,
                    query=run_context.user_request,
                    user_id=user_id,
                    document_ids=resolved_ids,
                    pages=pages,
                )
            else:
                hits = await self._document_retriever_fn(
                    query=run_context.user_request,
                    user_id=user_id,
                    document_ids=resolved_ids,
                    pages=pages,
                    top_k=self._top_k,
                )
            # Lexical (BM25) reranking so query terms lift the relevant chunks
            # over the non-semantic hash-stub dense ordering (Phase 44).
            from app.agent.retriever.lexical import rerank_hits

            hits = rerank_hits(run_context.user_request, hits or [])
            by_id = {str(d.get("document_id") or d.get("_id")): d for d in thread_documents}
            for hit in hits or []:
                doc_id = str(hit.get("document_id", ""))
                filename = str((by_id.get(doc_id) or {}).get("filename", "document"))
                run_context.append_evidence(
                    EvidenceItem(
                        source=f"document:{filename}",
                        content=hit.get("text", ""),
                        score=hit.get("score"),
                        metadata={
                            "document_id": doc_id,
                            "filename": filename,
                            "page": hit.get("page"),
                            "source_type": "document",
                        },
                    )
                )
                evidence_count += 1

        # Resolved documents (id + filename), in the resolved order — so the final
        # synthesis layer covers EVERY selected document, including ones that
        # produced no evidence (Phase 44.1 balanced comparison).
        by_id = {str(d.get("document_id") or d.get("_id")): d for d in thread_documents}
        resolved_documents = [
            {"document_id": doc_id, "filename": str((by_id.get(doc_id) or {}).get("filename", "document"))}
            for doc_id in resolved_ids
        ]
        meta["document_scope"] = {
            "status": resolution.status.value,
            "resolved_document_ids": resolved_ids,
            "documents": resolved_documents,
            "resolution_source": resolution.resolution_source,
            "evidence_count": evidence_count,
        }
        logger.info(
            "scope_gate.resolved",
            extra={
                "resolution_source": resolution.resolution_source,
                "resolved_count": len(resolved_ids),
                "evidence_count": evidence_count,
            },
        )
        return ScopeDecision(
            action="proceed",
            resolved_document_ids=resolved_ids,
            metadata={"document_scope": resolution.status.value},
        )
