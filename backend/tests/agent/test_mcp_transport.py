"""Phase 41A tests — MCP transport abstraction + concrete transports + health.

Config-free. Drives FakeTransport (contract + health transitions), the real
StdioTransport over an in-memory fake process, and the real StreamableHTTPTransport
over an injected fake POST — exercising the genuine JSON-RPC protocol path with no
live server and no SDK.
"""

import asyncio
import json

import pytest

from app.agent.mcp.errors import (
    TransportAuthenticationError,
    TransportConnectionLost,
    TransportProtocolError,
    TransportUnavailable,
)
from app.agent.mcp.models import MCPServerConfig, MCPTransport, MCPToolCallResult
from app.agent.mcp.transport import FakeTransport, HealthStatus, MCPTransport as TransportABC
from app.agent.mcp.transports.http import StreamableHTTPTransport
from app.agent.mcp.transports.stdio import StdioTransport


def run(coro):
    return asyncio.run(coro)


def stdio_cfg(sid="s"):
    return MCPServerConfig(server_id=sid, name=sid, transport=MCPTransport.STDIO, command=["srv"])


def http_cfg(sid="h"):
    return MCPServerConfig(server_id=sid, name=sid, transport=MCPTransport.STREAMABLE_HTTP,
                           url="https://mcp.example/x")


# --------------------------------------------------------------------------- #
# FakeTransport contract + health transitions
# --------------------------------------------------------------------------- #

def test_fake_transport_is_a_transport():
    assert isinstance(FakeTransport(stdio_cfg()), TransportABC)


def test_health_starts_offline_then_healthy():
    clk = [0.0]
    t = FakeTransport(stdio_cfg(), clock=lambda: clk[0])
    assert t.health().status == HealthStatus.OFFLINE
    run(t.connect())
    assert t.health().status == HealthStatus.HEALTHY
    assert t.health().last_success == 0.0


def test_server_health_state_machine():
    from app.agent.mcp.transport import ServerHealth

    h = ServerHealth()  # degrade_after=1, offline_after=3
    assert h.status == HealthStatus.OFFLINE
    h.record_success(1.0)
    assert h.status == HealthStatus.HEALTHY and h.last_success == 1.0
    h.record_failure(2.0)          # 1 consecutive → DEGRADED
    assert h.status == HealthStatus.DEGRADED and h.last_failure == 2.0
    h.record_failure(3.0)          # 2 consecutive → still DEGRADED
    assert h.status == HealthStatus.DEGRADED
    h.record_failure(4.0)          # 3 consecutive → OFFLINE
    assert h.status == HealthStatus.OFFLINE and h.consecutive_failures == 3
    h.record_success(5.0)          # recovery resets
    assert h.status == HealthStatus.HEALTHY and h.consecutive_failures == 0


def test_fake_transport_call_failure_degrades_health():
    clk = [0.0]
    t = FakeTransport(stdio_cfg(), clock=lambda: clk[0], fail_calls=1)
    run(t.connect())
    clk[0] = 1.0
    with pytest.raises(Exception):
        run(t.call_tool("x", {}))
    assert t.health().status == HealthStatus.DEGRADED
    assert t.health().last_failure == 1.0


def test_health_snapshot_is_secret_free():
    t = FakeTransport(stdio_cfg())
    run(t.connect())
    snap = t.health().snapshot()
    assert set(snap) == {"status", "last_success", "last_failure", "last_ping", "consecutive_failures"}


# --------------------------------------------------------------------------- #
# Real StdioTransport over an in-memory fake process (JSON-RPC)
# --------------------------------------------------------------------------- #

class _FakeStdin:
    def __init__(self, proc): self._proc = proc
    def write(self, b): self._proc.inbox.append(b.decode())
    async def drain(self): pass
    def close(self): self._proc.stdin_closed = True


