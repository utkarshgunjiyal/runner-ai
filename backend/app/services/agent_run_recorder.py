"""MongoRunRecorder (Phase 43) — the V1.5-backed RunRecorder.

Validates thread ownership and persists user/assistant messages + safe run
metadata around a V2 agent run. Installed at the composition root
(``configure_run_recorder``); the agent routes never import this module directly,
so they stay config-free and unit-testable.

Persistence never breaks a run: a waiting run keeps the user message and its
checkpoint (no fabricated assistant answer); a failed run persists only a safe
short note, never a raw stack trace.
"""

from fastapi import HTTPException

from app.agent.persistence import RunOutcomeView, ThreadOwnershipError
from app.logging_config import get_logger
from app.services import message_service, thread_service

logger = get_logger("agent_run_recorder")

_FAILED_NOTE = "The run could not be completed."


class MongoRunRecorder:
    async def before_run(self, user_id: str, thread_id: str | None, user_request: str) -> str:
        # Create a thread on first turn; otherwise verify ownership (404 → error).
        if not thread_id:
            title = (user_request or "New conversation").strip()[:60] or "New conversation"
            thread = await thread_service.create_thread(user_id, title)
            thread_id = str(thread["_id"])
        else:
            try:
                await thread_service.get_thread(user_id, thread_id)
            except HTTPException as exc:
                raise ThreadOwnershipError(str(exc.detail)) from exc

        seq = await thread_service.allocate_next_sequence(user_id, thread_id)
        await message_service.save_message(
            user_id=user_id, thread_id=thread_id, seq=seq, role="user", content=user_request,
        )
        return thread_id

    async def after_run(self, user_id: str, thread_id: str | None, outcome: RunOutcomeView) -> None:
        if not thread_id:
            return

        content: str | None = None
        if outcome.answer_text:
            content = outcome.answer_text
        elif outcome.is_failed:
            content = _FAILED_NOTE
        # Waiting runs: no assistant message (the checkpoint represents the pause);
        # the user message + activity bump are enough.

        if content is not None:
            seq = await thread_service.allocate_next_sequence(user_id, thread_id)
            await message_service.save_message(
                user_id=user_id, thread_id=thread_id, seq=seq, role="assistant",
                content=content,
                metadata={
                    "run_id": outcome.run_id,
                    "runtime_outcome": outcome.runtime_outcome,
                    "resolved_document_ids": outcome.resolved_document_ids,
                },
            )
        else:
            await thread_service.touch_thread(user_id, thread_id)
        logger.info(
            "agent.run_recorded",
            extra={"runtime_outcome": outcome.runtime_outcome, "persisted_assistant": content is not None},
        )
