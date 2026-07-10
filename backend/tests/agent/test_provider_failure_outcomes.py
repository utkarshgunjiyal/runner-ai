"""Phase 37 tests — provider failures become safe RuntimeOutcomes.

Config-free: injected failing providers; no LLM, no credentials. Verifies the
orchestrator boundary converts domain provider errors into typed, API-safe
results without leaking vendor detail and without executing guessed plans.
"""

import asyncio

from app.agent.capabilities.models import CapabilityMatch, CapabilityRetrievalResponse
from app.agent.context.final_builder import FinalContextBuilder
from app.agent.gate.behavior_gate import BehaviorGate
from app.agent.llm.final_provider import DeterministicFinalProvider
from app.agent.llm.planner_provider import (
    DeterministicPlannerProvider,
    PlannerOutputParseError,
    PlannerOutputValidationError,
    PlannerProviderError,
)
from app.agent.llm.provider_adapter import FinalProviderError, ProviderUnavailableError
from app.agent.models.tool_spec import RiskLevel, SideEffectType, ToolKind, ToolSpec
from app.agent.runtime.context import RunContext
from app.agent.runtime.direct_runtime import DirectRuntime
from app.agent.runtime.orchestrator import AgentOrchestrator
from app.agent.runtime.outcome import RuntimeOutcome
from app.agent.runtime.planner_runtime import PlannerRuntime
from app.agent.tools.result import AdapterResult


def run(coro):
    return asyncio.run(coro)


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
    def __init__(self, exc):
        self._exc = exc
        self.calls = 0

    async def plan(self, planner_prompt):
        self.calls += 1
        raise self._exc


class RaisingFinalProvider:
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


# --------------------------------------------------------------------------- #
# Planner failures
# --------------------------------------------------------------------------- #

def test_direct_path_never_invokes_planner_provider():
    spy = RaisingPlanner(ProviderUnavailableError("x"))
    result = run(orchestrator(planner_provider=spy).run(DIRECT, user_id="u"))
    assert spy.calls == 0
    assert result.runtime_outcome == RuntimeOutcome.COMPLETED


def test_planner_unavailable_is_failed():
    result = run(orchestrator(planner_provider=RaisingPlanner(ProviderUnavailableError(VENDOR_SECRET))).run(PLANNER, user_id="u"))
    assert result.runtime_outcome == RuntimeOutcome.FAILED
    assert result.metadata["failure_stage"] == "planner_provider"
    assert result.metadata["retryable"] is True
    assert result.metadata["planner_error_type"] == "ProviderUnavailableError"


def test_planner_parse_error_fails_without_execution():
    spy = RaisingPlanner(PlannerOutputParseError("garbage"))
    result = run(orchestrator(planner_provider=spy).run(PLANNER, user_id="u"))
    assert result.runtime_outcome == RuntimeOutcome.FAILED
    # no tool outputs → no guessed/partial plan executed
    assert result.run_context.tool_outputs == []
    assert result.metadata["error_code"] == "planner_output_parse_error"


def test_planner_validation_error_waits_for_user():
    spy = RaisingPlanner(PlannerOutputValidationError("unknown capability"))
    result = run(orchestrator(planner_provider=spy).run(PLANNER, user_id="u"))
    assert result.runtime_outcome == RuntimeOutcome.WAITING_FOR_USER
    assert result.pending_action == "ask_user_for_clarification"
    assert result.metadata["clarification_needed"] is True
    assert result.run_context.tool_outputs == []  # no plan executed


def test_planner_failure_does_not_leak_vendor_detail():
    result = run(orchestrator(planner_provider=RaisingPlanner(PlannerProviderError(VENDOR_SECRET))).run(PLANNER, user_id="u"))
    assert VENDOR_SECRET not in result.answer.text
    assert VENDOR_SECRET not in str(result.metadata)
    assert VENDOR_SECRET not in (result.pending_reason or "")


# --------------------------------------------------------------------------- #
# Final-provider failures
# --------------------------------------------------------------------------- #

def test_final_provider_unavailable_is_failed():
    result = run(orchestrator(final_provider=RaisingFinalProvider(ProviderUnavailableError(VENDOR_SECRET))).run(DIRECT, user_id="u"))
    assert result.runtime_outcome == RuntimeOutcome.FAILED
    assert result.metadata["failure_stage"] == "final_provider"
    assert result.metadata["retryable"] is True
    assert VENDOR_SECRET not in result.answer.text


def test_final_provider_error_is_failed_and_safe():
    result = run(orchestrator(final_provider=RaisingFinalProvider(FinalProviderError(VENDOR_SECRET))).run(DIRECT, user_id="u"))
    assert result.runtime_outcome == RuntimeOutcome.FAILED
    assert result.metadata["error_code"] == "final_provider_error"
    assert result.answer.text  # a concise, safe message
    assert VENDOR_SECRET not in str(result.metadata)


def test_evaluation_not_run_on_provider_failure():
    # An evaluator that would raise if called proves evaluation is skipped.
    class ExplodingEvaluator:
        def evaluate(self, *a, **k):
            raise AssertionError("evaluation must not run on a failed draft")

    retriever = FakeRetriever()
    direct = DirectRuntime(retriever, FakeExecutor())
    orch = AgentOrchestrator(
        context_engine=FakeContextEngine(), behavior_gate=BehaviorGate(),
        direct_runtime=direct, planner_runtime=PlannerRuntime(direct, retriever),
        final_context_builder=FinalContextBuilder(),
        final_provider=RaisingFinalProvider(FinalProviderError("x")),
        planner_provider=DeterministicPlannerProvider(), capability_retriever=retriever,
        answer_evaluator=ExplodingEvaluator(),
    )
    result = run(orch.run(DIRECT, user_id="u"))
    assert result.runtime_outcome == RuntimeOutcome.FAILED
    assert "answer_evaluation" not in result.run_context.metadata


def test_programming_bug_still_propagates():
    # A non-domain error is NOT swallowed (avoid hiding real bugs).
    class BuggyFinal:
        provider = "b"
        model = "b"

        async def generate(self, final_prompt):
            raise KeyError("real bug")

    try:
        run(orchestrator(final_provider=BuggyFinal()).run(DIRECT, user_id="u"))
        assert False, "expected the bug to propagate"
    except KeyError:
        pass
