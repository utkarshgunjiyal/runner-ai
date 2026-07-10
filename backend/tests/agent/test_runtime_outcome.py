"""Phase 23 tests — RuntimeOutcome terminal state on AgentRunResult.

Config-free: pure-function derivation tests plus orchestrator integration driven
by a scripted evaluator + counting provider (deferred repairs are exposed, never
executed). No Mongo/Qdrant/Redis, no application settings, no real LLM.
"""

import asyncio
from types import SimpleNamespace

from app.agent.capabilities.models import CapabilityMatch, CapabilityRetrievalResponse
from app.agent.context.final_builder import FinalContextBuilder
from app.agent.evaluation.models import EvaluationReport, RepairAction, RepairDecision
from app.agent.gate.behavior_gate import BehaviorGate
from app.agent.llm.final_provider import FinalAnswer
from app.agent.models.tool_spec import RiskLevel, SideEffectType, ToolKind, ToolSpec
from app.agent.runtime.context import EvidenceItem, RunContext
from app.agent.runtime.direct_runtime import DirectRuntime
from app.agent.runtime.orchestrator import AgentOrchestrator
from app.agent.runtime.outcome import RuntimeOutcome, derive_runtime_outcome
from app.agent.runtime.planner_runtime import PlannerRuntime
from app.agent.tools.result import AdapterResult


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Pure derivation
# --------------------------------------------------------------------------- #

def _report(passed, reason="bad"):
    return EvaluationReport(passed=passed, overall_score=0.9 if passed else 0.2, reason=reason)


def _repair(action, reason="r"):
    return SimpleNamespace(action=action, reason=reason)


def test_runtime_outcome_is_independent_enum():
    # RuntimeOutcome is its own vocabulary, not RepairAction.
    assert RuntimeOutcome is not RepairAction
    assert {o.value for o in RuntimeOutcome}.isdisjoint({a.value for a in RepairAction})


def test_no_evaluation_is_completed():
    assert derive_runtime_outcome(False, None, None) == (RuntimeOutcome.COMPLETED, None, None)


def test_passing_is_completed():
    assert derive_runtime_outcome(True, _report(True), None)[0] == RuntimeOutcome.COMPLETED


def test_mapping_table():
    cases = {
        RepairAction.FAIL_GRACEFULLY: RuntimeOutcome.FAILED,
        RepairAction.RETURN_PARTIAL_WITH_WARNING: RuntimeOutcome.COMPLETED_WITH_WARNING,
        RepairAction.RETRIEVE_MORE_CONTEXT: RuntimeOutcome.WAITING_FOR_CONTEXT,
        RepairAction.RERUN_CAPABILITY: RuntimeOutcome.WAITING_FOR_CONTEXT,
        RepairAction.REPLAN: RuntimeOutcome.WAITING_FOR_REPLAN,
        RepairAction.ASK_USER_FOR_CLARIFICATION: RuntimeOutcome.WAITING_FOR_USER,
        RepairAction.HUMAN_REVIEW: RuntimeOutcome.WAITING_FOR_APPROVAL,
    }
    for action, expected in cases.items():
        outcome, pending, reason = derive_runtime_outcome(True, _report(False), _repair(action))
        assert outcome == expected


def test_waiting_outcomes_carry_pending_action():
    outcome, pending, reason = derive_runtime_outcome(
        True, _report(False), _repair(RepairAction.ASK_USER_FOR_CLARIFICATION, "need info"))
    assert outcome == RuntimeOutcome.WAITING_FOR_USER
    assert pending == "ask_user_for_clarification"
    assert reason == "need info"


def test_failed_has_no_pending_action():
    outcome, pending, reason = derive_runtime_outcome(
        True, _report(False), _repair(RepairAction.FAIL_GRACEFULLY))
    assert outcome == RuntimeOutcome.FAILED
    assert pending is None


def test_exhausted_regenerate_is_completed_with_warning():
    outcome, pending, _ = derive_runtime_outcome(
        True, _report(False), _repair(RepairAction.REGENERATE_WITH_STRONGER_INSTRUCTIONS))
    assert outcome == RuntimeOutcome.COMPLETED_WITH_WARNING
    assert pending is None


# --------------------------------------------------------------------------- #
# Orchestrator integration
# --------------------------------------------------------------------------- #

def make_tool(tool_id):
    return ToolSpec(
        id=tool_id, name=tool_id, kind=ToolKind.INTERNAL, description=f"{tool_id} tool",
        input_schema={}, output_schema={}, risk_level=RiskLevel.LOW,
        side_effects=SideEffectType.READ, requires_approval=False,
    )


class FakeContextEngine:
    async def build(self, user_request, user_id, thread_id=None, metadata=None):
        return RunContext.create(user_request=user_request, user_id=user_id,
                                 thread_id=thread_id, metadata=dict(metadata or {}))


