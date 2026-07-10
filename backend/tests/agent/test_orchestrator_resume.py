"""Phase 26 tests — AgentOrchestrator.continue_run (resume continuation).

Config-free: a rehydrated RunContext (as ResumeRuntime produces) is continued
without re-entering the ContextEngine. Counting provider + scripted evaluator
keep it deterministic. No Mongo/Qdrant/Redis, no application settings, no LLM.
"""

import ast
import asyncio
import inspect

from app.agent.capabilities.models import CapabilityMatch, CapabilityRetrievalResponse
from app.agent.checkpoint.resume import ResumeKind, ResumeResolution, ResumeRuntime
from app.agent.checkpoint.store import InMemoryCheckpointStore
from app.agent.context.final_builder import FinalContextBuilder
from app.agent.evaluation.models import EvaluationReport, RepairAction, RepairDecision
from app.agent.gate.behavior_gate import BehaviorGate
from app.agent.llm.final_provider import FinalAnswer
from app.agent.models.tool_spec import RiskLevel, SideEffectType, ToolKind, ToolSpec
from app.agent.runtime import orchestrator as orchestrator_module
from app.agent.runtime.context import (
    BehaviorPath,
    BehaviorProfile,
    EvidenceItem,
    RunContext,
    ToolOutput,
    WorkingContextItem,
)
from app.agent.runtime.direct_runtime import DirectRuntime
from app.agent.runtime.orchestrator import AgentOrchestrator
from app.agent.runtime.outcome import RuntimeOutcome
from app.agent.runtime.planner_runtime import PlannerRuntime
from app.agent.tools.result import AdapterResult


def run(coro):
    return asyncio.run(coro)


def make_tool(tool_id):
    return ToolSpec(
        id=tool_id, name=tool_id, kind=ToolKind.INTERNAL, description=f"{tool_id} tool",
        input_schema={}, output_schema={}, risk_level=RiskLevel.LOW,
        side_effects=SideEffectType.READ, requires_approval=False,
    )


class SpyContextEngine:
    def __init__(self):
        self.build_calls = 0

    async def build(self, user_request, user_id, thread_id=None, metadata=None):
        self.build_calls += 1
        return RunContext.create(user_request, user_id=user_id, thread_id=thread_id)


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
        return AdapterResult.ok(output={"a": 1})


class CountingProvider:
    provider = "deterministic"
    model = "fake"

    def __init__(self):
        self.calls = 0
        self.last_prompt = None

    async def generate(self, final_prompt):
        self.calls += 1
        self.last_prompt = final_prompt
        return FinalAnswer(text=f"draft {self.calls}", used_citations=[], provider=self.provider, model=self.model)


class ScriptedEvaluator:
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


def failing(action):
    return EvaluationReport(passed=False, overall_score=0.2,
                            repair_decision=RepairDecision(action=action, reason="bad", max_attempts=5))


def build(evaluator=None):
    retriever = FakeRetriever([make_tool("cap")])
    direct = DirectRuntime(retriever, FakeExecutor())
    context_engine = SpyContextEngine()
    provider = CountingProvider()
    orch = AgentOrchestrator(
        context_engine=context_engine,
        behavior_gate=BehaviorGate(),
        direct_runtime=direct,
        planner_runtime=PlannerRuntime(direct, retriever),
        final_context_builder=FinalContextBuilder(),
        final_provider=provider,
        answer_evaluator=evaluator,
    )
    return orch, context_engine, provider


def paused_run_context(outcome_value, pending_action):
    """A RunContext shaped like one that ResumeRuntime just rehydrated."""
    rc = RunContext.create("What does the report say?", user_id="u", thread_id="t1",
                           working_context=[WorkingContextItem(source="thread_summary", content="prior")])
    rc.attach_behavior_profile(BehaviorProfile(path=BehaviorPath.DIRECT, reason="doc qa"))
    rc.append_tool_output(ToolOutput(capability_id="search_documents", output={"hits": []}))
    rc.append_evidence(EvidenceItem(source="document", content="ev", score=0.8))
    rc.metadata["runtime_outcome"] = outcome_value
    return rc


def resolved(outcome_value, pending_action, resolution):
    """Persist + resume so the RunContext carries metadata['resume'] like Phase 25."""
    store = InMemoryCheckpointStore()
    rc = paused_run_context(outcome_value, pending_action)
    outcome = RuntimeOutcome(outcome_value)
    record = store.save(rc, outcome, pending_action=pending_action, pending_reason="waiting")
    return ResumeRuntime().resume(store, record.checkpoint_id, resolution)


