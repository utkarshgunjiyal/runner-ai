"""Capability sources (Phase 40).

A ``CapabilitySource`` is a first-class provider of capabilities. Every source —
internal Python, MCP, or a future kind — describes itself uniformly for both
halves of the platform:

- **Retrieval**: ``load()`` / ``snapshot()`` produce the source's ``ToolSpec``s,
  which the Unified Capability Registry registers into one shared registry the
  existing hybrid retrieval reads. Nothing downstream knows the origin.
- **Execution**: ``tool_kind`` + ``build_executor()`` provide the adapter that
  runs this source's tools, which the factory routes to via the by-kind
  ``CompositeCapabilityExecutor``.

A source owns a ``namespace``. Strict-namespace sources (MCP, future) must emit
ids under ``<namespace>.``; the internal source is the one legacy flat namespace
(its historical ids — ``search_documents`` etc. — are stable and unprefixed), so
it declares ``strict_namespace = False``. Namespace isolation across sources is
enforced by the Unified Capability Registry via ownership + prefix checks.

Config-free at import: MCP is referenced only through the already-config-free
manager; V1.5 is reached lazily inside the internal adapters at execution time.
"""

from abc import ABC, abstractmethod

from app.agent.models.tool_spec import ToolKind, ToolSpec
from app.agent.tools.internal.specs import internal_tool_specs


class CapabilitySource(ABC):
    """A provider of capabilities (specs + an executor), owning a namespace."""

    #: Stable unique id for this source (e.g. "internal", "mcp").
    source_id: str
    #: The namespace this source owns (e.g. "internal", "mcp", "future").
    namespace: str
    #: The ToolKind every spec from this source uses (routes execution).
    tool_kind: ToolKind
    #: Whether every produced id must be under ``<namespace>.`` (legacy flat
    #: sources set False).
    strict_namespace: bool = True

    def snapshot(self) -> list[ToolSpec]:
        """Currently-known specs with no I/O. Defaults to ``_specs()``."""
        return list(self._specs())

    async def load(self) -> list[ToolSpec]:
        """Ensure specs are current (may do discovery), then return them."""
        return self.snapshot()

    async def reload(self) -> list[ToolSpec]:
        """Force a fresh reload (used by refresh). Defaults to ``load()``."""
        return await self.load()

    async def close(self) -> None:
        """Release any resources (sessions/clients). Default: no-op."""
        return None

    @abstractmethod
    def _specs(self) -> list[ToolSpec]:
        """Return the source's current specs (no I/O)."""

    @abstractmethod
    def build_executor(self):
        """Return the CapabilityExecutor that runs this source's tools."""


class InternalCapabilitySource(CapabilitySource):
    """Internal Python capabilities backed by V1.5 services.

    Namespace ``internal``; ids are the historical, stable, flat ids
    (``search_documents`` …), so ``strict_namespace`` is False.
    """

    source_id = "internal"
    namespace = "internal"
    tool_kind = ToolKind.INTERNAL
    strict_namespace = False

    def __init__(self, *, executor=None) -> None:
        # Cache specs once (fresh, deterministic instances).
        self._cached = internal_tool_specs()
        self._executor = executor

    def _specs(self) -> list[ToolSpec]:
        return list(self._cached)

    def build_executor(self):
        if self._executor is not None:
            return self._executor
        from app.agent.execution.capability_executor import InternalCapabilityExecutor

        self._executor = InternalCapabilityExecutor()
        return self._executor


class MCPCapabilitySource(CapabilitySource):
    """MCP capabilities discovered through an ``MCPRegistryManager``.

    Namespace ``mcp``; ids are ``mcp.<server_id>.<tool_name>`` (strict). The
    manager owns discovery, bindings, and connection lifecycle; this source
    adapts it to the unified platform contract. Execution routes to ``MCPAdapter``.
    """

    namespace = "mcp"
    tool_kind = ToolKind.MCP
    strict_namespace = True

    def __init__(self, manager, *, source_id: str = "mcp", result_normalizers=None) -> None:
        self.source_id = source_id
        self._manager = manager
        self._executor = None
        # Optional per-server result normalizers passed to the MCPAdapter
        # (Phase 46.2). Default None → unchanged generic MCP result handling.
        self._result_normalizers = result_normalizers

    @property
    def manager(self):
        return self._manager

    def _specs(self) -> list[ToolSpec]:
        # Already-discovered specs, no I/O (the composition root discovers first).
        registry = self._manager.tool_registry
        return [registry.get(cid) for cid in self._manager.list_discovered_tools()]

    async def load(self) -> list[ToolSpec]:
        # Ensure every registered server is discovered (idempotent).
        for server in self._manager.list_servers():
            await self._manager.discover_server_tools(server["server_id"])
        return self.snapshot()

    async def reload(self) -> list[ToolSpec]:
        # Force a fresh re-discovery of every server (atomic per server).
        for server in self._manager.list_servers():
            await self._manager.refresh_server_tools(server["server_id"])
        return self.snapshot()

    async def close(self) -> None:
        await self._manager.close()

    def build_executor(self):
        if self._executor is not None:
            return self._executor
        from app.agent.tools.mcp_adapter import MCPAdapter

        self._executor = MCPAdapter(self._manager, result_normalizers=self._result_normalizers)
        return self._executor
