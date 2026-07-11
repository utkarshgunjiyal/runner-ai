"""Phase 19 tests — Runtime Factory (composition root).

Config-free: the factory constructs the real default stack without touching V1.5
(adapters/providers lazy-import services only when executed). The execution test
injects a fake context engine + fake executor so nothing hits Mongo/Qdrant/Redis
and no real LLM is called.
"""

import ast
import asyncio
import inspect

from app.agent.context.final_builder import FinalContextBuilder
from app.agent.gate.behavior_gate import BehaviorGate
from app.agent.capabilities.keyword_retriever import KeywordCapabilityRetriever
from app.agent.llm.final_provider import DeterministicFinalProvider
from app.agent.models.tool_spec import RiskLevel, SideEffectType, ToolKind, ToolSpec
from app.agent.runtime import factory as factory_module
from app.agent.runtime.context import RunContext, WorkingContextItem
from app.agent.runtime.direct_runtime import DirectRuntime
from app.agent.runtime.factory import (
    InternalCapabilityExecutor,
    build_default_orchestrator,
    build_default_runtime,
)
from app.agent.runtime.orchestrator import AgentOrchestrator, AgentRunResult
from app.agent.runtime.planner_runtime import PlannerRuntime
from app.agent.tools.internal.job_adapter import JobAdapter
from app.agent.tools.result import AdapterResult, ErrorCode


def run(coro):
    return asyncio.run(coro)


def make_tool(tool_id: str) -> ToolSpec:
    return ToolSpec(
        id=tool_id, name=tool_id, kind=ToolKind.INTERNAL, description=f"{tool_id} tool",
        input_schema={}, output_schema={}, risk_level=RiskLevel.LOW,
        side_effects=SideEffectType.READ, requires_approval=False,
    )


class FakeContextEngine:
    async def build(self, user_request, user_id, thread_id=None, metadata=None):
        return RunContext.create(
            user_request=user_request, user_id=user_id, thread_id=thread_id,
            working_context=[WorkingContextItem(source="thread_summary", content="prior")],
            metadata=dict(metadata or {}),
        )


class FakeExecutor:
    async def execute(self, tool, args):
        return AdapterResult.ok(output={"answer": f"ran {tool.id}"})


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #

def test_factory_builds_orchestrator():
    orch = build_default_runtime()
    assert isinstance(orch, AgentOrchestrator)


def test_alias_points_to_same_factory():
    assert build_default_orchestrator is build_default_runtime


def test_every_dependency_wired():
    orch = build_default_runtime()
    assert hasattr(orch._context_engine, "build")
    assert isinstance(orch._behavior_gate, BehaviorGate)
    assert isinstance(orch._direct_runtime, DirectRuntime)
    assert isinstance(orch._planner_runtime, PlannerRuntime)
    assert isinstance(orch._final_context_builder, FinalContextBuilder)
    assert isinstance(orch._final_provider, DeterministicFinalProvider)
    # Retriever is the hybrid retriever whose Stage-1 base is the keyword
    # retriever over the default registry (Phase 29 wiring).
    from app.agent.retriever.capability_retriever import HybridCapabilityRetriever
    assert isinstance(orch._direct_runtime._retriever, HybridCapabilityRetriever)
    assert isinstance(orch._direct_runtime._retriever.base, KeywordCapabilityRetriever)
    # Planner reuses the same DirectRuntime instance (no duplicate engine).
    assert orch._planner_runtime._direct is orch._direct_runtime


def test_default_provider_is_deterministic():
    orch = build_default_runtime()
    assert isinstance(orch._final_provider, DeterministicFinalProvider)
    assert orch._final_provider.provider == "deterministic"


def test_injected_provider_override():
    custom = DeterministicFinalProvider(provider="custom-x", model="m9")
    orch = build_default_runtime(final_provider=custom)
    assert orch._final_provider is custom


# --------------------------------------------------------------------------- #
# Executes with fake dependencies (no V1.5, no LLM)
# --------------------------------------------------------------------------- #

def test_runtime_executes_with_fake_dependencies():
    orch = build_default_runtime(
        context_engine=FakeContextEngine(),
        capability_executor=FakeExecutor(),
    )
    result = run(orch.run("What does the document say about pricing?", user_id="u"))
    assert isinstance(result, AgentRunResult)
    assert result.behavior_path == "direct"
    assert result.answer.text
    assert result.run_context.metadata["final_answer"]["text"] == result.answer.text


# --------------------------------------------------------------------------- #
# Default executor binding (Execution Bridge for internal tools)
# --------------------------------------------------------------------------- #

def test_internal_executor_binds_known_tool():
    async def fake_get_job(job_id, user_id=None):
        return {"job_id": job_id, "status": "completed"}

    executor = InternalCapabilityExecutor(job_adapter=JobAdapter(get_job_fn=fake_get_job))
    result = run(executor.execute(make_tool("get_job_status"), {"job_id": "j1"}))
    assert result.success is True
    assert result.output["status"] == "completed"


def test_internal_executor_unknown_tool_is_failure():
    executor = InternalCapabilityExecutor()
    result = run(executor.execute(make_tool("not_a_real_tool"), {}))
    assert result.success is False
    assert result.error_code == ErrorCode.UNKNOWN_CAPABILITY
    assert result.retryable is False


