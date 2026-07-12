"""Phase 46.2.6 — GitHub resource resolution + tool argument construction.

Config-free, no network. Proves natural-language GitHub requests become
semantically correct, schema-valid, account-scoped arguments; that missing/
ambiguous resources clarify instead of executing a global/guessed call; that
internal orchestration fields never reach a tool; and that no token/header ever
appears in arguments, summaries, or diagnostics.
"""

import asyncio

from app.agent.github.arguments import GithubArgumentBuilder
from app.agent.github.enrich import github_spec_transform
from app.agent.github.identity import (
    GithubIdentity,
    resolve_github_identity,
    validate_owner,
)
from app.agent.github.resolver import GithubResourceResolver
from app.agent.github.resources import resolve_resources
from app.agent.resources import (
    ArgumentBuilderRegistry,
    ResourceAwareArgumentBuilder,
    ResourceResolverRegistry,
)
from app.agent.resources.models import ResourceSource
from app.agent.mcp.models import MCPServerConfig, MCPToolCallResult, MCPToolDefinition, MCPTransport
from app.agent.mcp.registry import convert_tool_definition
from app.agent.models.tool_spec import RiskLevel, SideEffectType, ToolKind, ToolSpec
from app.agent.runtime.arguments import ArgumentStatus
from app.agent.runtime.context import BehaviorPath, BehaviorProfile, RunContext
from app.agent.runtime.direct_runtime import DirectRuntime, ExecutionStatus
from app.agent.tools.result import AdapterResult


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Schemas mirroring the official server's discovered input schemas.
# --------------------------------------------------------------------------- #

_SCHEMAS = {
    "search_repositories": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    "list_issues": {"type": "object", "properties": {"owner": {}, "repo": {}, "state": {}}, "required": ["owner", "repo"]},
    "issue_read": {"type": "object", "properties": {"owner": {}, "repo": {}, "issue_number": {}, "method": {}}, "required": ["owner", "repo", "issue_number"]},
    "list_pull_requests": {"type": "object", "properties": {"owner": {}, "repo": {}, "state": {}}, "required": ["owner", "repo"]},
    "pull_request_read": {"type": "object", "properties": {"owner": {}, "repo": {}, "pull_number": {}, "method": {}}, "required": ["owner", "repo", "pull_number"]},
    "search_issues": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
}


def _cfg():
    return MCPServerConfig(server_id="github", name="github", transport=MCPTransport.STDIO,
                           command=["srv"], timeout_seconds=5.0)


def spec(tool_name: str) -> ToolSpec:
    td = MCPToolDefinition(name=tool_name, description="d", input_schema=_SCHEMAS[tool_name])
    return github_spec_transform(_cfg(), tool_name, convert_tool_definition(_cfg(), td))


def rc(text: str, meta: dict | None = None) -> RunContext:
    c = RunContext.create(text, user_id="u", thread_id="t1")
    if meta:
        c.metadata.update(meta)
    return c


IDENT = GithubIdentity(owner="octocat", source="deployment_setting")


def pipeline(identity=IDENT):
    """The production layering: GitHub resolver + argument builder via the pipeline."""
    resolvers = ResourceResolverRegistry()
    resolvers.register(GithubResourceResolver(identity=identity))
    builders = ArgumentBuilderRegistry()
    builders.register(GithubArgumentBuilder())
    return ResourceAwareArgumentBuilder(resolvers, builders)


def build(tool_name, text, *, identity=IDENT, meta=None, planner=None):
    default = {"query": text, "user_id": "u", "thread_id": "t1"}
    m = dict(meta or {})
    if planner:
        m["capability_args"] = planner
    return pipeline(identity).build(spec(tool_name), rc(text, m), default)


# --------------------------------------------------------------------------- #
# Resource resolver
# --------------------------------------------------------------------------- #

def test_resolver_explicit_owner_repo_wins():
    r = resolve_resources("Show details for utkarshgunjiyal/runner-ai.", identity=IDENT)
    assert r.owner == "utkarshgunjiyal" and r.repo == "runner-ai"
    assert r.owner_source == "explicit"


def test_resolver_bare_repo_resolves_owner_from_identity():
    r = resolve_resources("List issues in runner-ai.", identity=IDENT)
    assert r.repo == "runner-ai" and r.owner == "octocat"
    assert r.owner_source == "connector_identity"


def test_resolver_my_is_account_scoped():
    r = resolve_resources("List all my GitHub repositories.", identity=IDENT)
    assert r.account_scoped is True


