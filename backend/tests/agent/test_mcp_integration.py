"""Phase 39 tests — MCP end-to-end integration + regression + hygiene.

Config-free. Verifies discovered MCP tools participate in the EXISTING hybrid
capability retrieval (no separate path), execute through the runtime Execution
Bridge into an AdapterResult on RunContext, that the default runtime without MCP
is unchanged, lifecycle/sharing, and that no vendor MCP SDK is imported anywhere
in app.agent and no connection happens at import time.
"""

import ast
import asyncio
import pathlib

from app.agent.capabilities.keyword_retriever import KeywordCapabilityRetriever
from app.agent.capabilities.models import CapabilityRetrievalRequest
from app.agent.mcp.client import FakeMCPClient
from app.agent.mcp.models import MCPServerConfig, MCPToolDefinition, MCPTransport
from app.agent.mcp.registry import MCPRegistryManager
from app.agent.models.tool_spec import ToolKind
from app.agent.registry.loader import get_default_tool_registry
from app.agent.retriever.capability_retriever import HybridCapabilityRetriever
from app.agent.runtime.context import BehaviorPath, BehaviorProfile, RunContext
from app.agent.runtime.direct_runtime import DirectRuntime
from app.agent.runtime.factory import (
    CompositeCapabilityExecutor,
    InternalCapabilityExecutor,
    build_default_runtime,
)
from app.agent.tools.mcp_adapter import MCPAdapter


def run(coro):
    return asyncio.run(coro)


def cfg(server_id="github", **kw):
    base = dict(server_id=server_id, name=server_id, transport=MCPTransport.STDIO, command=["srv"])
    base.update(kw)
    return MCPServerConfig(**base)


def gh_tool():
    return MCPToolDefinition(
        name="create_github_issue",
        description="Create a GitHub issue on a repository",
        input_schema={"type": "object", "properties": {"title": {"type": "string"}}},
    )


def wired_registry():
    """Default internal registry + one discovered MCP tool, sharing one manager."""
    reg = get_default_tool_registry()
    client = FakeMCPClient(tools={"github": [gh_tool()]})
    mgr = MCPRegistryManager(reg, client)
    run(mgr.register_server(cfg("github")))
    run(mgr.discover_server_tools("github"))
    return reg, mgr, client


# --------------------------------------------------------------------------- #
# Capability retrieval participation (existing hybrid pipeline)
# --------------------------------------------------------------------------- #

def test_mcp_tools_participate_in_hybrid_retrieval():
    reg, mgr, _ = wired_registry()
    retriever = HybridCapabilityRetriever(KeywordCapabilityRetriever(reg))
    resp = retriever.retrieve(CapabilityRetrievalRequest(query="create a github issue", top_k=5))
    ids = [m.tool.id for m in resp.matches]
    assert "mcp.github.create_github_issue" in ids
    # it ranks top for a clearly-MCP request, alongside internal tools in one view
    assert resp.matches[0].tool.kind == ToolKind.MCP


def test_retrieval_is_bounded_by_top_k_not_full_catalog():
    # Many MCP tools, but only top_k reach the caller — no full-catalog dump.
    reg = get_default_tool_registry()
    tools = [MCPToolDefinition(name=f"tool_{i}", description=f"issue helper {i}",
                               input_schema={"type": "object"}) for i in range(20)]
    client = FakeMCPClient(tools={"github": tools})
    mgr = MCPRegistryManager(reg, client)
    run(mgr.register_server(cfg("github")))
    run(mgr.discover_server_tools("github"))
    retriever = HybridCapabilityRetriever(KeywordCapabilityRetriever(reg))
    resp = retriever.retrieve(CapabilityRetrievalRequest(query="issue helper", top_k=3))
    assert len(resp.matches) == 3  # bounded, not 20+internal


# --------------------------------------------------------------------------- #
# Execution through the runtime Execution Bridge
# --------------------------------------------------------------------------- #

def test_mcp_capability_executes_through_direct_runtime():
    reg, mgr, client = wired_registry()
    retriever = HybridCapabilityRetriever(KeywordCapabilityRetriever(reg))
    executor = CompositeCapabilityExecutor({
        ToolKind.INTERNAL: InternalCapabilityExecutor(),
        ToolKind.MCP: MCPAdapter(mgr),
    })
    direct = DirectRuntime(retriever, executor)

    rc = RunContext.create("create a github issue about pricing", user_id="u")
    rc.attach_behavior_profile(BehaviorProfile(path=BehaviorPath.DIRECT, reason="t", confidence=1.0))
    run(direct.run(rc))

    assert rc.metadata["direct_runtime"]["capability_id"] == "mcp.github.create_github_issue"
    assert rc.metadata["execution_status"] == "success"
    # the MCP call was actually made and its provenance recorded
    assert client.call_tool_calls[-1][0] == "github"
    assert rc.tool_outputs[-1].capability_id == "mcp.github.create_github_issue"


