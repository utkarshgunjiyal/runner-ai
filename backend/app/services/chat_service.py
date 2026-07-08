from app.services.thread_service import (
    create_thread,
    get_thread,
    allocate_next_sequence,
)
from app.services.message_service import save_message
from app.services.memory_retrieval_service import retrieve_memory
from app.services.context_composer import compose_context
from app.services.llm_provider import generate_answer
from app.services.behavior_router import create_request_plan
from app.services.thread_summary_service import (
    create_empty_thread_summary,
    get_thread_summary,
    should_update_thread_summary,
)
from app.services.summary_queue_service import enqueue_thread_summary_job
from app.services.context_policy_service import get_context_policy
from app.config import settings

DEV_USER_ID = "dev_user"


def generate_thread_title(question: str) -> str:
    return question[:60]


async def handle_chat(
    question: str,
    thread_id: str | None,
    document_id: str | None = None,
) -> dict:
    user_id = DEV_USER_ID

    if thread_id:
        thread = await get_thread(
            user_id=user_id,
            thread_id=thread_id,
        )
    else:
        title = generate_thread_title(question)

        thread = await create_thread(
            user_id=user_id,
            title=title,
        )

        await create_empty_thread_summary(
            user_id=user_id,
            thread_id=str(thread["_id"]),
        )

    thread_id = str(thread["_id"])

    user_seq = await allocate_next_sequence(
        user_id=user_id,
        thread_id=thread_id,
    )

    await save_message(
        user_id=user_id,
        thread_id=thread_id,
        seq=user_seq,
        role="user",
        content=question,
    )

    request_plan = create_request_plan(question)
    context_policy = get_context_policy(request_plan)

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

    assistant_seq = await allocate_next_sequence(
        user_id=user_id,
        thread_id=thread_id,
    )

    await save_message(
        user_id=user_id,
        thread_id=thread_id,
        seq=assistant_seq,
        role="assistant",
        content=assistant_answer,
        metadata={
            "llm_provider": settings.llm_provider,
            "llm_model": settings.llm_model,
            "evidence_blocks": len(context.get("evidence", [])),
        },
    )

    should_enqueue_summary = await should_update_thread_summary(
        user_id=user_id,
        thread_id=thread_id,
        latest_seq=assistant_seq,
        threshold=20,
    )

    if should_enqueue_summary:
        summary_doc = await get_thread_summary(
            user_id=user_id,
            thread_id=thread_id,
        )

        from_seq = summary_doc.get("last_summarized_seq", 0) + 1
        to_seq = assistant_seq

        await enqueue_thread_summary_job(
            user_id=user_id,
            thread_id=thread_id,
            from_seq=from_seq,
            to_seq=to_seq,
        )

    return {
        "thread_id": thread_id,
        "answer": assistant_answer,
    }