def test_resolver_issue_and_pull_numbers_positive():
    assert resolve_resources("Show issue 12.").issue_number == 12
    assert resolve_resources("Summarize pull request 5.").pull_number == 5
    # non-positive / absent numbers are never invented
    assert resolve_resources("Show issue 0.").issue_number is None
    assert resolve_resources("List issues in runner-ai.").issue_number is None


def test_resolver_ambiguous_owner_not_guessed():
    known = [{"owner": "a", "repo": "runner-ai"}, {"owner": "b", "repo": "runner-ai"}]
    r = resolve_resources("List issues in runner-ai.", identity=IDENT, known_repositories=known)
    assert r.owner is None and r.owner_candidates == 2


# --------------------------------------------------------------------------- #
# A. Authenticated repository listing
# --------------------------------------------------------------------------- #

def test_account_scoped_listing_uses_user_qualifier():
    res = build("search_repositories", "List all my GitHub repositories.")
    assert res.status == ArgumentStatus.OK
    assert res.arguments == {"query": "user:octocat"}  # scoped, NOT a global search
    assert "List all my GitHub repositories." not in str(res.arguments)


def test_account_scoped_listing_falls_back_to_at_me_without_identity():
    res = build("search_repositories", "List all my GitHub repositories.", identity=GithubIdentity())
    assert res.arguments == {"query": "user:@me"}


# --------------------------------------------------------------------------- #
# B/C. Explicit + implicit repository
# --------------------------------------------------------------------------- #

def test_explicit_owner_repo_preserved():
    res = build("list_issues", "List issues in utkarshgunjiyal/runner-ai.")
    assert res.arguments["owner"] == "utkarshgunjiyal"
    assert res.arguments["repo"] == "runner-ai"


def test_implicit_owner_from_connector_identity():
    res = build("search_repositories", "Find my runner-ai repository.")
    assert res.status == ArgumentStatus.OK
    assert res.arguments == {"query": "runner-ai user:octocat"}


# --------------------------------------------------------------------------- #
# D/E/F. Issues + reads + pull requests
# --------------------------------------------------------------------------- #

def test_list_issues_builds_owner_repo_state():
    res = build("list_issues", "List open issues in runner-ai.")
    assert res.arguments == {"owner": "octocat", "repo": "runner-ai", "state": "open"}


def test_issue_read_parses_and_validates_number():
    res = build("issue_read", "Show issue 1 in runner-ai.")
    assert res.arguments["issue_number"] == 1
    assert res.arguments["owner"] == "octocat" and res.arguments["repo"] == "runner-ai"
    assert res.arguments["method"] == "get"


def test_pull_request_read_resolves_pull_number():
    res = build("pull_request_read", "Show pull request 1 in runner-ai.")
    assert res.arguments["pull_number"] == 1
    assert res.arguments["repo"] == "runner-ai"


def test_search_issues_is_account_scoped():
    res = build("search_issues", "Search my GitHub issues for Docker.")
    assert res.arguments == {"query": "Docker author:octocat"}


# --------------------------------------------------------------------------- #
# G/H. Missing context + ambiguity → clarify, never guess
# --------------------------------------------------------------------------- #

def test_missing_repository_context_is_clarification():
    res = build("issue_read", "Show issue 12.", identity=GithubIdentity())
    assert res.status == ArgumentStatus.MISSING
    assert "owner" in res.missing_fields and "repo" in res.missing_fields
    assert res.arguments == {}


def test_ambiguous_repository_requests_clarification():
    known = [{"owner": "a", "repo": "runner-ai"}, {"owner": "b", "repo": "runner-ai"}]
    res = build("list_issues", "List issues in runner-ai.",
                meta={"resource_state": {"github_active_repositories": known}})
    assert res.status == ArgumentStatus.AMBIGUOUS
    assert res.ambiguity_count == 2


# --------------------------------------------------------------------------- #
# I. Schema validation
# --------------------------------------------------------------------------- #

def test_internal_fields_never_in_built_arguments():
    res = build("search_repositories", "List all my GitHub repositories.")
    for internal in ("user_id", "thread_id", "run_id", "request_id"):
        assert internal not in res.arguments


def test_undeclared_keys_are_projected_out():
    # A schema declaring only 'query' must not receive owner/repo etc.
    res = build("search_repositories", "Find my runner-ai repository.")
    assert set(res.arguments.keys()) == {"query"}


def test_invalid_issue_number_is_not_sent():
    # "issue 0" is not a positive integer → missing, never a zero/negative arg.
    res = build("issue_read", "Show issue 0 in runner-ai.")
    assert res.status == ArgumentStatus.MISSING
    assert "issue_number" in res.missing_fields