# --------------------------------------------------------------------------- #
# Continuation basics
# --------------------------------------------------------------------------- #

def test_continue_run_preserves_run_id_and_skips_context_engine():
    rc = resolved("waiting_for_user", "ask_user_for_clarification",
                  ResumeResolution(kind=ResumeKind.CLARIFICATION, value="the Q3 report"))
    orch, context_engine, provider = build()
    result = run(orch.continue_run(rc))
    assert result.run_id == rc.run_id
    assert context_engine.build_calls == 0  # never rebuilds initial context
    assert provider.calls == 1


def test_clarification_reaches_final_prompt_metadata():
    rc = resolved("waiting_for_user", "ask_user_for_clarification",
                  ResumeResolution(kind=ResumeKind.CLARIFICATION, value="the Q3 report"))
    orch, _, provider = build()
    result = run(orch.continue_run(rc))
    assert result.final_prompt.metadata["resume"]["value"] == "the Q3 report"
    assert "RESUME" in result.final_prompt.final_instructions
    assert "the Q3 report" in provider.last_prompt.final_instructions
    assert result.metadata["resume_kind"] == "clarification"


def test_approval_reaches_final_prompt_metadata():
    rc = resolved("waiting_for_approval", "human_review",
                  ResumeResolution(kind=ResumeKind.APPROVAL, value=True, reason="ok"))
    orch, _, _ = build()
    result = run(orch.continue_run(rc))
    assert result.final_prompt.metadata["resume"]["kind"] == "approval"
    assert result.metadata["resumed"] is True


def test_final_answer_attached_after_continuation():
    rc = resolved("waiting_for_user", "ask_user_for_clarification",
                  ResumeResolution(kind=ResumeKind.CLARIFICATION, value="x"))
    orch, _, _ = build()
    result = run(orch.continue_run(rc))
    assert result.run_context.metadata["final_answer"]["text"] == result.answer.text
    assert result.runtime_outcome == RuntimeOutcome.COMPLETED  # no evaluator → completed


def test_evaluation_runs_on_continuation_when_evaluator_present():
    rc = resolved("waiting_for_user", "ask_user_for_clarification",
                  ResumeResolution(kind=ResumeKind.CLARIFICATION, value="x"))
    evaluator = ScriptedEvaluator([failing(RepairAction.REGENERATE_WITH_STRONGER_INSTRUCTIONS), passing()])
    orch, _, provider = build(evaluator=evaluator)
    result = run(orch.continue_run(rc))
    assert provider.calls == 2  # initial continuation + one repair regeneration
    assert result.metadata["evaluation_passed"] is True
    assert "answer_evaluation" in result.run_context.metadata


# --------------------------------------------------------------------------- #
# Deferred continuations (not executed)
# --------------------------------------------------------------------------- #

def test_waiting_for_context_is_deferred_not_executed():
    rc = resolved("waiting_for_context", "retrieve_more_context",
                  ResumeResolution(kind=ResumeKind.CONTEXT_AVAILABLE, value={"doc": "d1"}))
    orch, _, provider = build()
    result = run(orch.continue_run(rc))
    assert result.runtime_outcome == RuntimeOutcome.WAITING_FOR_CONTEXT
    assert result.metadata["deferred"] is True
    assert result.pending_action == "retrieve_more_context"
    assert provider.calls == 0  # no generation faked


def test_waiting_for_replan_is_deferred_not_executed():
    rc = resolved("waiting_for_replan", "replan",
                  ResumeResolution(kind=ResumeKind.REPLAN_REQUESTED, value=True))
    orch, _, provider = build()
    result = run(orch.continue_run(rc))
    assert result.runtime_outcome == RuntimeOutcome.WAITING_FOR_REPLAN
    assert result.metadata["deferred"] is True
    assert provider.calls == 0


# --------------------------------------------------------------------------- #
# Immutability + hygiene
# --------------------------------------------------------------------------- #

def test_working_context_immutable_after_continuation():
    rc = resolved("waiting_for_user", "ask_user_for_clarification",
                  ResumeResolution(kind=ResumeKind.CLARIFICATION, value="x"))
    orch, _, _ = build()
    before = [w.content for w in rc.working_context]
    run(orch.continue_run(rc))
    assert [w.content for w in rc.working_context] == before
    assert len(rc.working_context) == 1


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
        "app.config", "app.services", "app.db", "motor", "pymongo", "redis",
        "qdrant", "openai", "anthropic", "genai", "llm_provider",
    )
    for name in banned:
        assert not any(name in t for t in targets), (name, targets)
