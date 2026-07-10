"""Phase 21 tests — Repair Runtime.

Config-free: RepairDecisions/FinalPrompts/RunContexts are hand-built; the runtime
is fully deterministic. No Mongo/Qdrant/Redis, no application settings, no LLM,
no tool execution.
"""

import ast
import inspect

from app.agent.evaluation.models import (
    EvaluationReport,
    RepairAction,
    RepairDecision,
)
from app.agent.llm.final_provider import FinalAnswer
from app.agent.models.final_prompt import ExecutionSummary, FinalPrompt
from app.agent.repair import runtime as runtime_module
from app.agent.repair.models import RepairResult
from app.agent.repair.runtime import RepairRuntime
from app.agent.runtime.context import RunContext, WorkingContextItem


def final_prompt(instructions="answer using the evidence"):
    return FinalPrompt(
        system_prompt="system",
        user_request="What does the document say?",
        execution_summary=ExecutionSummary(path="direct", status="success"),
        final_instructions=instructions,
    )


def answer(text="a draft answer"):
    return FinalAnswer(text=text, provider="deterministic", model="fake")


def report(action, *, reason="grounding failed", max_attempts=2, passed=False):
    return EvaluationReport(
        passed=passed, overall_score=0.3,
        repair_decision=RepairDecision(action=action, reason=reason, max_attempts=max_attempts),
    )


def run_context(**kw):
    return RunContext.create("What does the document say?", user_id="u", **kw)


def repair(action, *, rc=None, prompt=None, **report_kw):
    rc = rc or run_context()
    return RepairRuntime().repair(rc, prompt or final_prompt(), answer(), report(action, **report_kw))


# --------------------------------------------------------------------------- #
# No-op
# --------------------------------------------------------------------------- #

def test_none_action_is_noop():
    result = repair(RepairAction.NONE, passed=True)
    assert isinstance(result, RepairResult)
    assert result.applied is False
    assert result.updated_final_prompt is None
    assert result.updated_run_context is None
    assert result.target_stage is None


# --------------------------------------------------------------------------- #
# Local repairs
# --------------------------------------------------------------------------- #

def test_regenerate_with_same_context_keeps_prompt():
    p = final_prompt("original instructions")
    result = repair(RepairAction.REGENERATE_WITH_SAME_CONTEXT, prompt=p)
    assert result.applied is True
    assert result.target_stage == "final_provider"
    # Instructions unchanged; repair metadata attached.
    assert result.updated_final_prompt.final_instructions == "original instructions"
    assert result.updated_final_prompt.metadata["repair"]["action"] == "regenerate_with_same_context"


def test_regenerate_with_stronger_instructions_adds_directive():
    p = final_prompt("original instructions")
    result = repair(RepairAction.REGENERATE_WITH_STRONGER_INSTRUCTIONS, prompt=p)
    assert result.applied is True
    new = result.updated_final_prompt.final_instructions
    assert "original instructions" in new
    assert "REPAIR" in new
    assert len(new) > len("original instructions")


def test_return_partial_with_warning_records_warning():
    rc = run_context()
    result = repair(RepairAction.RETURN_PARTIAL_WITH_WARNING, rc=rc)
    assert result.applied is True
    assert result.target_stage == "orchestrator"
    assert result.metadata["status"] == "partial"
    assert "warning" in result.metadata
    assert rc.metadata["repair_warning"]


def test_fail_gracefully_records_failure():
    rc = run_context()
    result = repair(RepairAction.FAIL_GRACEFULLY, rc=rc)
    assert result.applied is True
    assert result.metadata["status"] == "failed"
    assert rc.metadata["repair_failure"]


# --------------------------------------------------------------------------- #
# Deferred hand-offs
# --------------------------------------------------------------------------- #

def test_retrieve_more_context_targets_context_engine():
    result = repair(RepairAction.RETRIEVE_MORE_CONTEXT)
    assert result.applied is False
    assert result.target_stage == "context_engine"


def test_rerun_capability_targets_direct_runtime():
    result = repair(RepairAction.RERUN_CAPABILITY)
    assert result.applied is False
    assert result.target_stage == "direct_runtime"


def test_replan_targets_planner():
    result = repair(RepairAction.REPLAN)
    assert result.applied is False
    assert result.target_stage == "planner"


def test_ask_user_for_clarification_waits_for_input():
    result = repair(RepairAction.ASK_USER_FOR_CLARIFICATION)
    assert result.applied is False
    assert result.metadata["status"] == "waiting"
    assert "input" in result.metadata["waiting_for"]


def test_human_review_waits_for_approval():
    result = repair(RepairAction.HUMAN_REVIEW)
    assert result.applied is False
    assert result.metadata["status"] == "waiting"
    assert "approval" in result.metadata["waiting_for"]


# --------------------------------------------------------------------------- #
# Bounded repair
# --------------------------------------------------------------------------- #

def test_regenerate_is_bounded_by_max_attempts():
    rc = run_context()
    # First two attempts apply, then attempts are exhausted (max_attempts=2).
    r1 = repair(RepairAction.REGENERATE_WITH_SAME_CONTEXT, rc=rc, max_attempts=2)
    r2 = repair(RepairAction.REGENERATE_WITH_SAME_CONTEXT, rc=rc, max_attempts=2)
    r3 = repair(RepairAction.REGENERATE_WITH_SAME_CONTEXT, rc=rc, max_attempts=2)
    assert r1.applied is True and r2.applied is True
    assert r3.applied is False
    assert r3.metadata.get("exhausted") is True


# --------------------------------------------------------------------------- #
# Immutability + hygiene
# --------------------------------------------------------------------------- #

def test_working_context_remains_immutable():
    rc = run_context(working_context=[WorkingContextItem(source="thread_summary", content="prior")])
    before = [w.content for w in rc.working_context]
    repair(RepairAction.REGENERATE_WITH_STRONGER_INSTRUCTIONS, rc=rc)
    repair(RepairAction.RETURN_PARTIAL_WITH_WARNING, rc=rc)
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
    for module in (runtime_module, __import__("app.agent.repair.models", fromlist=["x"])):
        targets = _module_level_import_targets(module)
        for banned in (
            "app.config", "app.services", "app.db", "motor", "redis", "qdrant",
            "openai", "anthropic", "google.generativeai", "genai", "llm",
        ):
            assert not any(banned in t for t in targets), (module.__name__, banned, targets)
