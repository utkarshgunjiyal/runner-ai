"""Phase 43 — run recorder wiring on /agent/run (thread ownership + persistence).

Config-free: bare app + fake coordinator + fake recorder installed via
configure_run_recorder. No DB, no LLM.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent.checkpoint.store import InMemoryCheckpointStore
from app.agent.context.final_builder import FinalContextBuilder
from app.agent.gate.behavior_gate import BehaviorGate
from app.agent.llm.final_provider import DeterministicFinalProvider
from app.agent.models.tool_spec import RiskLevel, SideEffectType, ToolKind, ToolSpec
from app.agent.persistence import ThreadOwnershipError
from app.agent.capabilities.models import CapabilityMatch, CapabilityRetrievalResponse
from app.agent.runtime.context import RunContext
from app.agent.runtime.direct_runtime import DirectRuntime
from app.agent.runtime.orchestrator import AgentOrchestrator
from app.agent.runtime.planner_runtime import PlannerRuntime
from app.agent.runtime.resume_coordinator import ResumeCoordinator
from app.agent.tools.result import AdapterResult
from app.routes.agent import (
    configure_run_recorder,
    get_current_user,
    get_resume_coordinator,
    router,
)


class FakeContextEngine:
    async def build(self, user_request, user_id, thread_id=None, metadata=None):
        return RunContext.create(user_request, user_id=user_id, thread_id=thread_id,
                                 metadata=dict(metadata or {}))


class FakeRetriever:
    def _resp(self, q):
        tool = ToolSpec(id="cap", name="cap", kind=ToolKind.INTERNAL, description="c",
                        input_schema={}, output_schema={}, risk_level=RiskLevel.LOW,
                        side_effects=SideEffectType.READ, requires_approval=False)
        return CapabilityRetrievalResponse(query=q, matches=[CapabilityMatch(tool=tool, score=1.0)])

    def retrieve(self, request):
        return self._resp(request.query)

    def retrieve_for_run_context(self, run_context, **kw):
        return self._resp(run_context.user_request)


class FakeExecutor:
    async def execute(self, tool, args):
        return AdapterResult.ok(output={"a": 1})


def _coordinator():
    retriever = FakeRetriever()
    direct = DirectRuntime(retriever, FakeExecutor())
    orch = AgentOrchestrator(
        context_engine=FakeContextEngine(), behavior_gate=BehaviorGate(),
        direct_runtime=direct, planner_runtime=PlannerRuntime(direct, retriever),
        final_context_builder=FinalContextBuilder(), final_provider=DeterministicFinalProvider(),
    )
    return ResumeCoordinator(orch, InMemoryCheckpointStore())


class RecordingRecorder:
    def __init__(self, owned_threads=None, new_thread_id="t-new"):
        self.owned = set(owned_threads or [])
        self.new_thread_id = new_thread_id
        self.before_calls = []
        self.after_calls = []

    async def before_run(self, user_id, thread_id, user_request):
        self.before_calls.append((user_id, thread_id, user_request))
        if thread_id is None:
            return self.new_thread_id
        if thread_id not in self.owned:
            raise ThreadOwnershipError("not yours")
        return thread_id

    async def after_run(self, user_id, thread_id, outcome):
        self.after_calls.append((user_id, thread_id, outcome))


def _client(recorder, user=None):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_resume_coordinator] = _coordinator
    if user is not None:
        app.dependency_overrides[get_current_user] = lambda: user
    configure_run_recorder(recorder)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_recorder():
    yield
    configure_run_recorder(None)  # never leak the recorder across tests


def test_run_persists_user_and_assistant_messages():
    recorder = RecordingRecorder(owned_threads={"t1"})
    resp = _client(recorder).post("/agent/run", json={"user_request": "hello", "thread_id": "t1"})
    assert resp.status_code == 200
    assert recorder.before_calls == [("dev_user", "t1", "hello")]
    assert len(recorder.after_calls) == 1
    _, thread_id, outcome = recorder.after_calls[0]
    assert thread_id == "t1"
    assert outcome.runtime_outcome == "completed"
    assert isinstance(outcome.answer_text, str) and outcome.answer_text


def test_run_rejects_unowned_thread_with_404():
    recorder = RecordingRecorder(owned_threads={"mine"})
    resp = _client(recorder).post("/agent/run", json={"user_request": "x", "thread_id": "someone_else"})
    assert resp.status_code == 404
    assert recorder.after_calls == []  # never executed the run


def test_run_creates_thread_when_none_supplied():
    recorder = RecordingRecorder()
    resp = _client(recorder).post("/agent/run", json={"user_request": "x"})
    assert resp.status_code == 200
    # The recorder-created thread id is used for the run + assistant persistence.
    assert recorder.after_calls[0][1] == "t-new"


def test_no_recorder_configured_is_unaffected():
    # Default: no recorder → run still works, no persistence.
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_resume_coordinator] = _coordinator
    configure_run_recorder(None)
    resp = TestClient(app).post("/agent/run", json={"user_request": "x", "thread_id": "anything"})
    assert resp.status_code == 200


def test_selected_document_ids_accepted_in_contract():
    recorder = RecordingRecorder(owned_threads={"t1"})
    resp = _client(recorder).post(
        "/agent/run",
        json={"user_request": "compare them", "thread_id": "t1",
              "selected_document_ids": ["d1", "d2"], "explicit_context_mode": "selected"},
    )
    assert resp.status_code == 200
