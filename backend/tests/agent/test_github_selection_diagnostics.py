"""Phase 46.2.3 — GitHub runtime selection diagnostics.

Asserts the safe diagnostic events are emitted (and are redacted) WITHOUT changing
selection behavior. Config-free; fake MCP; no live GitHub. The events let an
operator distinguish a ranking error vs a planner-selection error vs a
task-resolution error vs a registry-binding error vs an MCP-invocation error.
"""

import asyncio
import json

from app.agent.github import (
    build_github_mcp_server_config,
    github_result_normalizer,
    github_spec_transform,
)
from app.agent.github.server import GITHUB_MCP_SERVER_ID, GITHUB_READ_ONLY_TOOLS
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
    {"name": "runner-ai", "full_name": "u/runner-ai", "html_url": "https://gh/u/runner-ai"},
]})


def _defs():
    return [MCPToolDefinition(name=n, description=n,
                             input_schema={"type": "object", "properties": {"query": {"type": "string"}}})
            for n in GITHUB_READ_ONLY_TOOLS]


def _snapshot(*, connected):
    state = derive_state(configured=True, connected=connected,
                         capabilities=["search_repositories"] if connected else [],
                         allowed_tool_count=6 if connected else 0,
                         error_code=None if connected else "mcp_transport_unavailable")
    rec = build_github_connector_record("u", state)
    return [rec.public_view()] if rec else []


class _SG:
    def __init__(self, snap):
        self._snap = snap

    async def evaluate(self, rc, *, is_resume=False):
        rc.metadata["connectors"] = self._snap
        return ScopeDecision(action="proceed", metadata={"document_scope": "none"})


def _orch(*, connected=True):
    client = FakeMCPClient(tools={GITHUB_MCP_SERVER_ID: _defs()},
                           results={(GITHUB_MCP_SERVER_ID, "search_repositories"): REPOS})
    mgr = MCPRegistryManager(ToolRegistry(), client, spec_transform=github_spec_transform)
    run(mgr.register_server(build_github_mcp_server_config(token="ghp_SECRET")))
    run(mgr.discover_server_tools(GITHUB_MCP_SERVER_ID))
    orch = build_default_runtime(mcp_registry_manager=mgr,
                                 mcp_result_normalizers={"github": github_result_normalizer},
                                 connector_eligibility=True, scope_gate=_SG(_snapshot(connected=connected)))
    return orch, client


def _events(rc, name):
    return [d for d in rc.metadata.get("diagnostics", []) if d["event"] == name]


# --------------------------------------------------------------------------- #
# A. Direct path
# --------------------------------------------------------------------------- #

def test_direct_path_diagnostics_show_repository_selection():
    orch, _ = _orch(connected=True)
    res = run(orch.run("List all my GitHub repositories.", user_id="u", thread_id="t1"))
    rc = res.run_context

    path = _events(rc, "agent.runtime_path_selected")[0]
    assert path["path"] == "direct"
    assert "request_hash" in path and "List" not in json.dumps(path)  # raw text not logged

    cand = _events(rc, "agent.capability_candidates")[-1]
    assert cand["candidates"][0]["capability_id"] == "mcp.github.search_repositories"
    # search_repositories outranks list_issues in the recorded ranking.
    by_id = {c["capability_id"]: c for c in cand["candidates"]}
    assert by_id["mcp.github.search_repositories"]["final_score"] > by_id["mcp.github.list_issues"]["final_score"]

    sel = _events(rc, "agent.capability_selected")[-1]
    assert sel["capability_id"] == "mcp.github.search_repositories"

    binding = _events(rc, "agent.tool_binding_resolved")[-1]
    assert binding["server_id"] == "github" and binding["mcp_tool_name"] == "search_repositories"
    assert binding["binding_lookup_success"] is True


def test_direct_path_mcp_invocation_events():
    orch, _ = _orch(connected=True)
    rc = run(orch.run("List all my GitHub repositories.", user_id="u", thread_id="t1")).run_context

    invoked = _events(rc, "agent.mcp_tool_invoked")[-1]
    assert invoked["server_id"] == "github" and invoked["tool_name"] == "search_repositories"
    assert invoked["connector_status"] == "connected"
    # Argument KEY names only — never values.
    assert invoked["argument_keys"] == ["query", "thread_id", "user_id"]

    completed = _events(rc, "agent.mcp_tool_completed")[-1]
    assert completed["tool_name"] == "search_repositories" and completed["success"] is True
    assert completed["item_count"] == 1 and completed["error_code"] is None


# --------------------------------------------------------------------------- #
# B. Planner path
# --------------------------------------------------------------------------- #

