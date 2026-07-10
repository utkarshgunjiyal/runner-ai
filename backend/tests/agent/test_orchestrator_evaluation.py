"""Phase 22 tests — Evaluation + Repair integration in AgentOrchestrator.

Config-free: a fake ContextEngine + fake retrieval/execution drive the real
BehaviorGate/DirectRuntime/FinalContextBuilder; a counting provider and a
scripted evaluator make the evaluate→repair→regenerate loop deterministic. No
Mongo/Qdrant/Redis, no application settings, no real LLM.
"""

import ast
import asyncio
import inspect

from app.agent.capabilities.models import CapabilityMatch, CapabilityRetrievalResponse
from app.agent.context.final_builder import FinalContextBuilder
from app.agent.evaluation.models import EvaluationReport, RepairAction, RepairDecision
from app.agent.gate.behavior_gate import BehaviorGate
from app.agent.llm.final_provider import FinalAnswer
from app.agent.models.tool_spec import RiskLevel, SideEffectType, ToolKind, ToolSpec
from app.agent.runtime import orchestrator as orchestrator_module
from app.agent.runtime.context import EvidenceItem, RunContext, WorkingContextItem
from app.agent.runtime.direct_runtime import DirectRuntime
from app.agent.runtime.orchestrator import AgentOrchestrator
from app.agent.runtime.planner_runtime import PlannerRuntime
from app.agent.tools.result import AdapterResult


def run(coro):
    return asyncio.run(coro)


DIRECT_REQUEST = "What does the document say about pricing?"


def make_tool(tool_id):
    return ToolSpec(
        id=tool_id, name=tool_id, kind=ToolKind.INTERNAL, description=f"{tool_id} tool",
        input_schema={}, output_schema={}, risk_level=RiskLevel.LOW,
        side_effects=SideEffectType.READ, requires_approval=False,
    )


class FakeContextEngine:
    def __init__(self, working_context=None):
        self._wc = working_context or []

    async def build(self, user_request, user_id, thread_id=None, metadata=None):
        return RunContext.create(
            user_request=user_request, user_id=user_id, thread_id=thread_id,
            working_context=list(self._wc), metadata=dict(metadata or {}),
        )


class FakeRetriever:
    def __init__(self, tools):
        self._tools = tools

    def _resp(self, query):
        return CapabilityRetrievalResponse(
            query=query,
            matches=[CapabilityMatch(tool=t, score=1.0) for t in self._tools],
        )

    def retrieve(self, request):
        return self._resp(request.query)

    def retrieve_for_run_context(self, run_context, *, top_k=5, **kw):
        return self._resp(run_context.user_request)


class FakeExecutor:
    async def execute(self, tool, args):
        return AdapterResult.ok(
            output={"answer": "x"}, evidence=[EvidenceItem(source="document", content="g", score=0.9)]
        )


class CountingProvider:
    provider = "deterministic"
    model = "fake"

    def __init__(self):
        self.calls = 0

    async def generate(self, final_prompt):
        self.calls += 1
        return FinalAnswer(text=f"draft {self.calls}", used_citations=[], provider=self.provider, model=self.model)


class ScriptedEvaluator:
    """Returns a fixed sequence of EvaluationReports (repeats the last)."""

    def __init__(self, reports):
        self._reports = list(reports)
        self.calls = 0

    def evaluate(self, final_prompt, final_answer, run_context=None):
        report = self._reports[min(self.calls, len(self._reports) - 1)]
        self.calls += 1
        return report


def passing():
    return EvaluationReport(passed=True, overall_score=0.9,
                            repair_decision=RepairDecision(action=RepairAction.NONE))


def failing(action, *, max_attempts=5):
    return EvaluationReport(
        passed=False, overall_score=0.3,
        repair_decision=RepairDecision(action=action, reason="bad", max_attempts=max_attempts),
    )


def build(*, evaluator=None, provider=None, working_context=None, max_repair_rounds=1):
    retriever = FakeRetriever([make_tool("cap_a")])
    direct = DirectRuntime(retriever, FakeExecutor())
    provider = provider or CountingProvider()
    orch = AgentOrchestrator(
        context_engine=FakeContextEngine(working_context),
        behavior_gate=BehaviorGate(),
        direct_runtime=direct,
        planner_runtime=PlannerRuntime(direct, retriever),
        final_context_builder=FinalContextBuilder(),
        final_provider=provider,
        answer_evaluator=evaluator,
        max_repair_rounds=max_repair_rounds,
    )
    return orch, provider


# --------------------------------------------------------------------------- #
# Default (no evaluator) unchanged
# --------------------------------------------------------------------------- #

def test_without_evaluator_behavior_unchanged():
    orch, provider = build(evaluator=None)
    result = run(orch.run(DIRECT_REQUEST, user_id="u"))
    assert provider.calls == 1
    assert result.answer.text == "draft 1"
    assert "evaluation_passed" not in result.metadata
    assert "answer_evaluation" not in result.run_context.metadata


