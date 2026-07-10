"""Phase 41A tests — transport end-to-end + composition + runtime unchanged.

Config-free. Drives the real MCPRegistryManager / MCPAdapter / factory over a
TransportMCPClient backed by FakeTransports (no live server, no SDK). Verifies
discovery + execution through the transport stack, the composition helper +
lifecycle, safe transport-error → AdapterResult mapping, config surface, and that
the runtime/planner/retrieval/execution wiring is unchanged.
"""

import asyncio

from app.agent.capabilities.models import CapabilityRetrievalRequest
from app.agent.execution.capability_executor import (
    CompositeCapabilityExecutor,
    InternalCapabilityExecutor,
)
from app.agent.llm.planner_provider import DeterministicPlannerProvider
from app.agent.mcp.composition import build_mcp_registry_manager, default_transport_factory
from app.agent.mcp.connection import MCPConnectionManager, TransportMCPClient
from app.agent.mcp.models import MCPServerConfig, MCPToolCallResult, MCPToolDefinition, MCPTransport
from app.agent.mcp.registry import MCPRegistryManager
from app.agent.mcp.transport import FakeTransport
from app.agent.models.tool_spec import ToolKind
from app.agent.registry.registry import ToolRegistry
from app.agent.retriever.capability_retriever import HybridCapabilityRetriever
from app.agent.runtime.direct_runtime import DirectRuntime
from app.agent.runtime.factory import build_default_runtime
from app.agent.runtime.planner_runtime import PlannerRuntime
from app.agent.tools.mcp_adapter import MCPAdapter


def run(coro):
    return asyncio.run(coro)


async def _nosleep(_s):
    return None


def cfg(sid="github", **kw):
    base = dict(server_id=sid, name=sid, transport=MCPTransport.STDIO, command=["srv"])
    base.update(kw)
    return MCPServerConfig(**base)


def gh_tool(name="create_github_issue"):
    return MCPToolDefinition(name=name, description=f"github {name}", input_schema={"type": "object"})


def fake_factory(tools_by_server):
    def factory(config):
        return FakeTransport(config, tools=tools_by_server.get(config.server_id, []))
    return factory


def transport_client(tools_by_server):
    mgr = MCPConnectionManager(fake_factory(tools_by_server), sleep=_nosleep)
    return TransportMCPClient(mgr), mgr


# --------------------------------------------------------------------------- #
# Discovery + execution through the transport stack
# --------------------------------------------------------------------------- #

def test_discovery_and_execution_via_transport_client():
    client, conn = transport_client({"github": [gh_tool()]})
    reg = ToolRegistry()
    manager = MCPRegistryManager(reg, client)

    async def go():
        await manager.register_server(cfg("github"))
        specs = await manager.discover_server_tools("github")
        adapter = MCPAdapter(manager)
        result = await adapter.execute(reg.get("mcp.github.create_github_issue"), {"title": "x"})
        return specs, result

    specs, result = run(go())
    assert [s.id for s in specs] == ["mcp.github.create_github_issue"]
    assert result.success
    assert result.metadata["adapter_type"] == "mcp"
    # one pooled transport session serviced discovery + execution
    assert conn.stats()["pooled"] == 1


# --------------------------------------------------------------------------- #
# Composition helper + lifecycle ownership
# --------------------------------------------------------------------------- #

def test_composition_helper_builds_and_discovers():
    factory = fake_factory({"github": [gh_tool()], "filesystem": [gh_tool("read_file")]})

    async def go():
        manager, conn = await build_mcp_registry_manager(
            [cfg("github"), cfg("filesystem")], transport_factory=factory, sleep=_nosleep,
        )
        ids = sorted(manager.list_discovered_tools())
        await conn.shutdown()
        return ids, conn

    ids, conn = run(go())
    assert ids == ["mcp.filesystem.read_file", "mcp.github.create_github_issue"]
    assert conn.stats()["pooled"] == 0  # shutdown closed every session


def test_disabled_server_is_not_mounted():
    factory = fake_factory({"github": [gh_tool()]})

    async def go():
        manager, conn = await build_mcp_registry_manager(
            [cfg("github", enabled=False)], transport_factory=factory, sleep=_nosleep,
        )
        return manager.list_discovered_tools()

    assert run(go()) == []