def test_internal_executor_bound_ids_cover_expected_capabilities():
    ids = InternalCapabilityExecutor().bound_tool_ids()
    assert set(ids) == {
        "search_documents",
        "get_document_summary",
        "get_job_status",
        "get_thread_summary",
        "get_user_preferences",
    }


# --------------------------------------------------------------------------- #
# Executor override composition (regression: MCP routing must survive an
# internal-execution override — see the `unknown_capability` investigation).
# --------------------------------------------------------------------------- #

def _discovered_mcp_manager():
    """A registry manager with one discovered GitHub MCP tool (fake client)."""
    from app.agent.mcp.client import FakeMCPClient
    from app.agent.mcp.models import (
        MCPServerConfig,
        MCPToolCallResult,
        MCPToolDefinition,
        MCPTransport,
    )
    from app.agent.mcp.registry import MCPRegistryManager
    from app.agent.registry.registry import ToolRegistry

    tooldef = MCPToolDefinition(
        name="search_repositories", description="list repos",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
    )
    results = {("github", "search_repositories"): MCPToolCallResult(
        success=True, content=[{"type": "text", "text": "repo list"}],
        structured_content={"repos": ["a", "b"]})}
    client = FakeMCPClient(tools={"github": [tooldef]}, results=results)
    mgr = MCPRegistryManager(ToolRegistry(), client)
    cfg = MCPServerConfig(server_id="github", name="github",
                          transport=MCPTransport.STDIO, command=["srv"], timeout_seconds=5.0)
    run(mgr.register_server(cfg))
    run(mgr.discover_server_tools("github"))
    return mgr


def test_executor_override_alone_is_returned_verbatim():
    # Internal-only runtime with an override stays byte-identical: the override IS
    # the whole execution bridge (no composition wrapper).
    override = InternalCapabilityExecutor()
    executor = factory_module._executor_for({ToolKind.INTERNAL: override}, override)
    assert executor is override


def test_executor_override_composes_with_mcp_routing():
    # THE regression: an internal-execution override must NOT discard the MCP
    # route. With internal + MCP mounted, a selected MCP capability has to reach
    # the MCPAdapter instead of the internal-only executor (which would fail
    # unknown_capability before any transport call).
    from app.agent.execution.capability_executor import CompositeCapabilityExecutor
    from app.agent.runtime.factory import build_capability_platform

    mgr = _discovered_mcp_manager()
    platform = build_capability_platform(mcp_registry_manager=mgr)
    override = InternalCapabilityExecutor()

    executor = factory_module._executor_for(platform.executors_by_kind(), override)
    assert isinstance(executor, CompositeCapabilityExecutor)

    # MCP capability → routed to the MCPAdapter, not unknown_capability.
    mcp_spec = platform.tool_registry.get("mcp.github.search_repositories")
    mcp_result = run(executor.execute(mcp_spec, {"query": "my repos"}))
    assert mcp_result.success is True
    assert mcp_result.error_code is None
    assert mcp_result.metadata["adapter_type"] == "mcp"
    assert mcp_result.metadata["tool_name"] == "search_repositories"


def test_override_still_governs_internal_execution_when_composed():
    # The override must remain the INTERNAL executor after composition (the
    # composition root wires it for real document retrieval — that intent stands).
    from app.agent.runtime.factory import build_capability_platform

    mgr = _discovered_mcp_manager()
    platform = build_capability_platform(mcp_registry_manager=mgr)

    async def fake_get_job(job_id, user_id=None):
        return {"job_id": job_id, "status": "completed"}

    override = InternalCapabilityExecutor(job_adapter=JobAdapter(get_job_fn=fake_get_job))
    executor = factory_module._executor_for(platform.executors_by_kind(), override)

    internal_result = run(executor.execute(make_tool("get_job_status"), {"job_id": "j1"}))
    assert internal_result.success is True
    assert internal_result.output["status"] == "completed"


def test_build_default_runtime_override_plus_mcp_routes_mcp_tool():
    # End-to-end through the public factory entry point: override + MCP manager
    # yields a runtime whose execution bridge can run an MCP capability.
    from app.agent.execution.capability_executor import CompositeCapabilityExecutor

    mgr = _discovered_mcp_manager()
    orch = build_default_runtime(
        mcp_registry_manager=mgr,
        capability_executor=InternalCapabilityExecutor(),
    )
    bridge = orch._direct_runtime._executor
    assert isinstance(bridge, CompositeCapabilityExecutor)
    mcp_spec = mgr.tool_registry.get("mcp.github.search_repositories")
    result = run(bridge.execute(mcp_spec, {"query": "my repos"}))
    assert result.success is True
    assert result.metadata["adapter_type"] == "mcp"


# --------------------------------------------------------------------------- #
# Hygiene
# --------------------------------------------------------------------------- #

def _module_level_import_targets(module):
    tree = ast.parse(inspect.getsource(module))
    targets = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            targets += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            targets.append(node.module or "")
    return targets


def test_no_config_db_or_vendor_imports():
    targets = _module_level_import_targets(factory_module)
    banned = (
        "app.config", "app.services", "app.db", "motor", "redis", "qdrant",
        "openai", "anthropic", "google.generativeai", "genai",
    )
    for name in banned:
        assert not any(name in t for t in targets), (name, targets)
