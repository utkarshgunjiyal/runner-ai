"""Phase 41A tests — MCPConnectionManager + TransportMCPClient.

Config-free: FakeTransport instances behind an injected factory, a fake clock and
a no-op sleep. Verifies lazy connect, session reuse (no transport-per-request),
reconnect with bounded retry, idle recycle, disconnect, graceful shutdown,
multi-server isolation, health/stats, and the MCPClient adapter surface.
"""

import asyncio

import pytest

from app.agent.mcp.connection import MCPConnectionManager, TransportMCPClient
from app.agent.mcp.errors import TransportUnavailable
from app.agent.mcp.models import MCPRetryConfig, MCPServerConfig, MCPToolDefinition, MCPTransport
from app.agent.mcp.transport import FakeTransport, HealthStatus


def run(coro):
    return asyncio.run(coro)


async def _nosleep(_seconds):
    return None


def cfg(sid="github", **kw):
    base = dict(server_id=sid, name=sid, transport=MCPTransport.STDIO, command=["srv"])
    base.update(kw)
    return MCPServerConfig(**base)


def tool(name="echo"):
    return MCPToolDefinition(name=name, input_schema={"type": "object"})


class RecordingFactory:
    """Factory that records how many transports it built per server."""

    def __init__(self, *, tools=None, clock=None, fail_connect=0):
        self._tools = tools or [tool()]
        self._clock = clock or (lambda: 0.0)
        self._fail_connect = fail_connect
        self.built: list[str] = []
        self.transports: dict[str, FakeTransport] = {}

    def __call__(self, config):
        self.built.append(config.server_id)
        t = FakeTransport(config, tools=self._tools, clock=self._clock,
                          fail_connect=self._fail_connect)
        self.transports[config.server_id] = t
        return t


def manager(factory, *, clock=None, idle_timeout=None):
    return MCPConnectionManager(factory, clock=clock, sleep=_nosleep, idle_timeout=idle_timeout)


# --------------------------------------------------------------------------- #
# Lazy connect + reuse (no transport per request)
# --------------------------------------------------------------------------- #

def test_lazy_connect_and_session_reuse():
    factory = RecordingFactory()
    mgr = manager(factory)

    async def go():
        c = cfg("github")
        t1 = await mgr.acquire(c)
        t2 = await mgr.acquire(c)
        t3 = await mgr.acquire(c)
        return t1, t2, t3

    t1, t2, t3 = run(go())
    assert t1 is t2 is t3                       # one pooled session reused
    assert factory.built == ["github"]          # one transport ever built
    assert t1.connect_calls == 1                # connected once, reused after
    assert mgr.stats()["connects"] == 1
    assert mgr.stats()["reuses"] == 2
    assert mgr.stats()["active_sessions"] == 1


def test_no_transport_per_request_across_calls():
    factory = RecordingFactory()
    mgr = manager(factory)
    client = TransportMCPClient(mgr)

    async def go():
        c = cfg("github")
        await client.list_tools(c)
        await client.call_tool(c, "echo", {})
        await client.call_tool(c, "echo", {})

    run(go())
    assert factory.built == ["github"]  # still exactly one transport


# --------------------------------------------------------------------------- #
# Reconnect + retry policy
# --------------------------------------------------------------------------- #

def test_reconnect_after_connection_lost():
    factory = RecordingFactory()
    mgr = manager(factory)
    client = TransportMCPClient(mgr)

    async def go():
        c = cfg("github")
        await client.call_tool(c, "echo", {})     # connect #1
        t = factory.transports["github"]
        t._fail_calls = 1                          # next call drops the session
        with pytest.raises(Exception):
            await client.call_tool(c, "echo", {})  # fails, marks disconnected
        assert not t.is_connected
        await client.call_tool(c, "echo", {})      # manager reconnects same transport
        return t

    t = run(go())
    assert t.connect_calls == 2                     # reconnected, not rebuilt
    assert factory.built == ["github"]
    assert mgr.stats()["failed_reconnects"] == 0    # reconnect succeeded


def test_connect_retries_then_succeeds():
    # First connect attempt fails, second succeeds (retry policy max_attempts=2).
    factory = RecordingFactory(fail_connect=1)
    mgr = manager(factory)

    async def go():
        return await mgr.acquire(cfg("github", retry=MCPRetryConfig(max_attempts=2, base_delay_seconds=0)))

    t = run(go())
    assert t.is_connected
    assert mgr.stats()["connect_attempts"] == 2


def test_connect_exhausts_retries_and_raises():
    factory = RecordingFactory(fail_connect=5)
    mgr = manager(factory)

    async def go():
        await mgr.acquire(cfg("github", retry=MCPRetryConfig(max_attempts=2, base_delay_seconds=0)))

    with pytest.raises(TransportUnavailable):
        run(go())


# --------------------------------------------------------------------------- #
# Idle recycle + disconnect + shutdown
# --------------------------------------------------------------------------- #

def test_idle_recycle_reconnects_new_session():
    clk = [0.0]
    factory = RecordingFactory(clock=lambda: clk[0])
    mgr = manager(factory, clock=lambda: clk[0], idle_timeout=10)

    async def go():
        c = cfg("github")
        await mgr.acquire(c)
        clk[0] = 100.0            # exceed idle timeout
        await mgr.acquire(c)      # recycle → new transport built + connected

    run(go())
    assert factory.built == ["github", "github"]   # rebuilt after idle
    assert mgr.stats()["idle_recycles"] == 1


def test_disconnect_closes_and_removes():
    factory = RecordingFactory()
    mgr = manager(factory)

    async def go():
        c = cfg("github")
        await mgr.acquire(c)
        t = factory.transports["github"]
        await mgr.disconnect("github")
        return t

    t = run(go())
    assert t.close_calls == 1
    assert mgr.stats()["pooled"] == 0


def test_shutdown_closes_all_sessions():
    factory = RecordingFactory()
    mgr = manager(factory)

    async def go():
        await mgr.acquire(cfg("github"))
        await mgr.acquire(cfg("filesystem"))
        await mgr.shutdown()

    run(go())
    assert factory.transports["github"].close_calls == 1
    assert factory.transports["filesystem"].close_calls == 1
    assert mgr.stats()["pooled"] == 0


# --------------------------------------------------------------------------- #
# Multiple servers + health/stats
# --------------------------------------------------------------------------- #

def test_multiple_servers_are_isolated():
    factory = RecordingFactory()
    mgr = manager(factory)

    async def go():
        await mgr.acquire(cfg("github"))
        await mgr.acquire(cfg("filesystem"))

    run(go())
    assert sorted(factory.transports) == ["filesystem", "github"]
    assert mgr.stats()["pooled"] == 2
    assert set(mgr.all_health()) == {"filesystem", "github"}


def test_health_reported_per_server():
    factory = RecordingFactory()
    mgr = manager(factory)

    async def go():
        await mgr.acquire(cfg("github"))

    run(go())
    assert mgr.health("github").status == HealthStatus.HEALTHY
    assert mgr.health("missing") is None
    snap = mgr.all_health()["github"]
    assert snap["status"] == "healthy"
