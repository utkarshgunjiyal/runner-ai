"""Phase 27 tests — ResumeCoordinator (in-memory pause/resume loop).

Config-free: real orchestrator (fake context engine + fake retrieval/execution),
scripted evaluator to drive waiting outcomes, in-memory checkpoint store. No
Mongo/Qdrant/Redis, no application settings, no LLM.
"""

import ast
import asyncio
import inspect

from app.agent.capabilities.models import CapabilityMatch, CapabilityRetrievalResponse
from app.agent.checkpoint.models import CheckpointStatus
from app.agent.checkpoint.resume import ResumeKind, ResumeResolution
from app.agent.checkpoint.store import InMemoryCheckpointStore
from app.agent.context.final_builder import FinalContextBuilder
from app.agent.evaluation.models import EvaluationReport, RepairAction, RepairDecision
from app.agent.gate.behavior_gate import BehaviorGate
from app.agent.llm.final_provider import FinalAnswer
from app.agent.models.tool_spec import RiskLevel, SideEffectType, ToolKind, ToolSpec
from app.agent.runtime import resume_coordinator as coordinator_module
from app.agent.runtime.context import EvidenceItem, RunContext
from app.agent.runtime.direct_runtime import DirectRuntime
from app.agent.runtime.orchestrator import AgentOrchestrator
from app.agent.runtime.outcome import RuntimeOutcome
from app.agent.runtime.planner_runtime import PlannerRuntime
from app.agent.runtime.resume_coordinator import ResumeCoordinator, ResumeCoordinatorResult
from app.agent.tools.result import AdapterResult


def run(coro):
    return asyncio.run(coro)


REQUEST = "What does the document say about pricing?"


def make_tool(tool_id):
    return ToolSpec(
        id=tool_id, name=tool_id, kind=ToolKind.INTERNAL, description=f"{tool_id} tool",
        input_schema={}, output_schema={}, risk_level=RiskLevel.LOW,
        side_effects=SideEffectType.READ, requires_approval=False,
    )


class FakeContextEngine:
    async def build(self, user_request, user_id, thread_id=None, metadata=None):
        return RunContext.create(user_request, user_id=user_id, thread_id=thread_id,
                                 metadata=dict(metadata or {}))


class FakeRetriever:
    def __init__(self, tools):
        self._tools = tools

    def _resp(self, q):
        return CapabilityRetrievalResponse(query=q, matches=[CapabilityMatch(tool=t, score=1.0) for t in self._tools])

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


def passing():
    return EvaluationReport(passed=True, overall_score=0.9,
                            repair_decision=RepairDecision(action=RepairAction.NONE))


def failing(action):
    return EvaluationReport(passed=False, overall_score=0.2,
                            repair_decision=RepairDecision(action=action, reason="need it", max_attempts=5))


def coordinator(evaluator=None):
    retriever = FakeRetriever([make_tool("cap")])
    direct = DirectRuntime(retriever, FakeExecutor())
    orch = AgentOrchestrator(
        context_engine=FakeContextEngine(),
        behavior_gate=BehaviorGate(),
        direct_runtime=direct,
        planner_runtime=PlannerRuntime(direct, retriever),
        final_context_builder=FinalContextBuilder(),
        final_provider=CountingProvider(),
        answer_evaluator=evaluator,
    )
    store = InMemoryCheckpointStore()
    return ResumeCoordinator(orch, store), store


# --------------------------------------------------------------------------- #
# start(): checkpoint only on WAITING_*
# --------------------------------------------------------------------------- #

def test_completed_run_does_not_checkpoint():
    coord, store = coordinator(ScriptedEvaluator([passing()]))
    out = run(coord.start(REQUEST, user_id="u"))
    assert isinstance(out, ResumeCoordinatorResult)
    assert out.result.runtime_outcome == RuntimeOutcome.COMPLETED
    assert out.checkpoint_id is None


