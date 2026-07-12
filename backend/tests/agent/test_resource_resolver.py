"""Phase 46.3.1 — provider-agnostic Resource Resolution layer.

Proves the layer is genuinely provider-neutral (a fake non-GitHub provider plugs
into the same registries and pipeline), that resolution provenance is tracked, that
tools with no registered provider pass through untouched, and that the GitHub
resolver emits deterministic resources with correct sources. No LLM, no network.
"""

from app.agent.github.identity import GithubIdentity
from app.agent.github.resolver import GithubResourceResolver
from app.agent.models.tool_spec import RiskLevel, SideEffectType, ToolKind, ToolSpec
from app.agent.resources import (
    ArgumentBuilderRegistry,
    ResourceAwareArgumentBuilder,
    ResourceResolverRegistry,
    provider_of,
)
from app.agent.resources.models import (
    Resource,
    ResolutionContext,
    ResolvedResources,
    ResourceSource,
)
from app.agent.runtime.arguments import ArgumentBuildResult
from app.agent.runtime.context import RunContext


def mcp_tool(tool_id: str, server: str, tool_name: str, schema: dict | None = None) -> ToolSpec:
    return ToolSpec(
        id=tool_id, name=tool_id, kind=ToolKind.MCP, description="d",
        input_schema=schema or {"type": "object"}, output_schema={},
        risk_level=RiskLevel.MEDIUM, side_effects=SideEffectType.EXTERNAL,
        requires_approval=False, handler_ref=f"mcp:{server}:{tool_name}",
        tags=[server],
    )


def internal_tool(tool_id: str) -> ToolSpec:
    return ToolSpec(
        id=tool_id, name=tool_id, kind=ToolKind.INTERNAL, description="d",
        input_schema={}, output_schema={}, risk_level=RiskLevel.LOW,
        side_effects=SideEffectType.READ, requires_approval=False,
    )


def rc(text, meta=None):
    c = RunContext.create(text, user_id="u", thread_id="t1")
    if meta:
        c.metadata.update(meta)
    return c


# --------------------------------------------------------------------------- #
# Resource model
# --------------------------------------------------------------------------- #

def test_resolved_resources_accessors():
    r = ResolvedResources(
        provider="demo",
        resources={"thing": Resource(type="thing", value="X", source=ResourceSource.REQUEST, provider="demo")},
        ambiguous={"other": 3}, flags={"scoped": True},
    )
    assert r.get("thing") == "X"
    assert r.source_of("thing") == ResourceSource.REQUEST
    assert r.get("missing") is None and r.source_of("missing") is None
    assert r.flag("scoped") is True and r.flag("nope") is False
    assert r.is_ambiguous is True
    assert r.source_map() == {"thing": "request"}


def test_resource_source_priority_order():
    order = [s.priority for s in (
        ResourceSource.REQUEST, ResourceSource.PRIOR_OUTPUT, ResourceSource.THREAD_STATE,
        ResourceSource.CONNECTOR_IDENTITY, ResourceSource.CACHED_CONTEXT, ResourceSource.CLARIFICATION,
    )]
    assert order == [0, 1, 2, 3, 4, 5]


# --------------------------------------------------------------------------- #
# provider_of + registry dispatch
# --------------------------------------------------------------------------- #

def test_provider_of_reads_mcp_server_id():
    assert provider_of(mcp_tool("mcp.github.search_repositories", "github", "search_repositories")) == "github"
    assert provider_of(mcp_tool("mcp.slack.list", "slack", "list")) == "slack"
    assert provider_of(internal_tool("get_job_status")) is None


def test_registry_dispatch_by_provider():
    class _R:
        provider = "demo"
        def resolve(self, ctx):
            return ResolvedResources(provider="demo")

    resolvers = ResourceResolverRegistry()
    resolvers.register(_R())
    assert resolvers.for_provider("demo") is not None
    assert resolvers.for_provider("github") is None
    assert resolvers.for_provider(None) is None
    assert resolvers.providers() == ["demo"]


# --------------------------------------------------------------------------- #
# Provider-agnostic pipeline (a fake, non-GitHub provider)
# --------------------------------------------------------------------------- #

class FakeResolver:
    provider = "demo"

    def resolve(self, ctx: ResolutionContext) -> ResolvedResources:
        # Deterministic: pull a "widget" resource out of the request text.
        widget = "gadget" if "gadget" in ctx.user_request else None
        resources = {}
        if widget:
            resources["widget"] = Resource(type="widget", value=widget,
                                           source=ResourceSource.REQUEST, provider="demo")
        return ResolvedResources(provider="demo", resources=resources)


class FakeBuilder:
    provider = "demo"

    def build(self, tool, resolved, *, planner_args, request_text):
        widget = resolved.get("widget")
        if widget is None:
            return ArgumentBuildResult.build_missing(["widget"])
        return ArgumentBuildResult.build_ok({"widget": widget})


