"""Phase 46.2 — GitHub connector runtime integration (config-free, fake MCP).

Proves the eligibility + execution guarantees end-to-end through the real
orchestrator: a GitHub request selects the GitHub capability when the connector is
CONNECTED and executes it into a grounded, normalized answer; when the connector is
UNAVAILABLE the GitHub tools are filtered out before planning (never reach the
planner / LLM), with no secret and no raw MCP payload.
"""

import asyncio

from app.agent.github import (
    build_github_mcp_server_config,
    github_result_normalizer,
    github_spec_transform,
)
from app.agent.github.server import GITHUB_MCP_SERVER_ID
from app.agent.github.status import build_github_connector_record, derive_state
from app.agent.mcp.client import FakeMCPClient
from app.agent.mcp.models import MCPToolCallResult, MCPToolDefinition
from app.agent.mcp.registry import MCPRegistryManager
from app.agent.registry.registry import ToolRegistry
from app.agent.runtime.factory import build_default_runtime
from app.agent.runtime.scope_gate import ScopeDecision


def run(coro):
    return asyncio.run(coro)


REPOS = MCPToolCallResult(success=True, structured_content={"items": [
    {"name": "runner-ai", "full_name": "u/runner-ai", "description": "Autonomous agent platform",
     "html_url": "https://github.com/u/runner-ai"},
    {"name": "invoice-intelligence", "full_name": "u/invoice-intelligence",
     "description": "HITL invoice processing", "html_url": "https://github.com/u/invoice-intelligence"},
]})


def _github_defs():
    return [MCPToolDefinition(name=n, description=f"{n}", input_schema={"type": "object"})
            for n in ("search_repositories", "list_issues", "list_pull_requests")]


def _connectors_snapshot(*, connected: bool):
    state = derive_state(
        configured=True, connected=connected,
        capabilities=["search_repositories"] if connected else [],
        allowed_tool_count=1 if connected else 0,
        error_code=None if connected else "mcp_transport_unavailable",
    )
    record = build_github_connector_record("u", state)
    return [record.public_view()] if record else []


class FakeScopeGate:
    """Injects the per-run connector snapshot the eligibility layer reads."""

    def __init__(self, snapshot):
        self._snapshot = snapshot

    async def evaluate(self, run_context, *, is_resume=False):
        run_context.metadata["connectors"] = self._snapshot
        return ScopeDecision(action="proceed", metadata={"document_scope": "none"})


def _orchestrator(*, connected: bool):
    client = FakeMCPClient(
        tools={GITHUB_MCP_SERVER_ID: _github_defs()},
        results={(GITHUB_MCP_SERVER_ID, "search_repositories"): REPOS},
    )
    mgr = MCPRegistryManager(ToolRegistry(), client, spec_transform=github_spec_transform)
    run(mgr.register_server(build_github_mcp_server_config(token="ghp_SECRET")))
    run(mgr.discover_server_tools(GITHUB_MCP_SERVER_ID))
    orch = build_default_runtime(
        mcp_registry_manager=mgr,
        mcp_result_normalizers={"github": github_result_normalizer},
        connector_eligibility=True,
        scope_gate=FakeScopeGate(_connectors_snapshot(connected=connected)),
    )
    return orch, client


