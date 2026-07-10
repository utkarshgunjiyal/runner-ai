"""Phase 33 tests — POST /agent/run/stream (SSE).

The route is mounted on a bare FastAPI app and get_runtime_streamer is overridden
so the runtime executes without a DB or a real LLM. Verifies the SSE wire format,
event envelope/ordering, failure frame, planner-only events, chunk reassembly,
and that no internal objects leak. Config-free.
"""

import ast
import inspect
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent.capabilities.models import CapabilityMatch, CapabilityRetrievalResponse
from app.agent.context.final_builder import FinalContextBuilder
from app.agent.gate.behavior_gate import BehaviorGate
from app.agent.llm.final_provider import DeterministicFinalProvider
from app.agent.models.tool_spec import RiskLevel, SideEffectType, ToolKind, ToolSpec
from app.agent.runtime.context import EvidenceItem, RunContext
from app.agent.runtime.direct_runtime import DirectRuntime
from app.agent.runtime.orchestrator import AgentOrchestrator
from app.agent.runtime.planner_runtime import ExecutionPlan, PlannerRuntime, PlannerTask
from app.agent.runtime.streaming import RuntimeStreamer
from app.agent.tools.result import AdapterResult
from app.routes import agent as agent_module
from app.routes.agent import get_runtime_streamer, router


DIRECT_REQUEST = "What does the document say about pricing?"
PLANNER_REQUEST = "Summarize the report and then email the team"


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
        return AdapterResult.ok(output={"answer": "x"}, evidence=[EvidenceItem(source="document", content="g")])


class FailingOrchestrator:
    async def run(self, *a, **kw):
        raise RuntimeError("boom")


def _orchestrator(plan_source=None):
    retriever = FakeRetriever([make_tool("cap")])
    direct = DirectRuntime(retriever, FakeExecutor())
    return AgentOrchestrator(
        context_engine=FakeContextEngine(),
        behavior_gate=BehaviorGate(),
        direct_runtime=direct,
        planner_runtime=PlannerRuntime(direct, retriever),
        final_context_builder=FinalContextBuilder(),
        final_provider=DeterministicFinalProvider(),
        plan_source=plan_source,
    )


def client(streamer):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_runtime_streamer] = lambda: streamer
    return TestClient(app)


def parse_sse(text):
    """Return a list of (event_type, data_dict) from an SSE body."""
    frames = []
    for block in text.strip().split("\n\n"):
        if not block.strip():
            continue
        event_type = None
        data = None
        for line in block.splitlines():
            if line.startswith("event:"):
                event_type = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = json.loads(line[len("data:"):].strip())
        frames.append((event_type, data))
    return frames


# --------------------------------------------------------------------------- #
# Wire format + envelope
# --------------------------------------------------------------------------- #

def test_content_type_is_event_stream():
    resp = client(RuntimeStreamer(_orchestrator())).post("/agent/run/stream", json={"user_request": DIRECT_REQUEST})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")


def test_runtime_started_first_and_completed_last():
    resp = client(RuntimeStreamer(_orchestrator())).post("/agent/run/stream", json={"user_request": DIRECT_REQUEST})
    frames = parse_sse(resp.text)
    types = [t for t, _ in frames]
    assert types[0] == "runtime_started"
    assert types[-1] == "runtime_completed"
    # each frame carries a serialized RuntimeEvent
    assert frames[0][1]["type"] == "runtime_started"
    assert "sequence" in frames[0][1]


def test_runtime_failed_terminates_stream():
    frames = parse_sse(client(RuntimeStreamer(FailingOrchestrator())).post(
        "/agent/run/stream", json={"user_request": DIRECT_REQUEST}).text)
    types = [t for t, _ in frames]
    assert types == ["runtime_started", "runtime_failed"]
    assert "runtime_completed" not in types
    assert frames[-1][1]["data"]["error_type"] == "RuntimeError"


# --------------------------------------------------------------------------- #
# Planner-only events + chunk reassembly
# --------------------------------------------------------------------------- #

def test_planner_events_only_for_planner_path():
    def plan(rc):
        return ExecutionPlan(id="p", goal=rc.user_request, tasks=[PlannerTask(id="t1", request="summarize")])

    direct = [t for t, _ in parse_sse(client(RuntimeStreamer(_orchestrator())).post(
        "/agent/run/stream", json={"user_request": DIRECT_REQUEST}).text)]
    planner = [t for t, _ in parse_sse(client(RuntimeStreamer(_orchestrator(plan_source=plan))).post(
        "/agent/run/stream", json={"user_request": PLANNER_REQUEST}).text)]
    assert "planner_started" not in direct
    assert "planner_started" in planner


def test_chunk_events_reconstruct_answer():
    streamer = RuntimeStreamer(_orchestrator(), chunk_answer=True, chunk_size=8)
    frames = parse_sse(client(streamer).post("/agent/run/stream", json={"user_request": DIRECT_REQUEST}).text)
    chunks = [d["data"]["text"] for t, d in frames if t == "answer_chunk"]
    completed = next(d for t, d in frames if t == "answer_completed")
    assert chunks
    assert "".join(chunks) == completed["data"]["text"]


# --------------------------------------------------------------------------- #
# No internal leakage + DI
# --------------------------------------------------------------------------- #

def test_stream_does_not_expose_internal_objects():
    body = client(RuntimeStreamer(_orchestrator())).post(
        "/agent/run/stream", json={"user_request": DIRECT_REQUEST}).text
    for leaked in ("run_context", "working_context", "final_prompt", "execution_plan", "evaluation_report"):
        assert leaked not in body


def test_dependency_injection_uses_injected_streamer():
    # A custom-configured streamer (chunking) proves the injected instance is used.
    frames = parse_sse(client(RuntimeStreamer(_orchestrator(), chunk_answer=True)).post(
        "/agent/run/stream", json={"user_request": DIRECT_REQUEST}).text)
    assert any(t == "answer_chunk" for t, _ in frames)


def test_blank_user_request_is_422():
    resp = client(RuntimeStreamer(_orchestrator())).post("/agent/run/stream", json={"user_request": ""})
    assert resp.status_code == 422


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
