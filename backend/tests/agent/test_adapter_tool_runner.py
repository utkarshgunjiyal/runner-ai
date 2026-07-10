"""Phase 9 tests — AdapterToolRunner dispatch."""

import pytest

from app.agent.execution.adapter_runner import (
    AdapterToolRunner,
    StepCapabilityMissingError,
)
from app.agent.models.plan import PlanStep, PlanStepType
from app.agent.models.tool_spec import (
    RiskLevel,
    SideEffectType,
    ToolKind,
    ToolSpec,
)
from app.agent.registry.registry import ToolNotFoundError, ToolRegistry
from app.agent.tools.adapter import ToolAdapter
from app.agent.tools.adapter_registry import AdapterNotFoundError, AdapterRegistry


# --------------------------------------------------------------------------- #
# Fake adapters (test-only)
# --------------------------------------------------------------------------- #

class RecordingAdapter(ToolAdapter):
    def __init__(self, label):
        self.label = label
        self.received = None

    def execute(self, tool: ToolSpec, args: dict) -> dict:
        self.received = (tool, args)
        return {"adapter": self.label, "tool_id": tool.id, "args": args}


def make_tool(tool_id, kind) -> ToolSpec:
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


def tool_step(step_id, capability_id):
    return PlanStep(
        id=step_id,
        step_type=PlanStepType.TOOL,
        capability_id=capability_id,
        description=f"do {step_id}",
    )


def final_step(step_id="final"):
    return PlanStep(id=step_id, step_type=PlanStepType.FINAL_RESPONSE, description="respond")


def build(kind):
    tool_reg = ToolRegistry()
    tool_reg.register(make_tool("cap", kind))
    adapter_reg = AdapterRegistry()
    adapters = {
        ToolKind.INTERNAL: RecordingAdapter("internal"),
        ToolKind.API: RecordingAdapter("api"),
        ToolKind.MCP: RecordingAdapter("mcp"),
    }
    for k, a in adapters.items():
        adapter_reg.register(k, a)
    return AdapterToolRunner(tool_reg, adapter_reg), adapters


# --------------------------------------------------------------------------- #
# Dispatch by kind
# --------------------------------------------------------------------------- #

def test_internal_dispatches_to_internal_adapter():
    runner, adapters = build(ToolKind.INTERNAL)
    out = runner.run(tool_step("s1", "cap"), {"q": 1})
    assert out["adapter"] == "internal"
    assert adapters[ToolKind.INTERNAL].received is not None
    assert adapters[ToolKind.API].received is None


def test_api_dispatches_to_api_adapter():
    runner, _ = build(ToolKind.API)
    assert runner.run(tool_step("s1", "cap"), {})["adapter"] == "api"


def test_mcp_dispatches_to_mcp_adapter():
    runner, _ = build(ToolKind.MCP)
    assert runner.run(tool_step("s1", "cap"), {})["adapter"] == "mcp"


def test_adapter_receives_correct_tool_and_args():
    runner, adapters = build(ToolKind.INTERNAL)
    runner.run(tool_step("s1", "cap"), {"query": "hello"})
    tool, args = adapters[ToolKind.INTERNAL].received
    assert tool.id == "cap"
    assert tool.kind == ToolKind.INTERNAL
    assert args == {"query": "hello"}


def test_adapter_output_returned():
    runner, _ = build(ToolKind.INTERNAL)
    out = runner.run(tool_step("s1", "cap"), {"a": 1})
    assert out == {"adapter": "internal", "tool_id": "cap", "args": {"a": 1}}


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #

def test_missing_capability_raises():
    runner, _ = build(ToolKind.INTERNAL)
    with pytest.raises(StepCapabilityMissingError):
        runner.run(final_step("final"), {})  # FINAL_RESPONSE has no capability_id


def test_missing_tool_propagates_tool_not_found():
    runner, _ = build(ToolKind.INTERNAL)
    with pytest.raises(ToolNotFoundError):
        runner.run(tool_step("s1", "ghost"), {})


def test_missing_adapter_propagates_adapter_not_found():
    tool_reg = ToolRegistry()
    tool_reg.register(make_tool("cap", ToolKind.MCP))
    adapter_reg = AdapterRegistry()
    adapter_reg.register(ToolKind.INTERNAL, RecordingAdapter("internal"))  # no MCP adapter
    runner = AdapterToolRunner(tool_reg, adapter_reg)
    with pytest.raises(AdapterNotFoundError):
        runner.run(tool_step("s1", "cap"), {})