class FakeRetriever:
    def __init__(self, tools):
        self._tools = tools

    def _resp(self, query):
        return CapabilityRetrievalResponse(
            query=query, matches=[CapabilityMatch(tool=t, score=1.0) for t in self._tools])

    def retrieve(self, request):
        return self._resp(request.query)

    def retrieve_for_run_context(self, run_context, *, top_k=5, **kw):
        return self._resp(run_context.user_request)


class FakeExecutor:
    async def execute(self, tool, args):
        return AdapterResult.ok(output={"a": 1}, evidence=[EvidenceItem(source="document", content="g")])


class CountingProvider:
    provider = "deterministic"
    model = "fake"

    def __init__(self):
        self.calls = 0

    async def generate(self, final_prompt):
        self.calls += 1
        return FinalAnswer(text=f"draft {self.calls}", used_citations=[], provider=self.provider, model=self.model)


class ScriptedEvaluator:
    def __init__(self, reports):
        self._reports = list(reports)
        self.calls = 0

    def evaluate(self, final_prompt, final_answer, run_context=None):
        report = self._reports[min(self.calls, len(self._reports) - 1)]
        self.calls += 1
        return report


def failing(action, max_attempts=5):
    return EvaluationReport(passed=False, overall_score=0.2,
                            repair_decision=RepairDecision(action=action, reason="bad", max_attempts=max_attempts))


def passing():
    return EvaluationReport(passed=True, overall_score=0.9,
                            repair_decision=RepairDecision(action=RepairAction.NONE))


def build(evaluator=None, max_repair_rounds=1):
    retriever = FakeRetriever([make_tool("cap")])
    direct = DirectRuntime(retriever, FakeExecutor())
    return AgentOrchestrator(
        context_engine=FakeContextEngine(),
        behavior_gate=BehaviorGate(),
        direct_runtime=direct,
        planner_runtime=PlannerRuntime(direct, retriever),
        final_context_builder=FinalContextBuilder(),
        final_provider=CountingProvider(),
        answer_evaluator=evaluator,
        max_repair_rounds=max_repair_rounds,
    )


REQUEST = "What does the document say about pricing?"


def test_result_has_outcome_fields():
    result = run(build().run(REQUEST, user_id="u"))
    assert result.runtime_outcome == RuntimeOutcome.COMPLETED
    assert result.pending_action is None
    assert result.pending_reason is None


def test_passing_run_is_completed():
    result = run(build(ScriptedEvaluator([passing()])).run(REQUEST, user_id="u"))
    assert result.runtime_outcome == RuntimeOutcome.COMPLETED


def test_ask_user_waits_for_user_without_executing():
    orch = build(ScriptedEvaluator([failing(RepairAction.ASK_USER_FOR_CLARIFICATION)]))
    result = run(orch.run(REQUEST, user_id="u"))
    assert result.runtime_outcome == RuntimeOutcome.WAITING_FOR_USER
    assert result.pending_action == "ask_user_for_clarification"
    assert result.pending_reason
    # Deferred: exposed, not executed — the answer is still the single draft.
    assert result.answer.text == "draft 1"
    assert result.metadata["runtime_outcome"] == "waiting_for_user"


def test_replan_waits_for_replan():
    result = run(build(ScriptedEvaluator([failing(RepairAction.REPLAN)])).run(REQUEST, user_id="u"))
    assert result.runtime_outcome == RuntimeOutcome.WAITING_FOR_REPLAN
    assert result.pending_action == "replan"


def test_fail_gracefully_is_failed():
    result = run(build(ScriptedEvaluator([failing(RepairAction.FAIL_GRACEFULLY)])).run(REQUEST, user_id="u"))
    assert result.runtime_outcome == RuntimeOutcome.FAILED
    assert result.pending_action is None


def test_partial_is_completed_with_warning():
    result = run(build(ScriptedEvaluator([failing(RepairAction.RETURN_PARTIAL_WITH_WARNING)])).run(REQUEST, user_id="u"))
    assert result.runtime_outcome == RuntimeOutcome.COMPLETED_WITH_WARNING


def test_retrieve_more_context_waits_for_context():
    result = run(build(ScriptedEvaluator([failing(RepairAction.RETRIEVE_MORE_CONTEXT)])).run(REQUEST, user_id="u"))
    assert result.runtime_outcome == RuntimeOutcome.WAITING_FOR_CONTEXT
    assert result.pending_action == "retrieve_more_context"


def test_regenerate_then_pass_is_completed():
    evaluator = ScriptedEvaluator([failing(RepairAction.REGENERATE_WITH_STRONGER_INSTRUCTIONS), passing()])
    result = run(build(evaluator).run(REQUEST, user_id="u"))
    assert result.runtime_outcome == RuntimeOutcome.COMPLETED
    assert result.answer.text == "draft 2"
