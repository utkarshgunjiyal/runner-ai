"""Phase 37 tests — provider failures over HTTP (/agent/run + /agent/run/stream).

Config-free: injected failing providers via an overridden coordinator/streamer.
Verifies failures become safe responses (never raw 500 / vendor leakage) and the
SSE stream terminates with runtime_failed.
"""

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent.capabilities.models import CapabilityMatch, CapabilityRetrievalResponse
from app.agent.checkpoint.store import InMemoryCheckpointStore
from app.agent.context.final_builder import FinalContextBuilder
from app.agent.gate.behavior_gate import BehaviorGate
from app.agent.llm.final_provider import DeterministicFinalProvider
from app.agent.llm.planner_provider import DeterministicPlannerProvider, PlannerProviderError
from app.agent.llm.provider_adapter import FinalProviderError, ProviderUnavailableError
from app.agent.models.tool_spec import RiskLevel, SideEffectType, ToolKind, ToolSpec
from app.agent.runtime.context import RunContext
from app.agent.runtime.direct_runtime import DirectRuntime
from app.agent.runtime.orchestrator import AgentOrchestrator
from app.agent.runtime.planner_runtime import PlannerRuntime
from app.agent.runtime.resume_coordinator import AsyncResumeCoordinator
from app.agent.runtime.streaming import RuntimeStreamer
from app.agent.tools.result import AdapterResult
from app.routes.agent import get_resume_coordinator, get_runtime_streamer, router


DIRECT = "What does the document say about pricing?"
PLANNER = "Summarize the report and then email the team"
VENDOR_SECRET = "sk-vendor-SECRET-42"


def make_tool(tid):
    return ToolSpec(id=tid, name=tid, kind=ToolKind.INTERNAL, description="t",
                    input_schema={}, output_schema={}, risk_level=RiskLevel.LOW,
                    side_effects=SideEffectType.READ, requires_approval=False)


class FakeContextEngine:
    async def build(self, user_request, user_id, thread_id=None, metadata=None):
        return RunContext.create(user_request, user_id=user_id, thread_id=thread_id)


class FakeRetriever:
    def _r(self, q):
        return CapabilityRetrievalResponse(query=q, matches=[CapabilityMatch(tool=make_tool("cap"), score=1.0)])

    def retrieve(self, request):
        return self._r(request.query)

    def retrieve_for_run_context(self, run_context, *, top_k=5, **kw):
        return self._r(run_context.user_request)


class FakeExecutor:
    async def execute(self, tool, args):
        return AdapterResult.ok(output={"a": 1})


class RaisingPlanner:
    async def plan(self, planner_prompt):
        raise ProviderUnavailableError(VENDOR_SECRET)


class RaisingFinal:
    provider = "boom"
    model = "boom"

    def __init__(self, exc):
        self._exc = exc

    async def generate(self, final_prompt):
        raise self._exc


def orchestrator(*, planner_provider=None, final_provider=None):
    retriever = FakeRetriever()
    direct = DirectRuntime(retriever, FakeExecutor())
    return AgentOrchestrator(
        context_engine=FakeContextEngine(), behavior_gate=BehaviorGate(),
        direct_runtime=direct, planner_runtime=PlannerRuntime(direct, retriever),
        final_context_builder=FinalContextBuilder(),
        final_provider=final_provider or DeterministicFinalProvider(),
        planner_provider=planner_provider or DeterministicPlannerProvider(),
        capability_retriever=retriever,
    )


def client(orch):
    app = FastAPI()
    app.include_router(router)
    coordinator = AsyncResumeCoordinator(orch, InMemoryCheckpointStore())
    app.dependency_overrides[get_resume_coordinator] = lambda: coordinator
    app.dependency_overrides[get_runtime_streamer] = lambda: RuntimeStreamer(orch)
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


# --------------------------------------------------------------------------- #
# /agent/run
# --------------------------------------------------------------------------- #

def test_final_provider_failure_is_200_failed_not_500():
    resp = client(orchestrator(final_provider=RaisingFinal(ProviderUnavailableError(VENDOR_SECRET)))).post(
        "/agent/run", json={"user_request": DIRECT})
    assert resp.status_code == 200  # graceful, not a 500
    body = resp.json()
    assert body["runtime_outcome"] == "failed"
    assert body["metadata"]["failure_stage"] == "final_provider"
    assert body["metadata"]["retryable"] is True
    assert VENDOR_SECRET not in resp.text  # no vendor leakage anywhere


def test_planner_provider_failure_is_200_failed():
    resp = client(orchestrator(planner_provider=RaisingPlanner())).post(
        "/agent/run", json={"user_request": PLANNER})
    assert resp.status_code == 200
    body = resp.json()
    assert body["runtime_outcome"] == "failed"
    assert body["metadata"]["failure_stage"] == "planner_provider"
    assert VENDOR_SECRET not in resp.text


def test_failure_response_stays_api_safe():
    body = client(orchestrator(final_provider=RaisingFinal(FinalProviderError("x")))).post(
        "/agent/run", json={"user_request": DIRECT}).json()
    assert set(body.keys()) == {
        "run_id", "thread_id", "runtime_outcome", "answer",
        "checkpoint_id", "pending_action", "pending_reason", "metadata",
    }
    for leaked in ("run_context", "final_prompt", "planner_prompt"):
        assert leaked not in resp_text(body)


def resp_text(body):
    return json.dumps(body)


# --------------------------------------------------------------------------- #
# /agent/run/stream
# --------------------------------------------------------------------------- #

def test_stream_emits_runtime_failed_on_final_provider_failure():
    frames = parse_sse(client(orchestrator(final_provider=RaisingFinal(FinalProviderError(VENDOR_SECRET)))).post(
        "/agent/run/stream", json={"user_request": DIRECT}).text)
    types = [t for t, _ in frames]
    assert types[0] == "runtime_started"
    assert types[-1] == "runtime_failed"
    assert "runtime_completed" not in types
    failed = frames[-1][1]
    assert failed["data"]["failure_stage"] == "final_provider"
    assert VENDOR_SECRET not in json.dumps(frames)


def test_stream_emits_runtime_failed_on_planner_failure():
    frames = parse_sse(client(orchestrator(planner_provider=RaisingPlanner())).post(
        "/agent/run/stream", json={"user_request": PLANNER}).text)
    types = [t for t, _ in frames]
    assert types[-1] == "runtime_failed"
    assert "runtime_completed" not in types
