"""Adapter registry — maps a ToolKind to the adapter that executes it.

Deterministic and side-effect-free. Phase 8 introduces the abstraction only; it
is not yet wired into PlanExecutor.
"""

from app.agent.models.tool_spec import ToolKind
from app.agent.tools.adapter import ToolAdapter


class AdapterRegistryError(Exception):
    """Base error for adapter registry operations."""


class DuplicateAdapterError(AdapterRegistryError):
    """Raised when registering a ToolKind that already has an adapter."""


class AdapterNotFoundError(AdapterRegistryError):
    """Raised when looking up a ToolKind that has no registered adapter."""


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[ToolKind, ToolAdapter] = {}

    def register(self, kind: ToolKind, adapter: ToolAdapter) -> None:
        if kind in self._adapters:
            raise DuplicateAdapterError(f"Adapter already registered for kind: {kind.value}")
        self._adapters[kind] = adapter

    def get(self, kind: ToolKind) -> ToolAdapter:
        try:
            return self._adapters[kind]
        except KeyError:
            raise AdapterNotFoundError(f"No adapter registered for kind: {kind.value}") from None

    def exists(self, kind: ToolKind) -> bool:
        return kind in self._adapters

    def list_kinds(self) -> list[ToolKind]:
        return sorted(self._adapters.keys(), key=lambda kind: kind.value)