def test_planner_supplied_args_are_honored():
    res = build("list_issues", "List issues.", planner={"owner": "acme", "repo": "widgets"})
    assert res.arguments["owner"] == "acme" and res.arguments["repo"] == "widgets"


# --------------------------------------------------------------------------- #
# J. Security
# --------------------------------------------------------------------------- #

def test_no_secret_in_arguments_or_summary():
    res = build("search_repositories", "List all my GitHub repositories.")
    blob = str(res.model_dump())
    assert "Authorization" not in blob and "Bearer" not in blob and "ghp_" not in blob


def test_non_github_tool_passthrough_unchanged():
    internal = ToolSpec(
        id="get_job_status", name="get_job_status", kind=ToolKind.INTERNAL,
        description="d", input_schema={}, output_schema={}, risk_level=RiskLevel.LOW,
        side_effects=SideEffectType.READ, requires_approval=False,
    )
    default = {"query": "status?", "user_id": "u", "job_id": "j1"}
    # No provider resolver registered for an internal tool → untouched passthrough.
    res = pipeline().build(internal, rc("status?"), default)
    assert res.status == ArgumentStatus.OK
    assert res.arguments == default  # left exactly as the caller built them


# --------------------------------------------------------------------------- #
# Identity resolution
# --------------------------------------------------------------------------- #

def test_validate_owner_rejects_bad_values():
    assert validate_owner("octocat") == "octocat"
    assert validate_owner("a-b-c") == "a-b-c"
    assert validate_owner("-bad") is None
    assert validate_owner("bad/slash") is None
    assert validate_owner("") is None and validate_owner(None) is None


def test_resolve_identity_prefers_get_me():
    async def get_me():
        return MCPToolCallResult(success=True, structured_content={"login": "real-user"})

    ident = run(resolve_github_identity(configured_owner="fallback", get_me_fn=get_me))
    assert ident.owner == "real-user" and ident.source == "get_me"


def test_resolve_identity_falls_back_to_setting_on_get_me_failure():
    async def get_me():
        raise RuntimeError("network down")

    ident = run(resolve_github_identity(configured_owner="deploy-owner", get_me_fn=get_me))
    assert ident.owner == "deploy-owner" and ident.source == "deployment_setting"


def test_resolve_identity_unknown_when_nothing_available():
    ident = run(resolve_github_identity(configured_owner=None, get_me_fn=None))
    assert ident.owner is None and ident.known is False


# --------------------------------------------------------------------------- #
# DirectRuntime integration (the production path)
# --------------------------------------------------------------------------- #

from app.agent.capabilities.models import CapabilityMatch, CapabilityRetrievalResponse


class _Retriever:
    def __init__(self, tool):
        self._tool = tool

    def retrieve(self, request):
        return CapabilityRetrievalResponse(
            query=request.query, matches=[CapabilityMatch(tool=self._tool, score=10.0)]
        )


class _RecordingExecutor:
    def __init__(self):
        self.calls = []

    async def execute(self, tool, args):
        self.calls.append((tool.id, dict(args)))
        return AdapterResult.ok(output={"items": []})


def _direct(tool, *, identity=IDENT):
    return DirectRuntime(
        _Retriever(tool), _RecordingExecutor(),
        argument_builder=pipeline(identity).build,
    )


def _direct_context(text):
    c = RunContext.create(text, user_id="u", thread_id="t1")
    c.attach_behavior_profile(BehaviorProfile(path=BehaviorPath.DIRECT, reason="gh", confidence=1.0))
    return c


def test_directruntime_executes_account_scoped_arguments():
    tool = spec("search_repositories")
    dr = _direct(tool)
    out = run(dr.run(_direct_context("List all my GitHub repositories.")))
    executor = dr._executor
    assert len(executor.calls) == 1
    _, args = executor.calls[0]
    assert args == {"query": "user:octocat"}  # not the raw request, not global
    assert out.metadata["execution_status"] == ExecutionStatus.SUCCESS.value


def test_directruntime_missing_resource_makes_no_mcp_call():
    tool = spec("issue_read")
    dr = _direct(tool, identity=GithubIdentity())
    out = run(dr.run(_direct_context("Show issue 12.")))
    assert dr._executor.calls == []  # no MCP call on missing resource
    assert out.metadata["execution_status"] == ExecutionStatus.NEEDS_USER.value
    assert out.metadata["argument_resolution"]["status"] == "missing"