# --------------------------------------------------------------------------- #
# Factory composition — runtime unchanged
# --------------------------------------------------------------------------- #

def _runtime_with_transport_mcp():
    factory = fake_factory({"github": [gh_tool()]})

    async def build():
        manager, conn = await build_mcp_registry_manager(
            [cfg("github")], transport_factory=factory, sleep=_nosleep,
        )
        return build_default_runtime(mcp_registry_manager=manager), conn

    return run(build())


def test_transport_mcp_runtime_is_composed_and_unchanged():
    orch, conn = _runtime_with_transport_mcp()
    # execution bridge routes by kind (internal + MCP), runtime otherwise unchanged
    assert isinstance(orch._direct_runtime._executor, CompositeCapabilityExecutor)
    assert isinstance(orch._direct_runtime, DirectRuntime)
    assert isinstance(orch._planner_runtime, PlannerRuntime)
    assert orch._planner_runtime._direct is orch._direct_runtime
    assert isinstance(orch._planner_provider, DeterministicPlannerProvider)
    assert isinstance(orch._capability_retriever, HybridCapabilityRetriever)
    # MCP tool discovered via the real transport is retrievable in the one view
    resp = orch._capability_retriever.retrieve(
        CapabilityRetrievalRequest(query="create a github issue", top_k=8))
    assert any(m.tool.id == "mcp.github.create_github_issue" for m in resp.matches)


def test_default_runtime_without_mcp_still_internal_only():
    orch = build_default_runtime()
    assert isinstance(orch._direct_runtime._executor, InternalCapabilityExecutor)


# --------------------------------------------------------------------------- #
# Safe transport-error → AdapterResult mapping (no leak)
# --------------------------------------------------------------------------- #

SECRET = "sk-transport-SECRET"


def test_transport_failure_maps_to_safe_adapter_result():
    from app.agent.mcp.errors import TransportConnectionLost

    tools = {"github": [gh_tool()]}

    def factory(config):
        # a transport whose call raises a transport error carrying a secret
        return FakeTransport(config, tools=tools["github"],
                             results={"create_github_issue": TransportConnectionLost(SECRET)})

    conn = MCPConnectionManager(factory, sleep=_nosleep)
    client = TransportMCPClient(conn)
    reg = ToolRegistry()
    manager = MCPRegistryManager(reg, client)

    async def go():
        await manager.register_server(cfg("github"))
        await manager.discover_server_tools("github")
        adapter = MCPAdapter(manager)
        return await adapter.execute(reg.get("mcp.github.create_github_issue"), {})

    result = run(go())
    assert not result.success
    assert result.retryable is True                       # connection-lost is retryable
    assert result.metadata["mcp_error_code"] == "mcp_transport_connection_lost"
    assert SECRET not in str(result.model_dump())         # raw transport text never leaks


# --------------------------------------------------------------------------- #
# Config surface + security
# --------------------------------------------------------------------------- #

def test_config_supports_working_directory_and_retry_without_leaking_secrets():
    from app.agent.mcp.models import MCPRetryConfig

    c = cfg("github", working_directory="/srv/app", environment={"TOKEN": SECRET},
            headers={"Authorization": SECRET}, retry=MCPRetryConfig(max_attempts=3))
    assert c.working_directory == "/srv/app"
    assert c.retry.max_attempts == 3
    # secrets and working_directory stay out of the public/observability view
    meta = c.public_metadata()
    assert SECRET not in str(meta)
    assert "working_directory" not in meta
    assert SECRET not in repr(c)


def test_transport_client_satisfies_mcp_client_protocol():
    from app.agent.mcp.client import MCPClient

    client, _ = transport_client({"github": [gh_tool()]})
    assert isinstance(client, MCPClient)  # runtime-checkable Protocol


def test_default_transport_factory_selects_by_transport():
    factory = default_transport_factory()
    from app.agent.mcp.transports.http import StreamableHTTPTransport
    from app.agent.mcp.transports.stdio import StdioTransport

    stdio = factory(cfg("a"))
    http = factory(MCPServerConfig(server_id="b", name="b",
                                   transport=MCPTransport.STREAMABLE_HTTP, url="https://x"))
    assert isinstance(stdio, StdioTransport)
    assert isinstance(http, StreamableHTTPTransport)