def test_composite_executor_routes_by_kind():
    reg, mgr, _ = wired_registry()
    executor = CompositeCapabilityExecutor({
        ToolKind.INTERNAL: InternalCapabilityExecutor(),
        ToolKind.MCP: MCPAdapter(mgr),
    })
    mcp_res = run(executor.execute(reg.get("mcp.github.create_github_issue"), {}))
    assert mcp_res.metadata["adapter_type"] == "mcp"
    # an internal tool with no injected fake returns a normal (failure) AdapterResult,
    # NOT an MCP one — proving routing by kind.
    internal_res = run(executor.execute(reg.get("search_documents"), {"query": "x", "user_id": "u"}))
    assert internal_res.metadata.get("adapter_type") != "mcp"


# --------------------------------------------------------------------------- #
# Factory seam + default-runtime regression
# --------------------------------------------------------------------------- #

def test_factory_with_mcp_manager_uses_composite_and_shared_registry():
    reg, mgr, _ = wired_registry()
    orch = build_default_runtime(mcp_registry_manager=mgr)
    assert isinstance(orch._direct_runtime._executor, CompositeCapabilityExecutor)
    # runtime retriever sees the manager's shared registry (MCP tool retrievable)
    resp = orch._capability_retriever.retrieve(
        CapabilityRetrievalRequest(query="create a github issue", top_k=5))
    assert any(m.tool.id == "mcp.github.create_github_issue" for m in resp.matches)


def test_default_runtime_without_mcp_is_unchanged():
    orch = build_default_runtime()
    assert isinstance(orch._direct_runtime._executor, InternalCapabilityExecutor)


# --------------------------------------------------------------------------- #
# Lifecycle + sharing
# --------------------------------------------------------------------------- #

def test_single_client_shared_across_servers_and_tools():
    reg = get_default_tool_registry()
    client = FakeMCPClient(tools={
        "github": [gh_tool()],
        "fs": [MCPToolDefinition(name="read_file", input_schema={"type": "object"})],
    })
    mgr = MCPRegistryManager(reg, client)
    run(mgr.register_server(cfg("github")))
    run(mgr.register_server(cfg("fs")))
    run(mgr.discover_server_tools("github"))
    run(mgr.discover_server_tools("fs"))
    assert reg.exists("mcp.github.create_github_issue")
    assert reg.exists("mcp.fs.read_file")
    assert mgr.client is client  # one shared client, not one per tool


def test_close_closes_all_sessions():
    reg, mgr, client = wired_registry()
    run(mgr.close())
    assert "github" in client.closed


def test_no_connection_at_registration_time():
    reg = get_default_tool_registry()
    client = FakeMCPClient(tools={"github": [gh_tool()]})
    mgr = MCPRegistryManager(reg, client)
    run(mgr.register_server(cfg("github")))
    # registering a server must not open a connection; that happens at discovery
    assert client.connect_calls == []


# --------------------------------------------------------------------------- #
# Hygiene — no vendor MCP SDK imports, config-free
# --------------------------------------------------------------------------- #

def _import_targets(path: pathlib.Path):
    tree = ast.parse(path.read_text())
    targets = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            targets.append(node.module or "")
    return targets


def test_no_vendor_mcp_sdk_imported_in_agent():
    agent_root = pathlib.Path(__file__).resolve().parents[2] / "app" / "agent"
    for path in agent_root.rglob("*.py"):
        for target in _import_targets(path):
            # Our own package is app.agent.mcp; a *bare* top-level `mcp` (or
            # `mcp.<x>`) would be a vendor SDK import — forbidden for this phase.
            assert target != "mcp" and not target.startswith("mcp."), (path, target)


def test_mcp_modules_are_config_free_at_import():
    # None of the MCP modules import application settings or the database.
    import app.agent.mcp.client as client_mod
    import app.agent.mcp.models as models_mod
    import app.agent.mcp.registry as registry_mod
    import app.agent.tools.mcp_adapter as adapter_mod
    for mod in (client_mod, models_mod, registry_mod, adapter_mod):
        for target in _import_targets(pathlib.Path(mod.__file__)):
            for banned in ("app.config", "app.database", "app.services"):
                assert banned not in target, (mod.__name__, target)
