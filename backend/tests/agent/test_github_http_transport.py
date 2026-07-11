"""Phase 46.2.1 — GitHub over the real Streamable HTTP MCP transport (fake POST).

Drives the ACTUAL ``StreamableHTTPTransport`` (with an injectable ``post``) through
the MCP connection manager + registry, proving the remote-endpoint path works from
a containerized backend: initialize → notifications/initialized → tools/list →
tools/call, JSON and SSE responses, session-id handling, the ``Authorization:
Bearer`` header, the read-only allowlist, and safe auth/error mapping — with the
token never appearing in headers-echo assertions or error text. No live network.
"""

import asyncio
import json

from app.agent.github import build_github_mcp_server_config, github_spec_transform
from app.agent.github.server import GITHUB_MCP_SERVER_ID, GITHUB_READ_ONLY_TOOLS
from app.agent.mcp.connection import MCPConnectionManager, TransportMCPClient
from app.agent.mcp.errors import TransportAuthenticationError
from app.agent.mcp.registry import MCPRegistryManager
from app.agent.mcp.transports.http import StreamableHTTPTransport
from app.agent.registry.registry import ToolRegistry


def run(coro):
    return asyncio.run(coro)


async def _nosleep(_s):
    return None


class FakeResponse:
    def __init__(self, payload, *, status=200, headers=None, sse=False):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self._sse = sse
        if sse:
            self.headers.setdefault("content-type", "text/event-stream")
            self.text = f"event: message\ndata: {json.dumps(payload)}\n\n"
        else:
            self.headers.setdefault("content-type", "application/json")

    def json(self):
        return self._payload


# Advertise real read AND write tool names — the allowlist must drop the writes.
_TOOLS = [
    {"name": n, "description": n, "inputSchema": {"type": "object"}}
    for n in (*GITHUB_READ_ONLY_TOOLS, "issue_write", "merge_pull_request", "push_files")
]
_REPOS = {
    "content": [], "isError": False,
    "structuredContent": {"items": [
        {"name": "runner-ai", "full_name": "u/runner-ai", "description": "Agent platform",
         "html_url": "https://github.com/u/runner-ai"},
    ]},
}


def make_post(*, status_for=None, sse=False, capture=None):
    """A fake httpx-style post that answers JSON-RPC by method."""
    status_for = status_for or {}

    async def _post(url, *, headers=None, json=None, timeout=None):
        if capture is not None:
            capture.append({"url": url, "headers": dict(headers or {}), "body": json})
        method = (json or {}).get("method")
        forced = status_for.get(method)
        if forced:
            return FakeResponse({"error": "x"}, status=forced)
        sess = {"mcp-session-id": "sess-123"}
        if method == "initialize":
            return FakeResponse(
                {"jsonrpc": "2.0", "id": json["id"], "result": {"protocolVersion": "2025-06-18", "capabilities": {}}},
                headers=sess, sse=sse,
            )
        if method == "notifications/initialized":
            return FakeResponse({"ok": True}, headers=sess)
        if method == "tools/list":
            return FakeResponse(
                {"jsonrpc": "2.0", "id": json["id"], "result": {"tools": _TOOLS}}, headers=sess, sse=sse)
        if method == "tools/call":
            return FakeResponse(
                {"jsonrpc": "2.0", "id": json["id"], "result": _REPOS}, headers=sess, sse=sse)
        return FakeResponse({"jsonrpc": "2.0", "id": json.get("id"), "result": {}}, headers=sess)

    return _post


def _manager(post):
    def factory(config):
        return StreamableHTTPTransport(config, post=post)

    conn = MCPConnectionManager(factory, sleep=_nosleep)
    client = TransportMCPClient(conn)
    mgr = MCPRegistryManager(ToolRegistry(), client, spec_transform=github_spec_transform)
    config = build_github_mcp_server_config(token="ghp_SECRET", transport="http")
    run(mgr.register_server(config))
    return mgr


def test_http_initialize_discover_and_allowlist_over_real_transport():
    capture: list[dict] = []
    mgr = _manager(make_post(capture=capture))
    specs = run(mgr.discover_server_tools(GITHUB_MCP_SERVER_ID))
    ids = {s.id for s in specs}
    # Only allowlisted read tools registered; advertised writes blocked.
    for name in GITHUB_READ_ONLY_TOOLS:
        assert f"mcp.github.{name}" in ids
    for blocked in ("issue_write", "merge_pull_request", "push_files"):
        assert f"mcp.github.{blocked}" not in ids
    # The Authorization: Bearer header was sent on every request.
    assert capture and all(c["headers"].get("Authorization") == "Bearer ghp_SECRET" for c in capture)
    # initialize + initialized + tools/list happened.
    methods = [c["body"].get("method") for c in capture]
    assert "initialize" in methods and "notifications/initialized" in methods and "tools/list" in methods


def test_http_tool_call_returns_normalized_result_over_real_transport():
    from app.agent.github import github_result_normalizer
    from app.agent.tools.mcp_adapter import MCPAdapter

    mgr = _manager(make_post())
    run(mgr.discover_server_tools(GITHUB_MCP_SERVER_ID))
    adapter = MCPAdapter(mgr, result_normalizers={GITHUB_MCP_SERVER_ID: github_result_normalizer})
    result = run(adapter.execute(mgr.tool_registry.get("mcp.github.search_repositories"), {"query": "user:@me"}))
    assert result.success
    assert result.output["kind"] == "repositories"
    assert result.output["repositories"][0]["full_name"] == "u/runner-ai"


def test_http_sse_response_body_is_parsed():
    mgr = _manager(make_post(sse=True))
    specs = run(mgr.discover_server_tools(GITHUB_MCP_SERVER_ID))
    assert any(s.id == "mcp.github.list_issues" for s in specs)


def test_http_401_maps_to_auth_error_without_leaking_token():
    mgr = _manager(make_post(status_for={"initialize": 401}))
    try:
        run(mgr.discover_server_tools(GITHUB_MCP_SERVER_ID))
        raise AssertionError("expected an auth error")
    except TransportAuthenticationError as exc:
        assert "ghp_SECRET" not in str(exc)
        assert "ghp_SECRET" not in exc.safe_message


def test_http_403_maps_to_auth_error():
    mgr = _manager(make_post(status_for={"initialize": 403}))
    try:
        run(mgr.discover_server_tools(GITHUB_MCP_SERVER_ID))
        raise AssertionError("expected an auth error")
    except TransportAuthenticationError as exc:
        assert "ghp_SECRET" not in str(exc)


def test_http_500_maps_safely_without_leaking_token():
    from app.agent.mcp.errors import MCPError

    mgr = _manager(make_post(status_for={"initialize": 500}))
    try:
        run(mgr.discover_server_tools(GITHUB_MCP_SERVER_ID))
        raise AssertionError("expected a transport error")
    except MCPError as exc:
        assert "ghp_SECRET" not in str(exc)
        assert "ghp_SECRET" not in exc.safe_message
