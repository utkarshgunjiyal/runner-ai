"""In-memory, deterministic Tool Registry.

Source of truth for all ToolSpecs. Phase 1: registration + lookup + filtering.
No retrieval ranking (Capability Retrieval Engine) and no execution here.
See docs/architecture/v2.md §5.
"""

from collections.abc import Iterable

from app.agent.models.tool_spec import RiskLevel, ToolKind, ToolSpec


class ToolRegistryError(Exception):
    """Base error for tool registry operations."""


class DuplicateToolError(ToolRegistryError):
    """Raised when registering a tool id that already exists."""


class ToolNotFoundError(ToolRegistryError):
    """Raised when looking up a tool id that is not registered."""


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, tool: ToolSpec) -> None:
        if tool.id in self._tools:
            raise DuplicateToolError(f"Tool id already registered: '{tool.id}'")
        self._tools[tool.id] = tool

    def unregister(self, tool_id: str) -> None:
        """Remove a tool id if present (idempotent).

        Additive to Phase 1: used by dynamic sources (e.g. MCP discovery refresh)
        that replace a subset of registrations. Removing an id that was never
        registered is a no-op, never an error.
        """
        self._tools.pop(tool_id, None)

    def get(self, tool_id: str) -> ToolSpec:
        try:
            return self._tools[tool_id]
        except KeyError:
            raise ToolNotFoundError(f"Unknown tool id: '{tool_id}'") from None

    def exists(self, tool_id: str) -> bool:
        return tool_id in self._tools

    @staticmethod
    def _sorted(tools: Iterable[ToolSpec]) -> list[ToolSpec]:
        # Deterministic ordering by tool id for stable, reproducible output.
        return sorted(tools, key=lambda tool: tool.id)

    def list_all(self) -> list[ToolSpec]:
        return self._sorted(self._tools.values())

    def list_enabled(self) -> list[ToolSpec]:
        return self._sorted(t for t in self._tools.values() if t.enabled)

    def filter_by_kind(self, kind: ToolKind) -> list[ToolSpec]:
        return self._sorted(t for t in self._tools.values() if t.kind == kind)

    def filter_by_risk(self, risk_level: RiskLevel) -> list[ToolSpec]:
        return self._sorted(
            t for t in self._tools.values() if t.risk_level == risk_level
        )

    def filter_by_tag(self, tag: str) -> list[ToolSpec]:
        return self._sorted(t for t in self._tools.values() if tag in t.tags)
