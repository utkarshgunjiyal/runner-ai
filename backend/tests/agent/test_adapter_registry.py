"""Phase 8 tests — tool adapter interface + adapter registry."""

import pytest

from app.agent.models.tool_spec import (
    RiskLevel,
    SideEffectType,
    ToolKind,
    ToolSpec,
)
from app.agent.tools.adapter import ToolAdapter
from app.agent.tools.adapter_registry import (
    AdapterNotFoundError,
    AdapterRegistry,
    DuplicateAdapterError,
)


# --------------------------------------------------------------------------- #
# Fake adapters (test-only)
# --------------------------------------------------------------------------- #

class FakeInternalAdapter(ToolAdapter):
    def execute(self, tool: ToolSpec, args: dict) -> dict:
        return {"adapter": "internal", "tool_id": tool.id, "args": args}


class FakeApiAdapter(ToolAdapter):
    def execute(self, tool: ToolSpec, args: dict) -> dict:
        return {"adapter": "api", "tool_id": tool.id, "args": args}


class FakeMcpAdapter(ToolAdapter):
    def execute(self, tool: ToolSpec, args: dict) -> dict:
        return {"adapter": "mcp", "tool_id": tool.id, "args": args}


def make_tool(tool_id="t", kind=ToolKind.INTERNAL) -> ToolSpec:
    return ToolSpec(
        id=tool_id,
        name=tool_id,
        kind=kind,
        description=f"{tool_id} tool",
        input_schema={},
        output_schema={},
        risk_level=RiskLevel.LOW,
        side_effects=SideEffectType.READ,
        requires_approval=False,
    )


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

def test_register_and_get_by_kind():
    reg = AdapterRegistry()
    adapter = FakeInternalAdapter()
    reg.register(ToolKind.INTERNAL, adapter)
    assert reg.get(ToolKind.INTERNAL) is adapter


def test_exists():
    reg = AdapterRegistry()
    reg.register(ToolKind.INTERNAL, FakeInternalAdapter())
    assert reg.exists(ToolKind.INTERNAL) is True
    assert reg.exists(ToolKind.API) is False


def test_duplicate_registration_raises():
    reg = AdapterRegistry()
    reg.register(ToolKind.INTERNAL, FakeInternalAdapter())
    with pytest.raises(DuplicateAdapterError):
        reg.register(ToolKind.INTERNAL, FakeInternalAdapter())


def test_unknown_lookup_raises():
    reg = AdapterRegistry()
    with pytest.raises(AdapterNotFoundError):
        reg.get(ToolKind.MCP)


def test_list_kinds_deterministic():
    reg = AdapterRegistry()
    # register in scrambled order
    reg.register(ToolKind.MCP, FakeMcpAdapter())
    reg.register(ToolKind.INTERNAL, FakeInternalAdapter())
    reg.register(ToolKind.API, FakeApiAdapter())
    assert [k.value for k in reg.list_kinds()] == ["api", "internal", "mcp"]
    # stable across calls
    assert reg.list_kinds() == reg.list_kinds()


def test_adapter_execute_receives_tool_and_args():
    adapter = FakeInternalAdapter()
    tool = make_tool("search_documents")
    result = adapter.execute(tool, {"query": "hello"})
    assert result == {
        "adapter": "internal",
        "tool_id": "search_documents",
        "args": {"query": "hello"},
    }


def test_registering_all_kinds_works():
    reg = AdapterRegistry()
    reg.register(ToolKind.INTERNAL, FakeInternalAdapter())
    reg.register(ToolKind.API, FakeApiAdapter())
    reg.register(ToolKind.MCP, FakeMcpAdapter())
    assert set(reg.list_kinds()) == {ToolKind.INTERNAL, ToolKind.API, ToolKind.MCP}
    # each dispatches to the right adapter
    tool = make_tool("t")
    assert reg.get(ToolKind.API).execute(tool, {})["adapter"] == "api"
    assert reg.get(ToolKind.MCP).execute(tool, {})["adapter"] == "mcp"
