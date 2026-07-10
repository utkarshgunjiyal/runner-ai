"""Phase 18 tests — Runtime Orchestrator end-to-end.

Config-free: a fake ContextEngine seeds working context; the real BehaviorGate,
DirectRuntime, PlannerRuntime, FinalContextBuilder, and DeterministicFinalProvider
are wired with fake retrieval/execution. No Mongo/Qdrant/Redis, no application
settings, no real LLM. Async ``run`` is driven via ``asyncio.run``.
"""

import ast
import asyncio
import inspect

import pytest

from app.agent.capabilities.models import CapabilityMatch, CapabilityRetrievalResponse
from app.agent.context.final_builder import FinalContextBuilder
from app.agent.gate.behavior_gate import BehaviorGate
from app.agent.llm.final_provider import DeterministicFinalProvider, FinalAnswer
from app.agent.models.final_prompt import FinalPrompt
from app.agent.models.tool_spec import RiskLevel, SideEffectType, ToolKind, ToolSpec
from app.agent.runtime import orchestrator as orchestrator_module
from app.agent.runtime.context import EvidenceItem, RunContext, WorkingContextItem
from app.agent.runtime.direct_runtime import DirectRuntime
from app.agent.runtime.orchestrator import (
    AgentOrchestrator,
    AgentRunResult,
    MissingPlanSourceError,
)
from app.agent.runtime.planner_runtime import ExecutionPlan, PlannerRuntime, PlannerTask
from app.agent.tools.result import AdapterResult


def run(coro):
    return asyncio.run(coro)


DIRECT_REQUEST = "What does the document say about pricing?"
PLANNER_REQUEST = "Summarize the report and then email it to the team"


def make_tool(tool_id: str) -> ToolSpec:
    return ToolSpec(
        id=tool_id, name=tool_id, kind=ToolKind.INTERNAL, description=f"{tool_id} tool",
        input_schema={}, output_schema={}, risk_level=RiskLevel.LOW,
        side_effects=SideEffectType.READ, requires_approval=False,
    )


class FakeContextEngine:
    """Async build() that seeds a RunContext with fixed working context."""

    def __init__(self, working_context=None):
        self._wc = working_context or []
        self.calls = []

    async def build(self, user_request, user_id, thread_id=None, metadata=None):
        self.calls.append((user_request, user_id, thread_id))
        return RunContext.create(
            user_request=user_request, user_id=user_id, thread_id=thread_id,
            working_context=list(self._wc), metadata=dict(metadata or {}),
        )


class FakeRetriever:
    def __init__(self, tools):
        self._tools = tools

    def _resp(self, query):
        matches = [
            CapabilityMatch(tool=t, score=float(len(self._tools) - i))
            for i, t in enumerate(self._tools)
        ]
        return CapabilityRetrievalResponse(query=query, matches=matches)

    def retrieve(self, request):
        return self._resp(request.query)

    def retrieve_for_run_context(self, run_context, *, top_k=5, **kw):
        return self._resp(run_context.user_request)


class FakeExecutor:
    """Every capability succeeds with one evidence item + an output."""

    def __init__(self):
        self.calls = []

    async def execute(self, tool, args):
        self.calls.append(tool.id)
        return AdapterResult.ok(
            output={"answer": f"result of {tool.id}"},
            evidence=[EvidenceItem(source="document", content=f"grounding for {tool.id}", score=0.9)],
        )


def build_orchestrator(*, working_context=None, provider=None, plan_source=None):
    retriever = FakeRetriever([make_tool("cap_a"), make_tool("cap_b")])
    executor = FakeExecutor()
    direct = DirectRuntime(retriever, executor)
    planner = PlannerRuntime(direct, retriever)
    return AgentOrchestrator(
        context_engine=FakeContextEngine(working_context),
        behavior_gate=BehaviorGate(),
        direct_runtime=direct,
        planner_runtime=planner,
        final_context_builder=FinalContextBuilder(),
        final_provider=provider or DeterministicFinalProvider(),
        plan_source=plan_source,
    ), executor


