"""Read-only, async context providers (Phase 10B).

Each provider returns Phase 10A ``WorkingContextItem`` objects for one slice of
*working context* (conversation continuity + long-term memory). Providers are
read-only: they never write to V1.5 services or the database.

Working context only — no external retrieval here (no document chunks, email,
calendar, or web). Those come later through capabilities.

V1.5 service functions are imported lazily inside each provider so this module
imports without application config, and tests can inject a fake ``fetch``
callable without importing V1.5. See backend/app/agent/ARCHITECTURE.md §6.
"""

from abc import ABC, abstractmethod

from pydantic import BaseModel, ConfigDict

from app.agent.runtime.context import WorkingContextItem


class ContextRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_request: str
    user_id: str
    thread_id: str | None = None


class ContextProvider(ABC):
    name: str = "provider"
    required: bool = False

    @abstractmethod
    async def provide(self, request: ContextRequest) -> list[WorkingContextItem]:
        ...


class RecentMessagesProvider(ContextProvider):
    name = "recent_message"

    def __init__(self, fetch=None, limit: int = 10, required: bool = False) -> None:
        self._fetch = fetch
        self._limit = limit
        self.required = required

    async def _resolve_fetch(self):
        if self._fetch is None:
            from app.services.message_service import get_recent_messages

            self._fetch = get_recent_messages
        return self._fetch

    async def provide(self, request: ContextRequest) -> list[WorkingContextItem]:
        if request.thread_id is None:
            return []
        fetch = await self._resolve_fetch()
        messages = await fetch(
            user_id=request.user_id, thread_id=request.thread_id, limit=self._limit
        )
        return [
            WorkingContextItem(
                source=self.name,
                content=message.get("content", ""),
                metadata={"role": message.get("role"), "seq": message.get("seq")},
            )
            for message in messages
        ]


class ThreadSummaryProvider(ContextProvider):
    name = "thread_summary"

    def __init__(self, fetch=None, required: bool = False) -> None:
        self._fetch = fetch
        self.required = required

    async def _resolve_fetch(self):
        if self._fetch is None:
            from app.services.thread_summary_service import get_thread_summary

            self._fetch = get_thread_summary
        return self._fetch

    async def provide(self, request: ContextRequest) -> list[WorkingContextItem]:
        if request.thread_id is None:
            return []
        fetch = await self._resolve_fetch()
        summary_doc = await fetch(user_id=request.user_id, thread_id=request.thread_id)
        if not summary_doc or not summary_doc.get("summary"):
            return []
        return [
            WorkingContextItem(
                source=self.name,
                content=summary_doc["summary"],
                metadata={"last_summarized_seq": summary_doc.get("last_summarized_seq", 0)},
            )
        ]


class UserPreferencesProvider(ContextProvider):
    name = "user_preference"

    def __init__(self, fetch=None, limit: int = 5, required: bool = False) -> None:
        self._fetch = fetch
        self._limit = limit
        self.required = required

    async def _resolve_fetch(self):
        if self._fetch is None:
            from app.services.preference_service import get_preferences

            self._fetch = get_preferences
        return self._fetch

    async def provide(self, request: ContextRequest) -> list[WorkingContextItem]:
        fetch = await self._resolve_fetch()
        preferences = await fetch(user_id=request.user_id, limit=self._limit)
        return [
            WorkingContextItem(
                source=self.name,
                content=pref.get("text", ""),
                metadata={"preference_id": str(pref.get("_id", ""))},
            )
            for pref in preferences
        ]


class UserKnowledgeProvider(ContextProvider):
    name = "user_knowledge"

    def __init__(self, fetch=None, limit: int = 5, required: bool = False) -> None:
        self._fetch = fetch
        self._limit = limit
        self.required = required

    async def _resolve_fetch(self):
        if self._fetch is None:
            from app.services.knowledge_service import list_knowledge

            self._fetch = list_knowledge
        return self._fetch

    async def provide(self, request: ContextRequest) -> list[WorkingContextItem]:
        fetch = await self._resolve_fetch()
        entries = await fetch(user_id=request.user_id, limit=self._limit)
        return [
            WorkingContextItem(
                source=self.name,
                content=entry.get("text", ""),
                metadata={"knowledge_id": str(entry.get("_id", ""))},
            )
            for entry in entries
        ]
