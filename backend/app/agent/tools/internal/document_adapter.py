"""Internal document adapter (Phase 13).

Bridges document capabilities to V1.5 retrieval/summary services and returns
normalized grounding evidence.

Capabilities:
- ``documents.retrieve_chunks`` — semantic chunk retrieval → evidence.
- ``documents.get_summary``     — stored document-level summary.

Real lazy wiring is left as a TODO here on purpose: retrieval needs an
embedding→vector composition (embedding_service + vector_store_service.search
takes a ``query_vector``, not a raw string), and the document summary is not a
single-getter service yet. The adapter *contract* and fake-callable injection
are complete now; the concrete service wiring lands when those paths exist.
See ARCHITECTURE.md §19-20.
"""

from app.agent.runtime.context import EvidenceItem
from app.agent.tools.internal.base import InternalAdapter
from app.agent.tools.result import AdapterResult, ErrorCode


class DocumentAdapter(InternalAdapter):
    name = "documents"

    RETRIEVE_CHUNKS = "documents.retrieve_chunks"
    GET_SUMMARY = "documents.get_summary"

    def __init__(self, retrieve_fn=None, summary_fn=None) -> None:
        # Inject fakes in tests; production resolvers are TODOs (see below).
        self._retrieve_fn = retrieve_fn
        self._summary_fn = summary_fn

    def _handlers(self):
        return {
            self.RETRIEVE_CHUNKS: self._retrieve_chunks,
            self.GET_SUMMARY: self._get_summary,
        }

    # -- Resolvers (lazy; real wiring deferred) ------------------------------

    async def _resolve_retrieve(self):
        if self._retrieve_fn is None:
            # TODO(execution-bridge): compose embedding_service.embed_query with
            # vector_store_service.search(query_vector=..., user_id=..., top_k=...)
            # for a real query→vector→hits path. Deferred: signatures require an
            # embedding step this shell does not yet own.
            raise NotImplementedError(
                "documents.retrieve_chunks real service wiring not implemented; "
                "inject retrieve_fn"
            )
        return self._retrieve_fn

    async def _resolve_summary(self):
        if self._summary_fn is None:
            # TODO(execution-bridge): wire the stored document summary once a
            # single-getter service exists (document_summary_service currently
            # only *generates* from pages).
            raise NotImplementedError(
                "documents.get_summary real service wiring not implemented; "
                "inject summary_fn"
            )
        return self._summary_fn

    # -- Handlers ------------------------------------------------------------

    async def _retrieve_chunks(self, args: dict) -> AdapterResult:
        query = args.get("query")
        user_id = args.get("user_id")
        if not query or not user_id:
            return AdapterResult.failure(
                ErrorCode.INVALID_ARGS,
                metadata={"missing": "query and user_id are required"},
            )

        retrieve = await self._resolve_retrieve()
        hits = await retrieve(
            query=query,
            user_id=user_id,
            top_k=args.get("top_k", 8),
            document_id=args.get("document_id"),
            page=args.get("page"),
        )
        hits = hits or []

        evidence = [
            EvidenceItem(
                source="document",
                content=hit.get("text", ""),
                score=hit.get("score"),
                metadata={
                    "page": hit.get("page"),
                    "document_id": hit.get("document_id"),
                    "chunk_index": hit.get("chunk_index"),
                },
            )
            for hit in hits
        ]
        top_score = evidence[0].score if evidence and evidence[0].score is not None else None
        confidence = top_score if top_score is not None else (1.0 if evidence else 0.0)
        return AdapterResult.ok(
            output={"hits": hits},
            evidence=evidence,
            confidence=confidence,
            partial=not evidence,
            metadata={"hit_count": len(evidence)},
        )

    async def _get_summary(self, args: dict) -> AdapterResult:
        document_id = args.get("document_id")
        if not document_id:
            return AdapterResult.failure(
                ErrorCode.INVALID_ARGS,
                metadata={"missing": "document_id is required"},
            )

        get_summary = await self._resolve_summary()
        result = await get_summary(document_id=document_id, user_id=args.get("user_id"))
        summary = result.get("summary", "") if isinstance(result, dict) else (result or "")
        if not summary:
            return AdapterResult.failure(
                ErrorCode.NOT_FOUND,
                metadata={"document_id": document_id},
            )
        return AdapterResult.ok(
            output={"summary": summary},
            evidence=[EvidenceItem(source="document_summary", content=summary)],
        )