# --------------------------------------------------------------------------- #
# Passing evaluation
# --------------------------------------------------------------------------- #

def test_passing_evaluation_returns_first_answer():
    orch, provider = build(evaluator=ScriptedEvaluator([passing()]))
    result = run(orch.run(DIRECT_REQUEST, user_id="u"))
    assert provider.calls == 1
    assert result.answer.text == "draft 1"
    assert result.metadata["evaluation_passed"] is True
    assert result.metadata["repair_rounds"] == 0


# --------------------------------------------------------------------------- #
# Local regeneration repairs
# --------------------------------------------------------------------------- #

def test_stronger_instructions_regenerates_once():
    evaluator = ScriptedEvaluator([failing(RepairAction.REGENERATE_WITH_STRONGER_INSTRUCTIONS), passing()])
    orch, provider = build(evaluator=evaluator)
    result = run(orch.run(DIRECT_REQUEST, user_id="u"))
    assert provider.calls == 2
    assert result.answer.text == "draft 2"
    assert "regenerate_with_stronger_instructions" in result.metadata["repair_actions"]
    assert result.metadata["repair_rounds"] == 1


def test_same_context_regenerates_once():
    evaluator = ScriptedEvaluator([failing(RepairAction.REGENERATE_WITH_SAME_CONTEXT), passing()])
    orch, provider = build(evaluator=evaluator)
    result = run(orch.run(DIRECT_REQUEST, user_id="u"))
    assert provider.calls == 2
    assert result.answer.text == "draft 2"


# --------------------------------------------------------------------------- #
# Deferred + terminal repairs (no regeneration)
# --------------------------------------------------------------------------- #

def test_deferred_retrieve_more_context_recorded_not_executed():
    evaluator = ScriptedEvaluator([failing(RepairAction.RETRIEVE_MORE_CONTEXT)])
    orch, provider = build(evaluator=evaluator)
    result = run(orch.run(DIRECT_REQUEST, user_id="u"))
    assert provider.calls == 1  # not regenerated
    assert "retrieve_more_context" in result.metadata["repair_actions"]
    round_records = result.run_context.metadata["repair_rounds"]
    assert round_records[0]["target_stage"] == "context_engine"
    assert round_records[0]["applied"] is False


def test_fail_gracefully_records_metadata():
    evaluator = ScriptedEvaluator([failing(RepairAction.FAIL_GRACEFULLY)])
    orch, provider = build(evaluator=evaluator)
    result = run(orch.run(DIRECT_REQUEST, user_id="u"))
    assert provider.calls == 1
    assert result.run_context.metadata["repair_failure"]
    assert "fail_gracefully" in result.metadata["repair_actions"]


# --------------------------------------------------------------------------- #
# Bounds + metadata
# --------------------------------------------------------------------------- #

def test_max_repair_rounds_enforced():
    # Always fails with a regenerate action; orchestrator cap = 1 regeneration.
    evaluator = ScriptedEvaluator([failing(RepairAction.REGENERATE_WITH_STRONGER_INSTRUCTIONS)])
    orch, provider = build(evaluator=evaluator, max_repair_rounds=1)
    result = run(orch.run(DIRECT_REQUEST, user_id="u"))
    assert provider.calls == 2  # 1 initial + 1 regeneration, then stop
    assert result.metadata["repair_rounds"] == 1
    assert result.metadata["evaluation_passed"] is False


def test_final_evaluation_and_repair_metadata_exist():
    evaluator = ScriptedEvaluator([failing(RepairAction.REGENERATE_WITH_STRONGER_INSTRUCTIONS), passing()])
    orch, _ = build(evaluator=evaluator)
    result = run(orch.run(DIRECT_REQUEST, user_id="u"))
    rc = result.run_context
    assert rc.metadata["final_answer"]["text"] == result.answer.text  # final answer metadata
    assert "answer_evaluation" in rc.metadata                         # evaluation metadata
    assert "repair_rounds" in rc.metadata                             # repair metadata


def test_working_context_remains_immutable():
    wc = [WorkingContextItem(source="thread_summary", content="prior")]
    evaluator = ScriptedEvaluator([failing(RepairAction.REGENERATE_WITH_STRONGER_INSTRUCTIONS), passing()])
    orch, _ = build(evaluator=evaluator, working_context=wc)
    result = run(orch.run(DIRECT_REQUEST, user_id="u"))
    assert [w.content for w in result.run_context.working_context] == ["prior"]
    assert len(result.run_context.working_context) == 1


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
    targets = _module_level_import_targets(orchestrator_module)
    banned = (
        "app.config", "app.services", "app.db", "motor", "redis", "qdrant",
        "openai", "anthropic", "google.generativeai", "genai", "llm_provider",
    )
    for name in banned:
        assert not any(name in t for t in targets), (name, targets)
