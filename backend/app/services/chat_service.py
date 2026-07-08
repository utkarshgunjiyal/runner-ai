from typing import AsyncIterator

from app.services.thread_service import (
    create_thread,
    get_thread,
    allocate_next_sequence,
)
from app.services.message_service import save_message
from app.services.memory_retrieval_service import retrieve_memory
from app.services.context_composer import compose_context
from app.services.llm_provider import generate_answer, stream_answer
from app.services.behavior_router import create_request_plan
from app.services.thread_summary_service import (
    create_empty_thread_summary,
    get_thread_summary,
    should_update_thread_summary,
)
from app.services.summary_queue_service import enqueue_thread_summary_job
from app.services.context_policy_service import get_context_policy
from app.services import preference_service
from app.config import settings
from app.logging_config import get_logger

logger = get_logger("chat")

DEV_USER_ID = "dev_user"


def generate_thread_title(question: str) -> str:
    return question[:60]


# ---------------------------------------------------------------------------
# Shared pipeline stages (used by both /chat/ask and /chat/stream)
# ---------------------------------------------------------------------------

async def ensure_thread(user_id: str, thread_id: str | None, question: str) -> str:
    if thread_id:
        thread = await get_thread(user_id=user_id, thread_id=thread_id)
    else:
        thread = await create_thread(user_id=user_id, title=generate_thread_title(question))
        await create_empty_thread_summary(user_id=user_id, thread_id=str(thread["_id"]))
    return str(thread["_id"])


async def record_user_message(user_id: str, thread_id: str, question: str) -> int:
    seq = await allocate_next_sequence(user_id=user_id, thread_id=thread_id)
    await save_message(user_id=user_id, thread_id=thread_id, seq=seq, role="user", content=question)
    return seq


async def maybe_save_preference(user_id, question, request_plan, user_seq) -> None:
    # Deterministic preference capture (no HITL) — persist before retrieval so
    # the new preference is available to this turn's context.
    if request_plan.intent == "preference":
        await preference_service.save_preference(
            user_id=user_id, message=question, source_seq=user_seq
        )


def _answer_metadata(context: dict) -> dict:
    return {
        "llm_provider": settings.llm_provider,
        "llm_model": settings.llm_model,
        "evidence_blocks": len(context.get("evidence", [])),
    }


async def persist_answer(user_id: str, thread_id: str, context: dict, answer: str) -> int:
    seq = await allocate_next_sequence(user_id=user_id, thread_id=thread_id)
    await save_message(
        user_id=user_id,
        thread_id=thread_id,
        seq=seq,
        role="assistant",
        content=answer,
        metadata=_answer_metadata(context),
    )
    return seq


async def maybe_enqueue_summary(user_id: str, thread_id: str, assistant_seq: int) -> None:
    if not await should_update_thread_summary(
        user_id=user_id, thread_id=thread_id, latest_seq=assistant_seq, threshold=20
    ):
        return
    summary_doc = await get_thread_summary(user_id=user_id, thread_id=thread_id)
    await enqueue_thread_summary_job(
        user_id=user_id,
        thread_id=thread_id,
        from_seq=summary_doc.get("last_summarized_seq", 0) + 1,
        to_seq=assistant_seq,
    )


# ---------------------------------------------------------------------------
# /chat/ask — non-streaming (behavior preserved)
# ---------------------------------------------------------------------------

async def handle_chat(
    question: str,
    thread_id: str | None,
    document_id: str | None = None,
) -> dict:
    user_id = DEV_USER_ID

    thread_id = await ensure_thread(user_id, thread_id, question)
    user_seq = await record_user_message(user_id, thread_id, question)

    request_plan = create_request_plan(question)
    context_policy = get_context_policy(request_plan)
    await maybe_save_preference(user_id, question, request_plan, user_seq)

    memory = await retrieve_memory(
        user_id=user_id,
        thread_id=thread_id,
        question=question,
        request_plan=request_plan,
        context_policy=context_policy,
        document_id=document_id,
    )
    context = compose_context(
        question=question,
        request_plan=request_plan,
        context_policy=context_policy,
        memory=memory,
    )

    assistant_answer = await generate_answer(context)

    assistant_seq = await persist_answer(user_id, thread_id, context, assistant_answer)
    await maybe_enqueue_summary(user_id, thread_id, assistant_seq)

    return {"thread_id": thread_id, "answer": assistant_answer}


# ---------------------------------------------------------------------------
# /chat/stream — Server-Sent Events (Phase 5)
# ---------------------------------------------------------------------------

def _event(name: str, data: dict | None = None) -> dict:
    return {"event": name, "data": data or {}}


async def stream_chat(
    question: str,
    thread_id: str | None,
    document_id: str | None = None,
) -> AsyncIterator[dict]:
    """Yield pipeline events: status -> tokens -> final -> completed.

    Reuses the same stage helpers as handle_chat so the two endpoints stay in
    lockstep. On error, a terminal 'error' event is yielded instead of raising,
    keeping the SSE stream well-formed.
    """
    user_id = DEV_USER_ID
    yield _event("request_received")

    try:
        thread_id = await ensure_thread(user_id, thread_id, question)
        user_seq = await record_user_message(user_id, thread_id, question)

        yield _event("planning_started")
        request_plan = create_request_plan(question)
        context_policy = get_context_policy(request_plan)
        await maybe_save_preference(user_id, question, request_plan, user_seq)

        yield _event("retrieving_context", {"intent": request_plan.intent, "operation": request_plan.operation})
        memory = await retrieve_memory(
            user_id=user_id,
            thread_id=thread_id,
            question=question,
            request_plan=request_plan,
            context_policy=context_policy,
            document_id=document_id,
        )

        yield _event("building_context")
        context = compose_context(
            question=question,
            request_plan=request_plan,
            context_policy=context_policy,
            memory=memory,
        )

        yield _event("generating_answer", {"evidence_blocks": len(context.get("evidence", []))})
        parts: list[str] = []
        async for token in stream_answer(context):
            parts.append(token)
            yield _event("token", {"text": token})
        answer = "".join(parts)

        yield _event("saving_response")
        assistant_seq = await persist_answer(user_id, thread_id, context, answer)
        await maybe_enqueue_summary(user_id, thread_id, assistant_seq)

        metadata = {**_answer_metadata(context), "assistant_seq": assistant_seq}
        yield _event("final", {"thread_id": thread_id, "answer": answer, "metadata": metadata})
        yield _event("completed")

    except Exception as exc:  # noqa: BLE001 - surface as a terminal SSE event
        logger.exception("chat.stream_failed")
        yield _event("error", {"message": str(exc)})