def test_planner_path_diagnostics():
    from app.agent.runtime.context import BehaviorPath, BehaviorProfile, RunContext
    from app.agent.runtime.planner_runtime import ExecutionPlan, PlannerRuntime, PlannerTask

    orch, _ = _orch(connected=True)

    # (a) planner candidate set is recorded when the planner prompt is built.
    rc = RunContext.create("List all my GitHub repositories.", user_id="u", thread_id="t1")
    rc.metadata["connectors"] = _snapshot(connected=True)
    rc.attach_behavior_profile(BehaviorProfile(path=BehaviorPath.PLANNER, reason="multi", confidence=1.0))
    orch._build_planner_prompt(rc)
    assert _events(rc, "agent.planner_candidates"), "planner_candidates not recorded"
    assert any(e["path"] == "planner" for e in _events(rc, "agent.capability_candidates"))

    # (b) each plan task records how it resolved to a tool + what executed.
    planner = PlannerRuntime(orch._direct_runtime, orch._capability_retriever)
    parent = RunContext.create("List all my GitHub repositories.", user_id="u", thread_id="t1")
    parent.metadata["connectors"] = _snapshot(connected=True)
    parent.attach_behavior_profile(BehaviorProfile(path=BehaviorPath.PLANNER, reason="multi", confidence=1.0))
    plan = ExecutionPlan(id="p1", goal="repos",
                         tasks=[PlannerTask(id="t1", request="List all my GitHub repositories.")])
    run(planner.run(parent, plan))
    resolved = _events(parent, "agent.plan_tool_resolved")
    assert resolved and resolved[0]["task_id"] == "t1"
    assert resolved[0]["resolved_capability"] == "mcp.github.search_repositories"


# --------------------------------------------------------------------------- #
# C. Mapping integrity — no cross-binding
# --------------------------------------------------------------------------- #

def test_binding_mapping_integrity():
    from app.agent.runtime.diagnostics import binding_view

    # Resolve bindings straight from ToolSpecs (what the diagnostics report).
    client = FakeMCPClient(tools={GITHUB_MCP_SERVER_ID: _defs()})
    mgr = MCPRegistryManager(ToolRegistry(), client, spec_transform=github_spec_transform)
    run(mgr.register_server(build_github_mcp_server_config(token="t")))
    run(mgr.discover_server_tools(GITHUB_MCP_SERVER_ID))

    repo = binding_view(mgr.tool_registry.get("mcp.github.search_repositories"))
    issue = binding_view(mgr.tool_registry.get("mcp.github.list_issues"))
    assert repo["server_id"] == "github" and repo["mcp_tool_name"] == "search_repositories"
    assert issue["server_id"] == "github" and issue["mcp_tool_name"] == "list_issues"
    assert repo["mcp_tool_name"] != issue["mcp_tool_name"]  # no cross-binding
    assert repo["handler_ref"] == "mcp:github:search_repositories"


# --------------------------------------------------------------------------- #
# D. Eligibility
# --------------------------------------------------------------------------- #

def test_unavailable_connector_shows_no_github_candidate_and_no_invocation():
    orch, client = _orch(connected=False)
    rc = run(orch.run("List all my GitHub repositories.", user_id="u", thread_id="t1")).run_context

    cand = _events(rc, "agent.capability_candidates")[-1]
    assert all(not c["capability_id"].startswith("mcp.github.") for c in cand["candidates"])
    # No GitHub MCP invocation happened.
    gh_invoked = [e for e in _events(rc, "agent.mcp_tool_invoked") if e.get("server_id") == "github"]
    assert gh_invoked == []
    assert not any(c[0] == "github" for c in client.call_tool_calls)


# --------------------------------------------------------------------------- #
# E. Security — nothing sensitive in any diagnostic event
# --------------------------------------------------------------------------- #

def test_diagnostics_never_leak_secrets_or_payloads():
    orch, _ = _orch(connected=True)
    rc = run(orch.run("List all my GitHub repositories.", user_id="u", thread_id="t1")).run_context
    blob = json.dumps(rc.metadata.get("diagnostics", []))
    assert "ghp_SECRET" not in blob                    # token
    assert "Authorization" not in blob and "Bearer" not in blob  # auth header
    assert "GITHUB_PERSONAL_ACCESS_TOKEN" not in blob
    assert "List all my GitHub repositories." not in blob  # raw request text (hash only)
    assert "structured_content" not in blob and "html_url" not in blob  # raw payload
    # Argument values are never present — only key names.
    for inv in _events(rc, "agent.mcp_tool_invoked"):
        assert all(isinstance(k, str) for k in inv["argument_keys"])
        assert "u" not in [v for v in inv["argument_keys"]]  # no user_id value


# --------------------------------------------------------------------------- #
# F. No behavior change
# --------------------------------------------------------------------------- #

def test_diagnostics_do_not_change_selection():
    orch, client = _orch(connected=True)
    res = run(orch.run("List all my GitHub repositories.", user_id="u", thread_id="t1"))
    # Selection + execution are exactly as before instrumentation.
    assert res.run_context.selected_capabilities == ["mcp.github.search_repositories"]
    assert client.call_tool_calls[-1][1] == "search_repositories"
    # Diagnostics live only under metadata["diagnostics"]; they never enter the answer.
    assert "diagnostics" not in json.dumps(res.metadata)
    assert "agent.capability_selected" not in res.answer.text
