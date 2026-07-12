"""Resource-aware argument pipeline (Phase 46.3.1).

The single object injected into ``DirectRuntime`` as its ``argument_builder``. It
keeps the runtime's existing seam (``build(tool, run_context, default_args) ->
ArgumentBuildResult``) but internally runs the new layered flow:

    provider = provider_of(tool)
    resolved = resolver_registry[provider].resolve(ctx)     # resolve WHAT resources
    result   = builder_registry[provider].build(resolved)   # shape onto the schema

A tool with no registered provider resolver (internal, or an unregistered MCP
server) is a passthrough — the caller's default args are returned unchanged, so
the runtime is byte-identical for everything except registered providers.
"""

from __future__ import annotations

from app.agent.models.tool_spec import ToolSpec
from app.agent.resources.models import ResolutionContext
from app.agent.resources.resolver import (
    ArgumentBuilderRegistry,
    ResourceResolverRegistry,
    provider_of,
)
from app.agent.runtime import diagnostics
from app.agent.runtime.arguments import ArgumentBuildResult


class ResourceAwareArgumentBuilder:
    """Resolve resources, then build arguments — the DirectRuntime seam."""

    def __init__(
        self,
        resolvers: ResourceResolverRegistry,
        builders: ArgumentBuilderRegistry,
    ) -> None:
        self._resolvers = resolvers
        self._builders = builders

    def build(self, tool: ToolSpec, run_context, default_args: dict) -> ArgumentBuildResult:
        provider = provider_of(tool)
        resolver = self._resolvers.for_provider(provider)
        builder = self._builders.for_provider(provider)
        if resolver is None or builder is None:
            # No provider resolution registered → leave the caller's args untouched.
            return ArgumentBuildResult.build_ok(default_args)

        ctx = self._context(provider, tool, run_context)
        diagnostics.resource_resolution_started(run_context, tool, provider=provider)
        resolved = resolver.resolve(ctx)
        diagnostics.resource_resolved(run_context, tool, resolved)

        return builder.build(
            tool, resolved,
            planner_args=ctx.hints.get("capability_args") or {},
            request_text=ctx.user_request,
        )

    def _context(self, provider: str, tool: ToolSpec, run_context) -> ResolutionContext:
        meta = getattr(run_context, "metadata", {}) or {}
        # Read-only, provider-namespaced prior/thread state (formalized in 46.3.2).
        execution_state = dict(meta.get("resource_state") or {})
        hints = {}
        if isinstance(meta.get("capability_args"), dict):
            hints["capability_args"] = meta["capability_args"]
        return ResolutionContext(
            provider=provider,
            capability_id=getattr(tool, "id", ""),
            user_request=getattr(run_context, "user_request", "") or "",
            execution_state=execution_state,
            hints=hints,
        )