def _demo_pipeline():
    resolvers = ResourceResolverRegistry(); resolvers.register(FakeResolver())
    builders = ArgumentBuilderRegistry(); builders.register(FakeBuilder())
    return ResourceAwareArgumentBuilder(resolvers, builders)


def test_fake_provider_plugs_into_same_pipeline():
    tool = mcp_tool("mcp.demo.do", "demo", "do")
    res = _demo_pipeline().build(tool, rc("please use the gadget"), {"user_id": "u"})
    assert res.ok and res.arguments == {"widget": "gadget"}


def test_fake_provider_missing_resource_rejects():
    tool = mcp_tool("mcp.demo.do", "demo", "do")
    res = _demo_pipeline().build(tool, rc("nothing here"), {"user_id": "u"})
    assert not res.ok and "widget" in res.missing_fields


def test_pipeline_passthrough_for_unregistered_provider():
    # A tool whose provider has no registered resolver is untouched.
    tool = mcp_tool("mcp.other.x", "other", "x")
    default = {"query": "hi", "user_id": "u"}
    res = _demo_pipeline().build(tool, rc("hi"), default)
    assert res.ok and res.arguments == default


def test_pipeline_passthrough_for_internal_tool():
    default = {"query": "hi", "user_id": "u"}
    res = _demo_pipeline().build(internal_tool("get_job_status"), rc("hi"), default)
    assert res.arguments == default


# --------------------------------------------------------------------------- #
# GitHub resolver produces resources with correct deterministic sources
# --------------------------------------------------------------------------- #

def _resolve(text, *, identity=GithubIdentity(owner="octocat", source="deployment_setting"), state=None):
    ctx = ResolutionContext(
        provider="github", capability_id="mcp.github.list_issues", user_request=text,
        execution_state=state or {},
    )
    return GithubResourceResolver(identity=identity).resolve(ctx)


def test_github_resolver_explicit_owner_source_is_request():
    r = _resolve("List issues in utkarshgunjiyal/runner-ai.")
    assert r.get("owner") == "utkarshgunjiyal"
    assert r.source_of("owner") == ResourceSource.REQUEST
    assert r.get("repo") == "runner-ai"


def test_github_resolver_my_owner_source_is_connector_identity():
    r = _resolve("List all my GitHub repositories.")
    assert r.get("owner") == "octocat"
    assert r.source_of("owner") == ResourceSource.CONNECTOR_IDENTITY
    assert r.flag("account_scoped") is True


def test_github_resolver_prior_context_owner_source_is_thread_state():
    state = {"github_active_repositories": [{"owner": "acme", "repo": "widgets"}]}
    r = _resolve("List issues in widgets.", state=state)
    assert r.get("owner") == "acme"
    assert r.source_of("owner") == ResourceSource.THREAD_STATE


def test_github_resolver_reports_ambiguity():
    state = {"github_active_repositories": [{"owner": "a", "repo": "dup"}, {"owner": "b", "repo": "dup"}]}
    r = _resolve("List issues in dup.", state=state)
    assert r.ambiguous.get("owner") == 2
    assert r.is_ambiguous is True


def test_github_resolver_numbers_are_positive_only():
    assert _resolve("Show issue 7 in runner-ai.").get("issue_number") == 7
    assert _resolve("Show pull request 3 in runner-ai.").get("pull_number") == 3
    assert _resolve("Show issue 0 in runner-ai.").get("issue_number") is None


# --------------------------------------------------------------------------- #
# Diagnostics + security: provenance only, never values
# --------------------------------------------------------------------------- #

def test_resource_resolved_diagnostic_has_sources_not_values():
    tool = mcp_tool("mcp.github.list_issues", "github", "list_issues",
                    {"type": "object", "properties": {"owner": {}, "repo": {}}, "required": ["owner", "repo"]})
    resolvers = ResourceResolverRegistry()
    resolvers.register(GithubResourceResolver(identity=GithubIdentity(owner="octocat")))
    builders = ArgumentBuilderRegistry()
    from app.agent.github.arguments import GithubArgumentBuilder
    builders.register(GithubArgumentBuilder())
    context = rc("List issues in secret-repo.")
    ResourceAwareArgumentBuilder(resolvers, builders).build(tool, context, {"user_id": "u"})

    events = {e["event"]: e for e in context.metadata.get("diagnostics", [])}
    assert "agent.resource_resolution_started" in events
    resolved = events["agent.resource_resolved"]
    assert "owner" in resolved["resolved_types"]
    assert resolved["resource_sources"]["owner"] == "connector_identity"
    # provenance only — the resolved repo VALUE must never appear in diagnostics.
    assert "secret-repo" not in str(resolved)