class _FakeStdout:
    def __init__(self, proc): self._proc = proc

    async def readline(self):
        while self._proc.inbox:
            msg = json.loads(self._proc.inbox.pop(0))
            if "id" not in msg:  # a notification — no response
                continue
            rid, method = msg["id"], msg["method"]
            result = self._proc.responder(method)
            return (json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\n").encode()
        return b""  # stream closed


class FakeProcess:
    def __init__(self, responder):
        self.inbox = []
        self.responder = responder
        self.stdin = _FakeStdin(self)
        self.stdout = _FakeStdout(self)
        self.returncode = 0
        self.terminated = False
        self.stdin_closed = False

    def terminate(self): self.terminated = True
    async def wait(self): return 0


def _responder(method):
    return {
        "initialize": {"protocolVersion": "2025-06-18"},
        "tools/list": {"tools": [{"name": "echo", "description": "e", "inputSchema": {"type": "object"}}]},
        "tools/call": {"content": [{"type": "text", "text": "hi"}], "structuredContent": {"ok": True}},
    }.get(method, {})


def test_stdio_transport_full_protocol():
    proc = FakeProcess(_responder)

    async def spawn(cfg):
        return proc

    t = StdioTransport(stdio_cfg(), spawn=spawn)
    run(t.connect())
    assert t.is_connected
    tools = run(t.list_tools())
    assert [x.name for x in tools] == ["echo"]
    result = run(t.call_tool("echo", {"a": 1}))
    assert isinstance(result, MCPToolCallResult)
    assert result.success and result.structured_content == {"ok": True}
    run(t.close())
    assert proc.terminated


class _ClosedStdout:
    async def readline(self):
        return b""  # stream already closed


class ClosedProcess(FakeProcess):
    def __init__(self):
        super().__init__(lambda m: {})
        self.stdout = _ClosedStdout()


def test_stdio_transport_stream_closed_is_connection_lost():
    async def spawn(cfg):
        return ClosedProcess()

    # readline returns b"" during the initialize handshake → connect wraps the
    # closed stream as a domain TransportUnavailable (no raw leak).
    t = StdioTransport(stdio_cfg(), spawn=spawn)
    with pytest.raises(TransportConnectionLost):
        run(t.connect())


# --------------------------------------------------------------------------- #
# Real StreamableHTTPTransport over an injected fake POST (JSON-RPC)
# --------------------------------------------------------------------------- #

class FakeResponse:
    def __init__(self, body=None, *, status=200, headers=None, text=None):
        self._body = body
        self.status_code = status
        self.headers = headers or {"content-type": "application/json"}
        self.text = text if text is not None else json.dumps(body)

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def _http_responder(session_header="sess-1"):
    calls = []

    async def post(url, *, headers, json, timeout):
        calls.append({"method": json.get("method"), "session": headers.get("mcp-session-id")})
        method = json.get("method")
        if "id" not in json:  # notification
            return FakeResponse({}, status=202)
        rid = json["id"]
        headers_out = {"content-type": "application/json"}
        if method == "initialize":
            headers_out["mcp-session-id"] = session_header
        return FakeResponse({"jsonrpc": "2.0", "id": rid, "result": _responder(method)}, headers=headers_out)

    return post, calls


def test_http_transport_full_protocol_and_session():
    post, calls = _http_responder("sess-1")
    t = StreamableHTTPTransport(http_cfg(), post=post)
    run(t.connect())
    assert t.is_connected
    assert [x.name for x in run(t.list_tools())] == ["echo"]
    assert run(t.call_tool("echo", {})).success
    # session id captured from initialize and echoed on later requests
    assert calls[-1]["session"] == "sess-1"
    run(t.close())


def test_http_transport_auth_error():
    async def post(url, *, headers, json, timeout):
        return FakeResponse(status=401, text="nope")

    t = StreamableHTTPTransport(http_cfg(), post=post)
    with pytest.raises(TransportAuthenticationError):
        run(t.connect())  # auth failure surfaces as a safe domain error (no leak)


def test_http_transport_malformed_body_is_protocol_error():
    async def post(url, *, headers, json, timeout):
        return FakeResponse(text="not json", headers={"content-type": "application/json"}, status=200)
    # body=None → .json() raises → TransportProtocolError inside connect → wrapped unavailable
    t = StreamableHTTPTransport(http_cfg(), post=post)
    with pytest.raises((TransportProtocolError, TransportUnavailable)):
        run(t.connect())
