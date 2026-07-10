"""Phase 41B tests — streamed WAITING_* run is resumable end-to-end.

A run streamed over POST /agent/run/stream that pauses (WAITING_FOR_USER) must
surface a ``checkpoint_id`` in its terminal event and be resumable via
POST /agent/resume over the SAME shared store — the flow the HITL UI depends on.
Config-free: real route + async coordinator + in-memory store, no DB/LLM.
"""

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent.capabilities.models import CapabilityMatch, CapabilityRetrievalResponse
from app.agent.checkpoint.store import InMemoryCheckpointStore
from app.agent.context.final_builder import FinalContextBuilder
from app.agent.evaluation.models import EvaluationReport, RepairAction, RepairDecision
from app.agent.gate.behavior_gate import BehaviorGate
from app.agent.llm.final_provider import DeterministicFinalProvider
from app.agent.models.tool_spec import RiskLevel, SideEffectType, ToolKind, ToolSpec
from app.agent.runtime.context import EvidenceItem, RunContext
from app.agent.runtime.direct_runtime import DirectRuntime
from app.agent.runtime.orchestrator import AgentOrchestrator
from app.agent.runtime.planner_runtime import PlannerRuntime
from app.agent.runtime.resume_coordinator import AsyncResumeCoordinator
from app.agent.runtime.streaming import RuntimeStreamer
from app.agent.tools.result import AdapterResult
from app.routes.agent import get_resume_coordinator, get_runtime_streamer, router


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
    def _resp(self, q):
        return CapabilityRetrievalResponse(query=q, matches=[CapabilityMatch(tool=make_tool("cap"), score=1.0)])

    def retrieve(self, request):
        return self._resp(request.query)

    def retrieve_for_run_context(self, run_context, *, top_k=5, **kw):
        return self._resp(run_context.user_request)


class FakeExecutor:
    async def execute(self, tool, args):
        return AdapterResult.ok(output={"a": 1}, evidence=[EvidenceItem(source="document", content="g")])


class ScriptedEvaluator:
    def __init__(self, reports):
        self._reports = list(reports)
        self.calls = 0

    def evaluate(self, final_prompt, final_answer, run_context=None):
        report = self._reports[min(self.calls, len(self._reports) - 1)]
        self.calls += 1
        return report


def waiting():
    return EvaluationReport(passed=False, overall_score=0.2,
                            repair_decision=RepairDecision(action=RepairAction.ASK_USER_FOR_CLARIFICATION,
                                                           reason="need info", max_attempts=5))


def passing():
    return EvaluationReport(passed=True, overall_score=0.9,
                            repair_decision=RepairDecision(action=RepairAction.NONE))


def client(evaluator):
    retriever = FakeRetriever()
    direct = DirectRuntime(retriever, FakeExecutor())
    orch = AgentOrchestrator(
        context_engine=FakeContextEngine(), behavior_gate=BehaviorGate(),
        direct_runtime=direct, planner_runtime=PlannerRuntime(direct, retriever),
        final_context_builder=FinalContextBuilder(),
        final_provider=DeterministicFinalProvider(), answer_evaluator=evaluator,
    )
    coordinator = AsyncResumeCoordinator(orch, InMemoryCheckpointStore())
    app = FastAPI()
    app.include_router(router)
    # Streamer + resume share ONE coordinator/store (as production wires them),
    # so the streamed checkpoint is the one /agent/resume reads.
    app.dependency_overrides[get_resume_coordinator] = lambda: coordinator
    app.dependency_overrides[get_runtime_streamer] = lambda: RuntimeStreamer(
        orch, checkpointer=coordinator.checkpoint_result
    )
    return TestClient(app)


def parse_sse(text):
    frames = []
    for block in text.strip().split("\n\n"):
        etype = data = None
        for line in block.splitlines():
            if line.startswith("event:"):
                etype = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:].strip())
        if etype:
            frames.append((etype, data))
    return frames


def test_streamed_waiting_run_surfaces_checkpoint_and_resumes():
    c = client(ScriptedEvaluator([waiting(), passing()]))

    frames = parse_sse(c.post("/agent/run/stream", json={"user_request": "What does the report say?"}).text)
    types = [t for t, _ in frames]
    assert types[-1] == "runtime_completed"
    terminal = frames[-1][1]
    assert terminal["data"]["runtime_outcome"] == "waiting_for_user"
    checkpoint_id = terminal["data"]["checkpoint_id"]
    assert checkpoint_id  # streamed waiting run IS resumable

    # Resume the exact checkpoint the stream produced.
    resumed = c.post("/agent/resume", json={
        "checkpoint_id": checkpoint_id,
        "resolution": {"kind": "clarification", "value": "the Q3 report"},
    })
    assert resumed.status_code == 200
    body = resumed.json()
    assert body["runtime_outcome"] == "completed"
    assert isinstance(body["answer"], str) and body["answer"]
    assert body["checkpoint_id"] is None


def test_streamed_completed_run_has_null_checkpoint():
    c = client(ScriptedEvaluator([passing()]))
    frames = parse_sse(c.post("/agent/run/stream", json={"user_request": "hello"}).text)
    terminal = frames[-1][1]
    assert terminal["data"]["runtime_outcome"] == "completed"
    assert terminal["data"]["checkpoint_id"] is None
