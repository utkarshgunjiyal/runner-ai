"""Resource-resolution + argument-building contracts and registries (Phase 46.3.1).

Two provider-agnostic seams and their registries:

- ``ResourceResolver`` — resolves a provider's resources deterministically from a
  ``ResolutionContext`` (no LLM, no arbitrary state).
- ``ProviderArgumentBuilder`` — shapes ALREADY-RESOLVED resources onto the selected
  tool's discovered ``input_schema``. It consumes ``ResolvedResources`` and never
  parses owners/ids/names itself.

Providers register one of each keyed by their provider id (the MCP ``server_id``,
e.g. ``github``). A tool with no registered resolver is left untouched, so internal
and unregistered capabilities are byte-identical.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.agent.models.tool_spec import ToolSpec
from app.agent.resources.models import ResolutionContext, ResolvedResources
from app.agent.runtime.arguments import ArgumentBuildResult


def provider_of(tool: ToolSpec) -> str | None:
    """The provider id for a tool = its MCP ``server_id`` (``mcp:<server>:<tool>``).

    Internal / non-MCP tools have no ``mcp:`` handler_ref → ``None`` (passthrough).
    """
    ref = getattr(tool, "handler_ref", None)
    if isinstance(ref, str) and ref.startswith("mcp:"):
        parts = ref.split(":", 2)
        if len(parts) == 3 and parts[1]:
            return parts[1]
    return None


@runtime_checkable
class ResourceResolver(Protocol):
    provider: str

    def resolve(self, ctx: ResolutionContext) -> ResolvedResources:
        ...


@runtime_checkable
class ProviderArgumentBuilder(Protocol):
    provider: str

    def build(
        self, tool: ToolSpec, resolved: ResolvedResources, *, planner_args: dict, request_text: str,
    ) -> ArgumentBuildResult:
        ...


class ResourceResolverRegistry:
    """Provider id → ``ResourceResolver``."""

    def __init__(self) -> None:
        self._by_provider: dict[str, ResourceResolver] = {}

    def register(self, resolver: ResourceResolver) -> None:
        self._by_provider[resolver.provider] = resolver

    def for_provider(self, provider: str | None) -> ResourceResolver | None:
        return self._by_provider.get(provider) if provider else None

    def providers(self) -> list[str]:
        return sorted(self._by_provider)


class ArgumentBuilderRegistry:
    """Provider id → ``ProviderArgumentBuilder``."""

    def __init__(self) -> None:
        self._by_provider: dict[str, ProviderArgumentBuilder] = {}

    def register(self, builder: ProviderArgumentBuilder) -> None:
        self._by_provider[builder.provider] = builder

    def for_provider(self, provider: str | None) -> ProviderArgumentBuilder | None:
        return self._by_provider.get(provider) if provider else None

    def providers(self) -> list[str]:
        return sorted(self._by_provider)
