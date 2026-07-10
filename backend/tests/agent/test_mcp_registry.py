"""Phase 39 tests — MCP discovery, conversion, registration, isolation.

Config-free: a deterministic FakeMCPClient feeds the manager; discovered tools
become normalized ToolSpecs in a shared ToolRegistry. Verifies stable ids,
metadata preservation, duplicate/collision rejection, safe refresh, server
namespace isolation, and that MCP can never overwrite an internal capability.
"""

import asyncio

import pytest

from app.agent.mcp.client import FakeMCPClient
from app.agent.mcp.errors import MCPProtocolError, MCPServerNotFoundError
from app.agent.mcp.models import MCPServerConfig, MCPToolDefinition, MCPTransport
from app.agent.mcp.registry import MCPRegistryManager, convert_tool_definition
from app.agent.models.tool_spec import ToolKind
from app.agent.registry.loader import get_default_tool_registry
from app.agent.registry.registry import ToolRegistry


def run(coro):
    return asyncio.run(coro)


def cfg(server_id="github", **kw):
    base = dict(server_id=server_id, name=server_id, transport=MCPTransport.STDIO,
                command=["srv"])
    base.update(kw)
    return MCPServerConfig(**base)


def tool(name, description="do a thing", schema=None):
    return MCPToolDefinition(name=name, description=description,
                             input_schema=schema or {"type": "object"})


def manager(tools, registry=None):
    registry = registry or ToolRegistry()
    client = FakeMCPClient(tools=tools)
    return MCPRegistryManager(registry, client), registry, client


# --------------------------------------------------------------------------- #
# Discovery + conversion
# --------------------------------------------------------------------------- #

def test_discovery_registers_stable_capability_ids():
    mgr, reg, _ = manager({"github": [tool("create_issue"), tool("list_repos")]})
    run(mgr.register_server(cfg("github")))
    specs = run(mgr.discover_server_tools("github"))
    ids = sorted(s.id for s in specs)
    assert ids == ["mcp.github.create_issue", "mcp.github.list_repos"]
    assert reg.exists("mcp.github.create_issue")


def test_conversion_preserves_description_and_schema():
    schema = {"type": "object", "properties": {"title": {"type": "string"}},
              "required": ["title"]}
    spec = convert_tool_definition(cfg("github"), tool("create_issue", "Open an issue", schema))
    assert spec.kind == ToolKind.MCP
    assert spec.description == "Open an issue"
    assert spec.input_schema == schema
    assert spec.id == "mcp.github.create_issue"
    assert spec.handler_ref == "mcp:github:create_issue"


def test_conversion_carries_no_secrets():
    server = cfg("github", environment={"TOKEN": "sk-SECRET"})
    spec = convert_tool_definition(server, tool("create_issue"))
    blob = spec.model_dump_json()
    assert "sk-SECRET" not in blob
    assert "TOKEN" not in blob


def test_empty_description_is_synthesized_non_empty():
    spec = convert_tool_definition(cfg("github"), tool("ping", description=""))
    assert spec.description  # ToolSpec forbids empty descriptions


def test_list_servers_is_secret_free():
    mgr, _, _ = manager({"github": []})
    run(mgr.register_server(cfg("github", headers={"Authorization": "SECRET"})))
    assert "SECRET" not in str(mgr.list_servers())


# --------------------------------------------------------------------------- #
# Rejections + validation of untrusted metadata
# --------------------------------------------------------------------------- #

def test_duplicate_tool_in_single_discovery_rejected():
    mgr, _, _ = manager({"github": [tool("create_issue"), tool("create_issue")]})
    run(mgr.register_server(cfg("github")))
    with pytest.raises(MCPProtocolError):
        run(mgr.discover_server_tools("github"))


def test_malformed_schema_rejected():
    bad = MCPToolDefinition(name="x", input_schema={"cycle": None})
    bad_dict = bad.model_copy(update={"input_schema": {"bad": {1, 2}}})  # non-serializable
    with pytest.raises(MCPProtocolError):
        convert_tool_definition(cfg("github"), bad_dict)


def test_invalid_tool_name_rejected():
    with pytest.raises(MCPProtocolError):
        convert_tool_definition(cfg("github"), MCPToolDefinition(name="bad name!"))


def test_duplicate_server_registration_rejected():
    mgr, _, _ = manager({"github": []})
    run(mgr.register_server(cfg("github")))
    with pytest.raises(ValueError):
        run(mgr.register_server(cfg("github")))


def test_discover_unknown_server_raises():
    mgr, _, _ = manager({})
    with pytest.raises(MCPServerNotFoundError):
        run(mgr.discover_server_tools("nope"))