def test_waiting_for_user_checkpoints():
    coord, store = coordinator(ScriptedEvaluator([failing(RepairAction.ASK_USER_FOR_CLARIFICATION)]))
    out = run(coord.start(REQUEST, user_id="u"))
    assert out.result.runtime_outcome == RuntimeOutcome.WAITING_FOR_USER
    assert out.checkpoint_id is not None
    record = store.load(out.checkpoint_id)
    assert record.status == CheckpointStatus.ACTIVE


def test_waiting_for_approval_checkpoints():
    coord, store = coordinator(ScriptedEvaluator([failing(RepairAction.HUMAN_REVIEW)]))
    out = run(coord.start(REQUEST, user_id="u"))
    assert out.result.runtime_outcome == RuntimeOutcome.WAITING_FOR_APPROVAL
    assert out.checkpoint_id is not None


def test_pending_action_and_reason_preserved_in_checkpoint():
    coord, store = coordinator(ScriptedEvaluator([failing(RepairAction.ASK_USER_FOR_CLARIFICATION)]))
    out = run(coord.start(REQUEST, user_id="u"))
    record = store.load(out.checkpoint_id)
    assert record.pending_action == "ask_user_for_clarification"
    assert record.pending_reason == out.result.pending_reason


# --------------------------------------------------------------------------- #
# resume(): load → continue_run → maybe re-checkpoint
# --------------------------------------------------------------------------- #

def test_resume_completes_without_new_checkpoint():
    coord, store = coordinator(
        ScriptedEvaluator([failing(RepairAction.ASK_USER_FOR_CLARIFICATION), passing()]))
    started = run(coord.start(REQUEST, user_id="u"))
    cid = started.checkpoint_id

    resumed = run(coord.resume(cid, ResumeResolution(kind=ResumeKind.CLARIFICATION, value="the Q3 report")))
    assert resumed.result.runtime_outcome == RuntimeOutcome.COMPLETED
    assert resumed.resumed_checkpoint_id == cid
    assert resumed.checkpoint_id is None  # completed → no new checkpoint
    # The consumed checkpoint was marked resumed by ResumeRuntime.
    assert store.load(cid).status == CheckpointStatus.RESUMED


def test_resume_that_waits_again_creates_new_checkpoint():
    coord, store = coordinator(
        ScriptedEvaluator([
            failing(RepairAction.ASK_USER_FOR_CLARIFICATION),
            failing(RepairAction.ASK_USER_FOR_CLARIFICATION),
        ]))
    started = run(coord.start(REQUEST, user_id="u"))
    cid = started.checkpoint_id

    resumed = run(coord.resume(cid, ResumeResolution(kind=ResumeKind.CLARIFICATION, value="more")))
    assert resumed.result.runtime_outcome == RuntimeOutcome.WAITING_FOR_USER
    assert resumed.checkpoint_id is not None
    assert resumed.checkpoint_id != cid          # a fresh checkpoint
    assert resumed.resumed_checkpoint_id == cid
    assert store.load(cid).status == CheckpointStatus.RESUMED
    assert store.load(resumed.checkpoint_id).status == CheckpointStatus.ACTIVE


def test_resume_calls_continue_run_not_context_engine():
    # continue_run preserves run_id; a fresh run() would mint a new one.
    coord, store = coordinator(
        ScriptedEvaluator([failing(RepairAction.ASK_USER_FOR_CLARIFICATION), passing()]))
    started = run(coord.start(REQUEST, user_id="u"))
    resumed = run(coord.resume(started.checkpoint_id,
                               ResumeResolution(kind=ResumeKind.CLARIFICATION, value="x")))
    assert resumed.result.run_id == started.result.run_id
    assert resumed.result.metadata.get("resumed") is True


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
    targets = _module_level_import_targets(coordinator_module)
    for banned in (
        "app.config", "app.services", "app.db", "motor", "pymongo", "redis",
        "qdrant", "openai", "anthropic", "genai", "llm_provider",
    ):
        assert not any(banned in t for t in targets), (banned, targets)
