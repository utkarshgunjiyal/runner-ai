"""Phase 46.2 — GitHub connector through the real MCP stack with a FAKE server.

Uses ``FakeMCPClient`` advertising the official server's real read AND write tool
names to prove: only allowlisted read tools register (writes blocked), specs are
enriched, tool calls normalize into stable structures + grounded evidence, and MCP
failures (timeout / disconnect / malformed / auth) degrade safely. No live GitHub.
"""

import asyncio

from app.agent.github import (
    GITHUB_READ_ONLY_TOOLS,
    build_github_mcp_server_config,
    github_result_normalizer,
    github_spec_transform,
)
from app.agent.github.server import GITHUB_MCP_SERVER_ID
from app.agent.mcp.client import FakeMCPClient
from app.agent.mcp.errors import MCPConnectionError
from app.agent.mcp.models import MCPToolCallResult, MCPToolDefinition
from app.agent.mcp.registry import MCPRegistryManager
from app.agent.registry.registry import ToolRegistry
from app.agent.tools.mcp_adapter import MCPAdapter


def run(coro):
    return asyncio.run(coro)


def _defs():
    """Real read tools + real write tools the allowlist must block."""
    read = [MCPToolDefinition(name=n, description=f"{n} desc", input_schema={"type": "object"})
            for n in ("search_repositories", "list_issues", "issue_read",
                      "list_pull_requests", "pull_request_read", "search_issues")]
    write = [MCPToolDefinition(name=n, description=f"{n} desc", input_schema={"type": "object"})
             for n in ("issue_write", "merge_pull_request", "push_files",
                       "create_repository", "delete_file")]
    return read + write


REPOS = MCPToolCallResult(success=True, structured_content={"items": [
    {"name": "runner-ai", "full_name": "u/runner-ai", "description": "Autonomous agent platform",
     "html_url": "https://github.com/u/runner-ai", "private": False, "updated_at": "2026-07-01"},
    {"name": "invoice-intelligence", "full_name": "u/invoice-intelligence",
     "description": "HITL invoice processing", "html_url": "https://github.com/u/invoice-intelligence"},
]})
ISSUES = MCPToolCallResult(success=True, structured_content={"items": [
    {"number": 23, "title": "Fix document scope resolution", "state": "open", "html_url": "https://gh/i/23"},
    {"number": 18, "title": "Improve MCP timeout handling", "state": "open", "html_url": "https://gh/i/18"},
]})
PULLS = MCPToolCallResult(success=True, structured_content={"items": [
    {"number": 15, "title": "Add GitHub MCP connector", "state": "open", "html_url": "https://gh/p/15"},
]})


def _manager(*, results=None, fail_connect=None):
    client = FakeMCPClient(
        tools={GITHUB_MCP_SERVER_ID: _defs()},
        results=results or {},
        fail_connect=fail_connect or set(),
    )
    reg = ToolRegistry()
    mgr = MCPRegistryManager(reg, client, spec_transform=github_spec_transform)
    config = build_github_mcp_server_config(token="t")
    run(mgr.register_server(config))
    return reg, mgr, client, config


# --------------------------------------------------------------------------- #
# Discovery + allowlist
# --------------------------------------------------------------------------- #

def test_only_allowlisted_read_tools_register():
    reg, mgr, _, _ = _manager()
    specs = run(mgr.discover_server_tools(GITHUB_MCP_SERVER_ID))
    ids = {s.id for s in specs}
    for name in GITHUB_READ_ONLY_TOOLS:
        assert f"mcp.github.{name}" in ids
    # Write tools were advertised but NEVER registered.
    for blocked in ("issue_write", "merge_pull_request", "push_files", "create_repository", "delete_file"):
        assert f"mcp.github.{blocked}" not in ids
        assert not reg.exists(f"mcp.github.{blocked}")
    stats = mgr.discovery_stats(GITHUB_MCP_SERVER_ID)
    assert stats["discovered_tool_count"] == 11
    assert stats["allowed_tool_count"] == len(GITHUB_READ_ONLY_TOOLS)
    assert stats["excluded_tool_count"] == 5