# --------------------------------------------------------------------------- #
# Refresh + isolation
# --------------------------------------------------------------------------- #

def test_refresh_replaces_stale_tools():
    client = FakeMCPClient(tools={"github": [tool("old_tool")]})
    reg = ToolRegistry()
    mgr = MCPRegistryManager(reg, client)
    run(mgr.register_server(cfg("github")))
    run(mgr.discover_server_tools("github"))
    assert reg.exists("mcp.github.old_tool")

    # server now advertises a different tool set
    client._tools["github"] = [tool("new_tool")]
    run(mgr.refresh_server_tools("github"))
    assert not reg.exists("mcp.github.old_tool")  # stale removed
    assert reg.exists("mcp.github.new_tool")       # fresh registered


def test_refresh_failure_does_not_corrupt_existing_tools():
    client = FakeMCPClient(tools={"github": [tool("good_tool")]},
                           fail_discovery=set())
    reg = ToolRegistry()
    mgr = MCPRegistryManager(reg, client)
    run(mgr.register_server(cfg("github")))
    run(mgr.discover_server_tools("github"))
    assert reg.exists("mcp.github.good_tool")

    # next discovery fails: previously registered tools must remain intact
    client._fail_discovery = {"github"}
    with pytest.raises(Exception):
        run(mgr.refresh_server_tools("github"))
    assert reg.exists("mcp.github.good_tool")


def test_one_server_cannot_overwrite_another_servers_tools():
    reg = ToolRegistry()
    client = FakeMCPClient(tools={
        "github": [tool("create_issue")],
        "gitlab": [tool("create_issue")],  # same tool name, different server
    })
    mgr = MCPRegistryManager(reg, client)
    run(mgr.register_server(cfg("github")))
    run(mgr.register_server(cfg("gitlab")))
    run(mgr.discover_server_tools("github"))
    run(mgr.discover_server_tools("gitlab"))
    # both coexist under isolated namespaces
    assert reg.exists("mcp.github.create_issue")
    assert reg.exists("mcp.gitlab.create_issue")


def test_mcp_cannot_overwrite_internal_capability():
    reg = get_default_tool_registry()  # holds internal "search_documents"
    # a malicious server advertises a tool whose id would collide with internal
    client = FakeMCPClient(tools={"x": [tool("create_issue")]})
    mgr = MCPRegistryManager(reg, client)
    run(mgr.register_server(cfg("x")))
    run(mgr.discover_server_tools("x"))
    # internal capability id is untouched and still INTERNAL
    assert reg.get("search_documents").kind == ToolKind.INTERNAL


def test_collision_with_existing_id_is_rejected_and_rolled_back():
    reg = ToolRegistry()
    # pre-seed the exact id a discovery would produce
    from app.agent.mcp.registry import convert_tool_definition as conv
    reg.register(conv(cfg("github"), tool("create_issue")))
    client = FakeMCPClient(tools={"github": [tool("list_repos"), tool("create_issue")]})
    mgr = MCPRegistryManager(reg, client)
    run(mgr.register_server(cfg("github")))
    with pytest.raises(MCPProtocolError):
        run(mgr.discover_server_tools("github"))
    # rollback: the partially-registered sibling from THIS batch is gone
    assert not reg.exists("mcp.github.list_repos")


# --------------------------------------------------------------------------- #
# Idempotent / concurrent discovery
# --------------------------------------------------------------------------- #

def test_repeat_discovery_is_idempotent():
    mgr, _, client = manager({"github": [tool("create_issue")]})
    run(mgr.register_server(cfg("github")))
    run(mgr.discover_server_tools("github"))
    run(mgr.discover_server_tools("github"))  # no re-list, no double register
    assert client.list_tools_calls.count("github") == 1
    assert mgr.list_discovered_tools() == ["mcp.github.create_issue"]


def test_concurrent_discovery_does_not_double_register():
    mgr, _, client = manager({"github": [tool("create_issue")]})
    run(mgr.register_server(cfg("github")))

    async def race():
        await asyncio.gather(
            mgr.discover_server_tools("github"),
            mgr.discover_server_tools("github"),
        )

    run(race())
    assert client.list_tools_calls.count("github") == 1
    assert mgr.list_discovered_tools() == ["mcp.github.create_issue"]


def test_unregister_removes_server_and_tools():
    mgr, reg, client = manager({"github": [tool("create_issue")]})
    run(mgr.register_server(cfg("github")))
    run(mgr.discover_server_tools("github"))
    run(mgr.unregister_server("github"))
    assert not reg.exists("mcp.github.create_issue")
    assert mgr.list_servers() == []
    assert "github" in client.closed
