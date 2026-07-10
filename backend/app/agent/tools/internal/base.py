"""Internal adapter base for the Execution Bridge (Phase 13).

An internal adapter is a *thin* translator: runtime input (a capability id +
args) → a V1.5 service call → an ``AdapterResult``. It holds no business logic;
V1.5 owns that. The Executor never calls a service directly — it goes through
AdapterToolRunner → AdapterRegistry → an adapter like this one.

``execute`` centralizes capability dispatch and exception→AdapterResult
translation so each concrete handler only maps a successful service payload.

Config-free: V1.5 service functions are imported lazily inside each adapter's
resolver, never at module import time. Tests inject fake callables and never
touch Mongo/Qdrant/Redis or application settings.
"""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from app.agent.models.tool_spec import ToolKind
from app.agent.tools.result import AdapterResult, ErrorCode, classify_exception

Handler = Callable[[dict], Awaitable[AdapterResult]]


class InternalAdapter(ABC):
    """Base for adapters over V1.5 internal services.

    Subclasses declare a capability→handler map via ``_handlers`` and implement
    each handler as ``async (args) -> AdapterResult``. This is the wiring
    foundation; a later phase adapts these into the ToolSpec-based
    ``ToolAdapter``/``AdapterToolRunner`` dispatch path.
    """

    kind: ToolKind = ToolKind.INTERNAL
    name: str = "internal"

    @abstractmethod
    def _handlers(self) -> dict[str, Handler]:
        """Return this adapter's capability id → async handler map."""

    def capabilities(self) -> list[str]:
        return sorted(self._handlers().keys())

    async def execute(self, capability: str, args: dict | None = None) -> AdapterResult:
        handler = self._handlers().get(capability)
        if handler is None:
            return AdapterResult.failure(
                ErrorCode.UNKNOWN_CAPABILITY,
                retryable=False,
                metadata={"adapter": self.name, "capability": capability},
            )
        try:
            return await handler(args or {})
        except Exception as exc:  # noqa: BLE001 — translate faults into AdapterResult
            error_code, retryable = classify_exception(exc)
            return AdapterResult.failure(
                error_code,
                retryable=retryable,
                metadata={
                    "adapter": self.name,
                    "capability": capability,
                    "exception": type(exc).__name__,
                    "detail": str(exc),
                },
            )
