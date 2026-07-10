"""Internal memory adapter (Phase 13).

Bridges memory capabilities to V1.5 memory services. Both signatures map
cleanly, so real service calls are lazily wired; tests inject fakes.

Capabilities:
- ``memory.get_thread_summary`` — thread_summary_service.get_thread_summary.
- ``memory.get_preferences``    — preference_service.get_preferences.
"""

from app.agent.tools.internal.base import InternalAdapter
from app.agent.tools.result import AdapterResult, ErrorCode


class MemoryAdapter(InternalAdapter):
    name = "memory"

    GET_THREAD_SUMMARY = "memory.get_thread_summary"
    GET_PREFERENCES = "memory.get_preferences"

    def __init__(self, thread_summary_fn=None, preferences_fn=None) -> None:
        self._thread_summary_fn = thread_summary_fn
        self._preferences_fn = preferences_fn

    def _handlers(self):
        return {
            self.GET_THREAD_SUMMARY: self._get_thread_summary,
            self.GET_PREFERENCES: self._get_preferences,
        }

    async def _resolve_thread_summary(self):
        if self._thread_summary_fn is None:
            from app.services.thread_summary_service import get_thread_summary

            self._thread_summary_fn = get_thread_summary
        return self._thread_summary_fn

    async def _resolve_preferences(self):
        if self._preferences_fn is None:
            from app.services.preference_service import get_preferences

            self._preferences_fn = get_preferences
        return self._preferences_fn

    async def _get_thread_summary(self, args: dict) -> AdapterResult:
        thread_id = args.get("thread_id")
        user_id = args.get("user_id")
        if not thread_id or not user_id:
            return AdapterResult.failure(
                ErrorCode.INVALID_ARGS,
                metadata={"missing": "thread_id and user_id are required"},
            )

        fetch = await self._resolve_thread_summary()
        doc = await fetch(user_id=user_id, thread_id=thread_id)
        summary = doc.get("summary", "") if isinstance(doc, dict) else ""
        if not summary:
            # No summary yet is a valid, empty-but-successful state.
            return AdapterResult.ok(output={"summary": ""}, confidence=0.0, partial=True)
        return AdapterResult.ok(output={"summary": summary})

    async def _get_preferences(self, args: dict) -> AdapterResult:
        user_id = args.get("user_id")
        if not user_id:
            return AdapterResult.failure(
                ErrorCode.INVALID_ARGS,
                metadata={"missing": "user_id is required"},
            )

        fetch = await self._resolve_preferences()
        preferences = await fetch(user_id=user_id, limit=args.get("limit", 5)) or []
        return AdapterResult.ok(
            output={"preferences": preferences},
            confidence=1.0 if preferences else 0.0,
            partial=not preferences,
            metadata={"count": len(preferences)},
        )
