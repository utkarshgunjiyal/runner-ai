"""Context Engine (Phase 10B).

Assembles working context by calling read-only providers and returns a
populated RunContext. Deterministic; no LLM, no external retrieval, no writes.

Optional provider failures are recorded and skipped; a required provider
failure surfaces as ContextEngineError. See backend/app/agent/ARCHITECTURE.md §6.
"""

from app.agent.context.providers import (
    ContextProvider,
    ContextRequest,
    RecentMessagesProvider,
    ThreadSummaryProvider,
    UserKnowledgeProvider,
    UserPreferencesProvider,
)
from app.agent.runtime.context import RunContext, WorkingContextItem
from app.logging_config import get_logger

logger = get_logger("context_engine")


class ContextEngineError(Exception):
    """Raised when a required context provider fails."""


class ContextEngine:
    def __init__(self, providers: list[ContextProvider]) -> None:
        self._providers = list(providers)

    async def build(
        self,
        user_request: str,
        user_id: str,
        thread_id: str | None = None,
        metadata: dict | None = None,
    ) -> RunContext:
        request = ContextRequest(
            user_request=user_request, user_id=user_id, thread_id=thread_id
        )

        items: list[WorkingContextItem] = []
        provider_report: dict[str, dict] = {}

        for provider in self._providers:
            try:
                produced = await provider.provide(request)
            except Exception as exc:  # noqa: BLE001 - optional providers degrade gracefully
                if getattr(provider, "required", False):
                    raise ContextEngineError(
                        f"required context provider '{provider.name}' failed: {exc}"
                    ) from exc
                logger.warning(
                    "context.provider_failed",
                    extra={"provider": provider.name, "error": str(exc)},
                )
                provider_report[provider.name] = {"ok": False, "count": 0, "error": str(exc)}
                continue

            items.extend(produced)
            provider_report[provider.name] = {"ok": True, "count": len(produced)}

        merged_metadata = dict(metadata or {})
        merged_metadata["context_providers"] = provider_report

        return RunContext.create(
            user_request=user_request,
            user_id=user_id,
            thread_id=thread_id,
            working_context=items,
            metadata=merged_metadata,
        )


def default_context_engine() -> ContextEngine:
    """A ContextEngine wired to the four V1.5-backed providers (all optional)."""
    return ContextEngine(
        [
            RecentMessagesProvider(),
            ThreadSummaryProvider(),
            UserPreferencesProvider(),
            UserKnowledgeProvider(),
        ]
    )
