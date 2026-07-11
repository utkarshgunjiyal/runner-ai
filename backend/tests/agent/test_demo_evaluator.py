"""Phase 42B — DemoEvaluator: deterministic, genuine HITL for the seeded demo.

Config-free: drives the REAL orchestrator (deterministic providers) with the
DemoEvaluator wired onto the existing answer-evaluator seam. Proves that marked
demo prompts reach a genuine WAITING outcome and that a plain prompt still
completes — no Mongo/Qdrant/Redis, no LLM, no settings.
"""

import asyncio

from app.agent.demo import DemoEvaluator
from app.agent.evaluation.models import RepairAction
from app.agent.models.tool_spec import RiskLevel, SideEffectType, ToolKind, ToolSpec
from app.agent.runtime.context import RunContext
from app.agent.runtime.factory import build_default_runtime
from app.agent.runtime.outcome import RuntimeOutcome
from app.agent.tools.result import AdapterResult


def run(coro):
    return asyncio.run(coro)


class FakeContextEngine:
    async def build(self, user_request, user_id, thread_id=None, metadata=None):
        return RunContext.create(
            user_request=user_request, user_id=user_id, thread_id=thread_id,
            metadata=dict(metadata or {}),
        )


class FakeExecutor:
    async def execute(self, tool, args):
        return AdapterResult.ok(output={"answer": f"ran {tool.id}"})


def _orchestrator(evaluator=None):
    return build_default_runtime(
        context_engine=FakeContextEngine(),
        capability_executor=FakeExecutor(),
        answer_evaluator=evaluator,
    )


# --------------------------------------------------------------------------- #
# Unit: the evaluator maps requests to the right repair action
# --------------------------------------------------------------------------- #

def test_approval_keyword_requests_human_review():
    report = DemoEvaluator().evaluate(
        final_prompt=None, final_answer=None,
        run_context=RunContext.create(user_request="Delete all archived documents", user_id="u"),
    )
    assert report.passed is False
    assert report.repair_decision.action == RepairAction.HUMAN_REVIEW


def test_clarification_keyword_requests_clarification():
    report = DemoEvaluator().evaluate(
        final_prompt=None, final_answer=None,
        run_context=RunContext.create(user_request="Summarize the report", user_id="u"),
    )
    assert report.passed is False
    assert report.repair_decision.action == RepairAction.ASK_USER_FOR_CLARIFICATION


def test_plain_request_passes():
    report = DemoEvaluator().evaluate(
        final_prompt=None, final_answer=None,
        run_context=RunContext.create(user_request="What is the refund policy?", user_id="u"),
    )
    assert report.passed is True
    assert report.repair_decision.action == RepairAction.NONE


def test_matching_is_case_insensitive():
    report = DemoEvaluator().evaluate(
        final_prompt=None, final_answer=None,
        run_context=RunContext.create(user_request="Please DEPLOY the new build", user_id="u"),
    )
    assert report.repair_decision.action == RepairAction.HUMAN_REVIEW


# --------------------------------------------------------------------------- #
# Integration: a genuine pause through the real orchestrator
# --------------------------------------------------------------------------- #

def test_demo_run_reaches_waiting_for_approval():
    orch = _orchestrator(DemoEvaluator())
    result = run(orch.run("Delete all archived documents for finance", user_id="u"))
    assert result.runtime_outcome == RuntimeOutcome.WAITING_FOR_APPROVAL
    assert result.pending_action == "human_review"


def test_demo_run_reaches_waiting_for_user():
    orch = _orchestrator(DemoEvaluator())
    result = run(orch.run("Summarize the report", user_id="u"))
    assert result.runtime_outcome == RuntimeOutcome.WAITING_FOR_USER
    assert result.pending_action == "ask_user_for_clarification"


def test_plain_demo_run_completes_normally():
    orch = _orchestrator(DemoEvaluator())
    result = run(orch.run("What does the document say about pricing?", user_id="u"))
    assert result.runtime_outcome == RuntimeOutcome.COMPLETED
    assert result.answer.text


def test_without_demo_evaluator_never_pauses():
    # Off by default: no evaluator wired → even an approval keyword completes.
    orch = _orchestrator(evaluator=None)
    result = run(orch.run("Delete all archived documents", user_id="u"))
    assert result.runtime_outcome == RuntimeOutcome.COMPLETED


def test_resumed_run_does_not_re_pause():
    # A resumed run carries metadata['resume'] — the DemoEvaluator must accept it
    # (the human already approved), not re-trigger the same pause into a loop.
    rc = RunContext.create(user_request="Delete all archived documents", user_id="u")
    rc.metadata["resume"] = {"kind": "approval", "value": True}
    report = DemoEvaluator().evaluate(final_prompt=None, final_answer=None, run_context=rc)
    assert report.passed is True
    assert report.repair_decision.action == RepairAction.NONE


def test_full_approval_pause_then_resume_completes():
    # End-to-end via the coordinator: pause on the keyword, then resume completes
    # (no second pause) — the fix that keeps HITL from looping.
    import asyncio as _asyncio

    from app.agent.checkpoint.resume import ResumeResolution
    from app.agent.checkpoint.store import InMemoryCheckpointStore
    from app.agent.runtime.resume_coordinator import AsyncResumeCoordinator

    coord = AsyncResumeCoordinator(_orchestrator(DemoEvaluator()), InMemoryCheckpointStore())
    start = _asyncio.run(coord.start("Please delete the old records", "u"))
    assert start.result.runtime_outcome == RuntimeOutcome.WAITING_FOR_APPROVAL
    resumed = _asyncio.run(coord.resume(start.checkpoint_id, ResumeResolution(kind="approval", value=True)))
    assert resumed.result.runtime_outcome == RuntimeOutcome.COMPLETED
