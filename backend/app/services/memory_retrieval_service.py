from app.services.message_service import get_recent_messages
from app.services.thread_summary_service import get_thread_summary
from app.services import document_service, embedding_service, vector_store_service
from app.schemas.request_plan import RequestPlan
from app.schemas.context_policy import ContextPolicy
from app.schemas.context_evidence import ContextEvidence
from app.schemas.memory_context import MemoryContext
from app.logging_config import get_logger

logger = get_logger("memory_retrieval")


async def retrieve_memory(
    user_id: str,
    thread_id: str,
    question: str,
    request_plan: RequestPlan,
    context_policy: ContextPolicy,
    document_id: str | None = None,
) -> MemoryContext:
    memory = MemoryContext()

    # -- Conversational memory (thread-scoped) -------------------------------
    if context_policy.recent_messages_limit > 0:
        recent_messages = await get_recent_messages(
            user_id=user_id,
            thread_id=thread_id,
            limit=context_policy.recent_messages_limit,
        )

        memory.recent_messages = [
            ContextEvidence(
                source="recent_message",
                header=f"[Recent Message | {msg['role']} | Seq {msg['seq']}]",
                content=msg["content"],
                score=1.0,
                metadata={"seq": msg["seq"], "role": msg["role"]},
            )
            for msg in recent_messages
        ]

    if context_policy.thread_summary:
        summary_doc = await get_thread_summary(
            user_id=user_id,
            thread_id=thread_id,
        )

        if summary_doc and summary_doc.get("summary"):
            memory.thread_summary = [
                ContextEvidence(
                    source="thread_summary",
                    header="[Thread Summary]",
                    content=summary_doc["summary"],
                    score=1.0,
                    metadata={
                        "last_summarized_seq": summary_doc.get(
                            "last_summarized_seq", 0
                        )
                    },
                )
            ]

    # -- Document memory (Phase 2) -------------------------------------------
    # Only document intents pull document evidence; the deterministic router
    # already selects the policy (chunks_top_k / document_summary / page_summary)
    # that drives which of the branches below run.
    if request_plan.intent == "document":
        await _retrieve_document_evidence(
            memory=memory,
            user_id=user_id,
            question=question,
            request_plan=request_plan,
            context_policy=context_policy,
            document_id=document_id,
        )

    return memory


async def _retrieve_document_evidence(
    memory: MemoryContext,
    user_id: str,
    question: str,
    request_plan: RequestPlan,
    context_policy: ContextPolicy,
    document_id: str | None,
) -> None:
    page = request_plan.filters.page

    # Resolve the target document: explicit id wins, else the user's latest
    # completed upload. document_id may stay None -> search spans all the
    # user's documents.
    target_doc = None
    if document_id:
        target_doc = await document_service.get_document(document_id, user_id=user_id)
    elif context_policy.document_summary:
        target_doc = await document_service.get_latest_completed_document(user_id)

    resolved_document_id = document_id
    if resolved_document_id is None and target_doc is not None:
        resolved_document_id = str(target_doc["_id"])

    # 1. Document-level summary (stored on the document record in Phase 1).
    if context_policy.document_summary and target_doc and target_doc.get("summary"):
        memory.document_summary = [
            ContextEvidence(
                source="document_summary",
                header=f"[Document Summary | {target_doc.get('filename', resolved_document_id)}]",
                content=target_doc["summary"],
                score=1.0,
                metadata={"document_id": str(target_doc["_id"])},
            )
        ]

    # 2. Page-scoped retrieval (deterministic: all chunks on the page).
    if context_policy.page_summary and page is not None:
        page_hits = await vector_store_service.list_page_chunks(
            user_id=user_id,
            document_id=resolved_document_id,
            page=page,
        )
        memory.page_summary = [
            ContextEvidence(
                source="page_summary",
                header=f"[Page {hit['page']} | doc {hit['document_id']} | chunk {hit['chunk_index']}]",
                content=hit["text"],
                score=1.0,
                metadata={
                    "page": hit["page"],
                    "document_id": hit["document_id"],
                    "chunk_index": hit["chunk_index"],
                },
            )
            for hit in page_hits
        ]

    # 3. Semantic chunk retrieval (document Q&A / compare / page Q&A).
    if context_policy.chunks_top_k > 0:
        query_vector = (
            await embedding_service.get_embedding_provider().embed([question])
        )[0]

        # For a page-scoped summarize request, keep semantic hits on that page.
        page_filter = page if request_plan.operation == "summarize" else None

        hits = await vector_store_service.search(
            query_vector=query_vector,
            user_id=user_id,
            top_k=context_policy.chunks_top_k,
            document_id=resolved_document_id,
            page=page_filter,
        )
        memory.chunks = [
            ContextEvidence(
                source="document_chunk",
                header=(
                    f"[Chunk | doc {hit['document_id']} | page {hit['page']} "
                    f"| #{hit['chunk_index']} | score {hit['score']:.3f}]"
                ),
                content=hit["text"],
                score=hit["score"] if hit["score"] is not None else 0.0,
                metadata={
                    "page": hit["page"],
                    "document_id": hit["document_id"],
                    "chunk_index": hit["chunk_index"],
                    "similarity_score": hit["score"],
                },
            )
            for hit in hits
        ]

    # 4. Section summaries — TODO (Phase 1 does not yet produce per-section
    #    summaries; leave empty so the composer simply skips this source).
    #    Populate memory.section_summaries here once section summarization
    #    exists in the ingestion pipeline.

    logger.info(
        "document_evidence.retrieved",
        extra={
            "document_id": resolved_document_id,
            "operation": request_plan.operation,
            "page": page,
            "chunks": len(memory.chunks),
            "page_chunks": len(memory.page_summary),
            "has_document_summary": bool(memory.document_summary),
        },
    )