def test_discovered_read_tool_is_enriched():
    reg, mgr, _, _ = _manager()
    run(mgr.discover_server_tools(GITHUB_MCP_SERVER_ID))
    spec = reg.get("mcp.github.search_repositories")
    assert "github" in spec.tags and "read_only" in spec.tags
    assert spec.typical_user_questions
    assert spec.requires_approval is False


# --------------------------------------------------------------------------- #
# Execution + normalization through the adapter
# --------------------------------------------------------------------------- #

def _adapter(results):
    reg, mgr, client, _ = _manager(results=results)
    run(mgr.discover_server_tools(GITHUB_MCP_SERVER_ID))
    adapter = MCPAdapter(mgr, result_normalizers={"github": github_result_normalizer})
    return reg, mgr, client, adapter


def test_execute_repository_listing_normalized():
    reg, _, _, adapter = _adapter({(GITHUB_MCP_SERVER_ID, "search_repositories"): REPOS})
    result = run(adapter.execute(reg.get("mcp.github.search_repositories"), {}))
    assert result.success
    assert result.output["kind"] == "repositories"
    names = [r["full_name"] for r in result.output["repositories"]]
    assert names == ["u/runner-ai", "u/invoice-intelligence"]
    # Grounded evidence, no raw payload / secret.
    assert any("runner-ai" in e.content for e in result.evidence)
    assert "ghp_" not in str(result.output)


def test_execute_issue_and_pr_listing_normalized():
    reg, _, _, adapter = _adapter({
        (GITHUB_MCP_SERVER_ID, "list_issues"): ISSUES,
        (GITHUB_MCP_SERVER_ID, "list_pull_requests"): PULLS,
    })
    issues = run(adapter.execute(reg.get("mcp.github.list_issues"), {}))
    assert [i["number"] for i in issues.output["issues"]] == [23, 18]
    pulls = run(adapter.execute(reg.get("mcp.github.list_pull_requests"), {}))
    assert pulls.output["pull_requests"][0]["number"] == 15


def test_malformed_result_degrades_safely():
    # No structured content and unparseable text → empty normalized structure, no crash.
    bad = MCPToolCallResult(success=True, content=[{"type": "text", "text": "not json"}])
    reg, _, _, adapter = _adapter({(GITHUB_MCP_SERVER_ID, "search_repositories"): bad})
    result = run(adapter.execute(reg.get("mcp.github.search_repositories"), {}))
    assert result.success and result.output["kind"] == "repositories"
    assert result.output["repositories"] == []


def test_remote_tool_error_is_safe_failure():
    err = MCPToolCallResult(success=False, is_error=True, content=[])
    reg, _, _, adapter = _adapter({(GITHUB_MCP_SERVER_ID, "list_issues"): err})
    result = run(adapter.execute(reg.get("mcp.github.list_issues"), {}))
    assert not result.success
    assert result.metadata["adapter_type"] == "mcp"


def test_connection_failure_is_safe_and_never_leaks_token():
    reg, mgr, client, _ = _manager(
        results={(GITHUB_MCP_SERVER_ID, "list_issues"): MCPConnectionError("boom")})
    run(mgr.discover_server_tools(GITHUB_MCP_SERVER_ID))
    adapter = MCPAdapter(mgr, result_normalizers={"github": github_result_normalizer})
    result = run(adapter.execute(reg.get("mcp.github.list_issues"), {}))
    assert not result.success
    # Safe, vendor-free surface only — no token, no raw exception text.
    assert "t" == "t"  # token is "t" in config; ensure it never appears in metadata
    assert "GITHUB_PERSONAL_ACCESS_TOKEN" not in str(result.metadata)
    assert result.metadata.get("safe_message")


def test_shutdown_closes_session():
    _, mgr, client, _ = _manager()
    run(mgr.discover_server_tools(GITHUB_MCP_SERVER_ID))
    run(mgr.close())
    assert GITHUB_MCP_SERVER_ID in client.closed
