"""Phase 39 tests — MCPAdapter execution + failure taxonomy.

Config-free: a FakeMCPClient backs the manager; the adapter runs an MCP-kind
ToolSpec and normalizes the result into an AdapterResult. Verifies argument
forwarding, provenance metadata, structured/textual normalization, that no
SDK-native objects surface, and the error taxonomy (unknown, timeout, invalid
args, remote error) with no raw exception leakage.
"""

import asyncio

from app.agent.mcp.client import FakeMCPClient
from app.agent.mcp.errors import MCPConnectionError, MCPToolInvocationError
from app.agent.mcp.models import MCPServerConfig, MCPToolCallResult, MCPToolDefinition, MCPTransport
from app.agent.mcp.registry import MCPRegistryManager
from app.agent.tools.mcp_adapter import MCPAdapter
from app.agent.tools.result import AdapterResult, ErrorCode
from app.agent.registry.registry import ToolRegistry


def run(coro):
    return asyncio.run(coro)


SECRET = "sk-vendor-SECRET-42"


def cfg(server_id="github", **kw):
    base = dict(server_id=server_id, name=server_id, transport=MCPTransport.STDIO,
                command=["srv"], timeout_seconds=5.0)
    base.update(kw)
    return MCPServerConfig(**base)


def tool(name="create_issue"):
    return MCPToolDefinition(name=name, description="d", input_schema={"type": "object"})


def wired(*, results=None, tools=None, server=None):
    server = server or cfg()
    reg = ToolRegistry()
    client = FakeMCPClient(tools=tools or {server.server_id: [tool()]}, results=results)
    mgr = MCPRegistryManager(reg, client)
    run(mgr.register_server(server))
    run(mgr.discover_server_tools(server.server_id))
    return MCPAdapter(mgr), mgr, reg, client


# --------------------------------------------------------------------------- #
# Success
# --------------------------------------------------------------------------- #

def test_known_capability_executes_and_forwards_args():
    adapter, mgr, reg, client = wired()
    spec = reg.get("mcp.github.create_issue")
    res = run(adapter.execute(spec, {"title": "Bug", "body": "x"}))
    assert res.success
    # arguments forwarded to the client verbatim
    assert client.call_tool_calls[-1] == ("github", "create_issue", {"title": "Bug", "body": "x"})


def test_success_result_is_adapter_result_with_provenance():
    adapter, mgr, reg, _ = wired(results={
        ("github", "create_issue"): MCPToolCallResult(
            success=True,
            content=[{"type": "text", "text": "Issue #5 created"}],
            structured_content={"number": 5},
        )
    })
    res = run(adapter.execute(reg.get("mcp.github.create_issue"), {"title": "x"}))
    assert isinstance(res, AdapterResult)
    assert res.metadata["adapter_type"] == "mcp"
    assert res.metadata["server_id"] == "github"
    assert res.metadata["tool_name"] == "create_issue"
    assert res.metadata["capability_id"] == "mcp.github.create_issue"
    assert isinstance(res.metadata["duration_ms"], (int, float))


def test_structured_and_textual_output_normalized():
    adapter, mgr, reg, _ = wired(results={
        ("github", "create_issue"): MCPToolCallResult(
            success=True,
            content=[{"type": "text", "text": "created"}],
            structured_content={"number": 7},
        )
    })
    res = run(adapter.execute(reg.get("mcp.github.create_issue"), {}))
    # textual → evidence; structured → output
    assert res.evidence and res.evidence[0].content == "created"
    assert res.evidence[0].source == "mcp:github:create_issue"
    assert res.output["structured_content"] == {"number": 7}
    assert res.output["content"] == [{"type": "text", "text": "created"}]


def test_result_contains_no_sdk_native_objects():
    # Everything on the AdapterResult must be plain JSON-friendly data.
    adapter, mgr, reg, _ = wired()
    res = run(adapter.execute(reg.get("mcp.github.create_issue"), {}))
    import json
    json.dumps(res.output)          # would raise if an SDK object leaked in
    json.dumps(res.metadata)


# --------------------------------------------------------------------------- #
# Failures
# --------------------------------------------------------------------------- #

def test_unknown_capability_is_safe_failure():
    adapter, mgr, reg, _ = wired()
    # a ToolSpec the manager has no binding for
    from app.agent.mcp.registry import convert_tool_definition
    orphan = convert_tool_definition(cfg("other"), tool("ghost"))
    res = run(adapter.execute(orphan, {}))
    assert not res.success
    assert res.error_code == ErrorCode.UNKNOWN_CAPABILITY
    assert res.retryable is False


def test_timeout_is_retryable():
    adapter, mgr, reg, _ = wired(
        results={("github", "create_issue"): _AsyncRaiser(asyncio.TimeoutError())},
        server=cfg(timeout_seconds=5.0),
    )
    res = run(adapter.execute(reg.get("mcp.github.create_issue"), {}))
    assert not res.success
    assert res.error_code == ErrorCode.UPSTREAM_TIMEOUT
    assert res.retryable is True


def test_real_timeout_via_wait_for_is_retryable():
    # A genuinely slow tool trips asyncio.wait_for against a tiny server timeout.
    async def slow(_args):
        await asyncio.sleep(0.2)
        return MCPToolCallResult()

    adapter, mgr, reg, _ = wired(
        results={("github", "create_issue"): slow},
        server=cfg(timeout_seconds=0.01),
    )
    res = run(adapter.execute(reg.get("mcp.github.create_issue"), {}))
    assert not res.success
    assert res.error_code == ErrorCode.UPSTREAM_TIMEOUT
    assert res.retryable is True


def test_connection_error_is_retryable():
    adapter, mgr, reg, _ = wired(
        results={("github", "create_issue"): MCPConnectionError("down")})
    res = run(adapter.execute(reg.get("mcp.github.create_issue"), {}))
    assert not res.success
    assert res.error_code == ErrorCode.UPSTREAM_UNAVAILABLE
    assert res.retryable is True


def test_remote_tool_error_is_non_retryable_and_safe():
    adapter, mgr, reg, _ = wired(results={
        ("github", "create_issue"): MCPToolCallResult(success=False, is_error=True,
                                                      content=[{"type": "text", "text": SECRET}])
    })
    res = run(adapter.execute(reg.get("mcp.github.create_issue"), {}))
    assert not res.success
    assert res.retryable is False
    # remote error content is not surfaced as evidence/output; only a safe message
    assert SECRET not in str(res.model_dump())
    assert res.metadata["safe_message"] == MCPToolInvocationError.safe_message


def test_raw_exception_does_not_leak():
    adapter, mgr, reg, _ = wired(
        results={("github", "create_issue"): RuntimeError(SECRET)})
    res = run(adapter.execute(reg.get("mcp.github.create_issue"), {}))
    assert not res.success
    assert res.error_code == ErrorCode.UPSTREAM_ERROR
    assert SECRET not in str(res.model_dump())  # raw exception text never leaks


def test_disabled_server_is_non_retryable_failure():
    reg = ToolRegistry()
    client = FakeMCPClient(tools={"github": [tool()]})
    mgr = MCPRegistryManager(reg, client)
    run(mgr.register_server(cfg("github")))
    run(mgr.discover_server_tools("github"))
    # disable the server after discovery
    mgr._servers["github"] = cfg("github", enabled=False)
    res = run(MCPAdapter(mgr).execute(reg.get("mcp.github.create_issue"), {}))
    assert not res.success
    assert res.retryable is False


class _AsyncRaiser:
    """A results-spec callable that raises the given exception when invoked."""

    def __init__(self, exc):
        self._exc = exc

    def __call__(self, _args):
        raise self._exc