def static_plan(run_context):
    return ExecutionPlan(
        id="plan-1", goal=run_context.user_request,
        tasks=[
            PlannerTask(id="t1", request="summarize the report"),
            PlannerTask(id="t2", request="email the team", optional=True),
        ],
    )


# --------------------------------------------------------------------------- #
# End-to-end
# --------------------------------------------------------------------------- #

def test_direct_path_end_to_end():
    orch, executor = build_orchestrator()
    result = run(orch.run(DIRECT_REQUEST, user_id="u", thread_id="t1"))

    assert isinstance(result, AgentRunResult)
    assert result.behavior_path == "direct"
    assert result.user_id == "u"
    assert result.thread_id == "t1"
    assert isinstance(result.answer, FinalAnswer)
    assert result.answer.text
    assert isinstance(result.final_prompt, FinalPrompt)
    assert len(executor.calls) == 1  # one capability on the direct path


def test_planner_path_end_to_end_with_static_plan():
    orch, executor = build_orchestrator(plan_source=static_plan)
    result = run(orch.run(PLANNER_REQUEST, user_id="u"))

    assert result.behavior_path == "planner"
    assert result.metadata["runtime_status"] == "completed"
    # Two tasks → DirectRuntime invoked twice → two tool outputs on the context.
    assert len(executor.calls) == 2
    assert len(result.run_context.tool_outputs) == 2


def test_planner_path_without_plan_source_raises():
    orch, _ = build_orchestrator(plan_source=None)
    with pytest.raises(MissingPlanSourceError):
        run(orch.run(PLANNER_REQUEST, user_id="u"))


# --------------------------------------------------------------------------- #
# Wiring guarantees
# --------------------------------------------------------------------------- #

def test_final_answer_attached_to_run_context():
    orch, _ = build_orchestrator()
    result = run(orch.run(DIRECT_REQUEST, user_id="u"))
    stored = result.run_context.metadata["final_answer"]
    assert stored["text"] == result.answer.text
    assert stored["provider"] == result.answer.provider


def test_final_prompt_produced_with_user_request():
    orch, _ = build_orchestrator()
    result = run(orch.run(DIRECT_REQUEST, user_id="u"))
    assert result.final_prompt.user_request == DIRECT_REQUEST


def test_behavior_decision_recorded():
    orch, _ = build_orchestrator()
    result = run(orch.run(DIRECT_REQUEST, user_id="u"))
    rc = result.run_context
    assert rc.behavior_profile is not None
    assert rc.behavior_profile.path.value == "direct"
    assert rc.metadata["behavior_decision"]["path"] == "direct"
    assert result.metadata["behavior_decision"]["path"] == "direct"


def test_tool_outputs_and_evidence_survive_into_final_prompt():
    orch, _ = build_orchestrator()
    result = run(orch.run(DIRECT_REQUEST, user_id="u"))
    assert result.run_context.tool_outputs  # execution produced output
    assert result.run_context.evidence      # and evidence
    assert result.final_prompt.tool_output_sections
    assert result.final_prompt.evidence_sections
    assert result.final_prompt.evidence_sections[0].content.startswith("grounding for")


def test_working_context_remains_immutable():
    wc = [WorkingContextItem(source="thread_summary", content="prior turn")]
    orch, _ = build_orchestrator(working_context=wc)
    result = run(orch.run(DIRECT_REQUEST, user_id="u"))
    assert [w.content for w in result.run_context.working_context] == ["prior turn"]
    assert len(result.run_context.working_context) == 1


def test_dependencies_are_injected():
    # A custom provider flows through untouched, proving injection.
    orch, _ = build_orchestrator(provider=DeterministicFinalProvider(provider="custom-x", model="m9"))
    result = run(orch.run(DIRECT_REQUEST, user_id="u"))
    assert result.answer.provider == "custom-x"
    assert result.answer.model == "m9"
    assert result.metadata["provider"] == "custom-x"


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


def test_no_config_db_or_llm_imports():
    targets = _module_level_import_targets(orchestrator_module)
    banned = (
        "app.config", "app.services", "app.db", "motor", "redis", "qdrant",
        "openai", "anthropic", "google.generativeai", "genai", "llm_provider",
    )
    for name in banned:
        assert not any(name in t for t in targets), (name, targets)