def test_repository_request_selects_repositories_despite_issue_pr_history():
    """Regression (Phase 46.2.2): 'List all my GitHub repositories' must select
    ``search_repositories`` even when the working context is full of prior issue/PR
    turns. Capability selection is driven by the CURRENT request, not by history
    folded into the query — otherwise a prior topic outranks the current intent."""
    from app.agent.capabilities.keyword_retriever import KeywordCapabilityRetriever
    from app.agent.connectors.eligibility import EligibilityCapabilityRetriever
    from app.agent.retriever.capability_retriever import HybridCapabilityRetriever
    from app.agent.runtime.context import BehaviorPath, BehaviorProfile, RunContext, WorkingContextItem
    from app.agent.runtime.direct_runtime import DirectRuntime
    from app.agent.tools.result import AdapterResult

    client = FakeMCPClient(tools={GITHUB_MCP_SERVER_ID: _github_defs()})
    mgr = MCPRegistryManager(ToolRegistry(), client, spec_transform=github_spec_transform)
    run(mgr.register_server(build_github_mcp_server_config(token="t")))
    run(mgr.discover_server_tools(GITHUB_MCP_SERVER_ID))

    executed: list[str] = []

    class RecordingExecutor:
        async def execute(self, tool, args):
            executed.append(tool.id)
            return AdapterResult.ok(output={"ok": True}, evidence=[])

    retriever = EligibilityCapabilityRetriever(
        HybridCapabilityRetriever(KeywordCapabilityRetriever(mgr.tool_registry))
    )
    direct = DirectRuntime(retriever, RecordingExecutor())

    # Prior turns were all about issues and pull requests.
    history = [
        WorkingContextItem(source="recent_message", content="List open issues in runner-ai", metadata={"seq": 1}),
        WorkingContextItem(source="recent_message", content="Show issue 23 in runner-ai", metadata={"seq": 2}),
        WorkingContextItem(source="recent_message", content="List open pull requests in runner-ai", metadata={"seq": 3}),
        WorkingContextItem(source="thread_summary",
                           content="The user is reviewing GitHub issues and pull requests in runner-ai."),
    ]
    rc = RunContext.create("List all my GitHub repositories.", user_id="u", thread_id="t1",
                           working_context=history)
    rc.attach_behavior_profile(BehaviorProfile(path=BehaviorPath.DIRECT, reason="github", confidence=1.0))
    rc.metadata["connectors"] = _connectors_snapshot(connected=True)

    run(direct.run(rc))
    assert executed == ["mcp.github.search_repositories"], executed


def test_github_request_selects_and_grounds_when_connected():
    orch, client = _orchestrator(connected=True)
    result = run(orch.run("List my GitHub repositories", user_id="u", thread_id="t1"))

    # The GitHub read tool was actually invoked via MCP.
    assert client.call_tool_calls
    assert client.call_tool_calls[-1][1] == "search_repositories"
    assert "mcp.github.search_repositories" in result.run_context.selected_capabilities

    # Answer is grounded in normalized GitHub data — real repos, no invention.
    answer = result.answer.text
    assert "runner-ai" in answer
    assert "invoice-intelligence" in answer

    # No raw MCP payload and no secret anywhere in the answer or tool output.
    assert "ghp_SECRET" not in answer
    assert "GITHUB_PERSONAL_ACCESS_TOKEN" not in answer
    # Normalized structured output is present (not a raw payload dump).
    out = result.run_context.tool_outputs[-1].output
    assert out["kind"] == "repositories"
    assert "ghp_SECRET" not in str(out)


def test_github_tools_excluded_when_connector_unavailable():
    orch, client = _orchestrator(connected=False)
    # The candidate set the planner/direct runtime sees must not contain GitHub tools.
    from app.agent.runtime.context import RunContext

    rc = RunContext.create("List my GitHub repositories", user_id="u", thread_id="t1")
    rc.metadata["connectors"] = _connectors_snapshot(connected=False)
    matches = orch._capability_retriever.retrieve_for_run_context(rc, top_k=10).matches
    assert all(not m.tool.id.startswith("mcp.github.") for m in matches)

    # And a full run never invokes the GitHub MCP tool.
    result = run(orch.run("List my GitHub repositories", user_id="u", thread_id="t1"))
    assert not any(c[1] == "search_repositories" for c in client.call_tool_calls)
    assert "mcp.github.search_repositories" not in result.run_context.selected_capabilities
    # No fabricated GitHub repository data in the answer.
    assert "invoice-intelligence" not in result.answer.text
