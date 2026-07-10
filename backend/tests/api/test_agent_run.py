"""Phase 30 tests — POST /agent/run.

The route is mounted on a bare FastAPI app (not app.main, which needs config) and
the current-user + resume-coordinator dependencies are overridden so the runtime
executes without a database or a real LLM. Config-free.
"""

import ast
import inspect

import pytest
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
from app.agent.runtime.resume_coordinator import ResumeCoordinator
from app.agent.tools.result import AdapterResult
from app.routes import agent as agent_module
from app.routes.agent import get_current_user, get_resume_coordinator, resolve_user_id, router


# --------------------------------------------------------------------------- #
# Test doubles (no DB, no LLM)
# --------------------------------------------------------------------------- #

class FakeContextEngine:
    async def build(self, user_request, user_id, thread_id=None, metadata=None):
        return RunContext.create(user_request, user_id=user_id, thread_id=thread_id,
                                 metadata=dict(metadata or {}))


def make_tool(tool_id):
    return ToolSpec(
        id=tool_id, name=tool_id, kind=ToolKind.INTERNAL, description=f"{tool_id} tool",
        input_schema={}, output_schema={}, risk_level=RiskLevel.LOW,
        side_effects=SideEffectType.READ, requires_approval=False,
    )


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


class ScriptedEvaluator:
    def __init__(self, reports):
        self._reports = list(reports)
        self.calls = 0

    def evaluate(self, final_prompt, final_answer, run_context=None):
        report = self._reports[min(self.calls, len(self._reports) - 1)]
        self.calls += 1
        return report


def _coordinator(evaluator=None):
    retriever = FakeRetriever([make_tool("cap")])
    direct = DirectRuntime(retriever, FakeExecutor())
    orch = AgentOrchestrator(
        context_engine=FakeContextEngine(),
        behavior_gate=BehaviorGate(),
        direct_runtime=direct,
        planner_runtime=PlannerRuntime(direct, retriever),
        final_context_builder=FinalContextBuilder(),
        final_provider=DeterministicFinalProvider(),
        answer_evaluator=evaluator,
    )
    return ResumeCoordinator(orch, InMemoryCheckpointStore())


def client(evaluator=None, user=None):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_resume_coordinator] = lambda: _coordinator(evaluator)
    if user is not None:
        app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def waiting():
    return EvaluationReport(passed=False, overall_score=0.2,
                            repair_decision=RepairDecision(action=RepairAction.ASK_USER_FOR_CLARIFICATION,
                                                           reason="need info", max_attempts=5))


# --------------------------------------------------------------------------- #
# Completed run
# --------------------------------------------------------------------------- #

def test_run_returns_200_and_answer_for_completed():
    resp = client().post("/agent/run", json={"user_request": "What does the document say about pricing?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["runtime_outcome"] == "completed"
    assert body["run_id"]
    assert isinstance(body["answer"], str) and body["answer"]
    assert body["checkpoint_id"] is None


def test_response_includes_run_id_and_outcome_and_thread():
    resp = client().post("/agent/run", json={"user_request": "hello there", "thread_id": "t1"})
    body = resp.json()
    assert "run_id" in body and "runtime_outcome" in body
    assert body["thread_id"] == "t1"


def test_response_does_not_expose_internal_run_context():
    body = client().post("/agent/run", json={"user_request": "hello there"}).json()
    for leaked in ("run_context", "working_context", "final_prompt", "evidence", "tool_outputs"):
        assert leaked not in body
    assert set(body.keys()) == {
        "run_id", "thread_id", "runtime_outcome", "answer",
        "checkpoint_id", "pending_action", "pending_reason", "metadata",
    }


# --------------------------------------------------------------------------- #
# Waiting run
# --------------------------------------------------------------------------- #

def test_waiting_run_returns_checkpoint_and_pending():
    body = client(evaluator=ScriptedEvaluator([waiting()])).post(
        "/agent/run", json={"user_request": "What does the document say?"}).json()
    assert body["runtime_outcome"] == "waiting_for_user"
    assert body["checkpoint_id"]
    assert body["pending_action"] == "ask_user_for_clarification"
    assert body["pending_reason"]
    assert body["answer"] is None  # no answer surfaced while waiting


# --------------------------------------------------------------------------- #
# Validation + auth
# --------------------------------------------------------------------------- #

def test_empty_user_request_is_validation_error():
    assert client().post("/agent/run", json={"user_request": ""}).status_code == 422
    assert client().post("/agent/run", json={"user_request": "   "}).status_code == 422
    assert client().post("/agent/run", json={}).status_code == 422


def test_resolve_user_id_variants():
    assert resolve_user_id({"user_id": "u1"}) == "u1"
    assert resolve_user_id({"id": "u2"}) == "u2"
    assert resolve_user_id({"_id": "u3"}) == "u3"

    class U:
        user_id = "u4"

    assert resolve_user_id(U()) == "u4"
    assert resolve_user_id(None) == "dev_user"


def test_custom_user_dependency_is_used():
    # Just proves the auth dependency is overridable; the response stays API-safe.
    resp = client(user={"user_id": "alice"}).post("/agent/run", json={"user_request": "hi there"})
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# Hygiene
# --------------------------------------------------------------------------- #

def test_route_module_imports_config_free():
    tree = ast.parse(inspect.getsource(agent_module))
    targets = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            targets += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            targets.append(node.module or "")
    for banned in ("app.config", "app.database", "motor", "app.services"):
        assert not any(banned in t for t in targets), (banned, targets)
