"""Phase 25 tests — RunContext rehydration + Resume Runtime.

Config-free: snapshots come from the Phase 24 store; rehydration/resume are pure
data-layer. No Mongo/Qdrant/Redis, no application settings, no LLM, no execution.
"""

import ast
import inspect

import pytest

from app.agent.checkpoint import rehydrate as rehydrate_module
from app.agent.checkpoint import resume as resume_module
from app.agent.checkpoint.models import CheckpointStatus
from app.agent.checkpoint.rehydrate import rehydrate_run_context
from app.agent.checkpoint.resume import ResumeKind, ResumeResolution, ResumeRuntime
from app.agent.checkpoint.store import (
    CheckpointNotFoundError,
    InMemoryCheckpointStore,
    snapshot_run_context,
)
from app.agent.models.execution import StepExecutionResult, StepStatus
from app.agent.models.plan import FinalResponseMode, Plan, PlanStep, PlanStepType
from app.agent.runtime.context import (
    BehaviorPath,
    BehaviorProfile,
    EvidenceItem,
    RunContext,
    ToolOutput,
    WorkingContextItem,
)
from app.agent.runtime.outcome import RuntimeOutcome


def rich_run_context():
    rc = RunContext.create(
        "Summarize the report and email the team", user_id="u", thread_id="t1",
        working_context=[
            WorkingContextItem(source="thread_summary", content="prior", metadata={"seq": 2}),
            WorkingContextItem(source="recent_message", content="do it"),
        ],
    )
    rc.attach_behavior_profile(BehaviorProfile(path=BehaviorPath.PLANNER, reason="multi", confidence=0.8))
    rc.attach_selected_capabilities(["get_document_summary", "get_job_status"])
    rc.append_tool_output(ToolOutput(capability_id="get_document_summary", output={"summary": "s"}))
    rc.append_evidence(EvidenceItem(source="document_summary", content="text", score=0.7, metadata={"page": 1}))
    rc.execution_state.record_result(
        StepExecutionResult(step_id="t1", capability_id="get_document_summary", status=StepStatus.SUCCEEDED)
    )
    rc.attach_plan(
        Plan(
            id="plan-1", user_goal="do it", intent="multi",
            steps=[PlanStep(id="t1", step_type=PlanStepType.TOOL, capability_id="get_document_summary",
                            description="summarize")],
            final_response_mode=FinalResponseMode.SUMMARIZE_RESULTS,
        )
    )
    rc.metadata["runtime_outcome"] = "waiting_for_user"
    rc.metadata["pending"] = {"x": 1}
    return rc


def stored():
    store = InMemoryCheckpointStore()
    rc = rich_run_context()
    record = store.save(
        rc, RuntimeOutcome.WAITING_FOR_USER,
        pending_action="ask_user_for_clarification", pending_reason="need info",
    )
    return store, record, rc


# --------------------------------------------------------------------------- #
# Rehydration
# --------------------------------------------------------------------------- #

def test_rehydrate_preserves_identity():
    rc = rich_run_context()
    snap = snapshot_run_context(rc)
    restored = rehydrate_run_context(snap)
    assert restored.run_id == rc.run_id
    assert restored.user_id == "u"
    assert restored.thread_id == "t1"
    assert restored.user_request == "Summarize the report and email the team"


def test_rehydrate_restores_working_context():
    restored = rehydrate_run_context(snapshot_run_context(rich_run_context()))
    assert [w.content for w in restored.working_context] == ["prior", "do it"]
    assert restored.working_context[0].metadata["seq"] == 2


def test_rehydrate_restores_tool_outputs_and_evidence():
    restored = rehydrate_run_context(snapshot_run_context(rich_run_context()))
    assert restored.tool_outputs[0].capability_id == "get_document_summary"
    assert restored.evidence[0].content == "text"
    assert restored.evidence[0].score == 0.7


def test_rehydrate_restores_behavior_profile_and_capabilities():
    restored = rehydrate_run_context(snapshot_run_context(rich_run_context()))
    assert restored.behavior_profile.path == BehaviorPath.PLANNER
    assert restored.behavior_profile.confidence == 0.8
    assert restored.selected_capabilities == ["get_document_summary", "get_job_status"]


def test_rehydrate_restores_execution_state_and_plan():
    restored = rehydrate_run_context(snapshot_run_context(rich_run_context()))
    assert restored.execution_state.completed_steps == ["t1"]
    assert restored.execution_state.get_result("t1").status == StepStatus.SUCCEEDED
    assert restored.plan is not None
    assert restored.plan.id == "plan-1"


def test_rehydrate_restores_metadata():
    restored = rehydrate_run_context(snapshot_run_context(rich_run_context()))
    assert restored.metadata["runtime_outcome"] == "waiting_for_user"
    assert restored.metadata["pending"] == {"x": 1}


def test_round_trip_snapshot_is_stable():
    snap1 = snapshot_run_context(rich_run_context())
    snap2 = snapshot_run_context(rehydrate_run_context(snap1))
    assert snap2 == snap1


def test_rehydrate_does_not_mutate_original_snapshot():
    import copy
    snap = snapshot_run_context(rich_run_context())
    original = copy.deepcopy(snap)  # reference copy of the same snapshot
    restored = rehydrate_run_context(snap)
    restored.metadata["mutated"] = True
    restored.working_context  # property returns a copy
    assert snap == original  # untouched


# --------------------------------------------------------------------------- #
# Resume Runtime
# --------------------------------------------------------------------------- #

def test_resume_marks_checkpoint_resumed():
    store, record, _ = stored()
    ResumeRuntime().resume(store, record.checkpoint_id,
                           ResumeResolution(kind=ResumeKind.CLARIFICATION, value="use plan B"))
    assert store.load(record.checkpoint_id).status == CheckpointStatus.RESUMED


def test_resume_injects_approval_resolution():
    store, record, _ = stored()
    rc = ResumeRuntime().resume(
        store, record.checkpoint_id,
        ResumeResolution(kind=ResumeKind.APPROVAL, value=True, reason="ok"),
    )
    resume = rc.metadata["resume"]
    assert resume["kind"] == "approval"
    assert resume["value"] is True
    assert resume["reason"] == "ok"
    assert resume["checkpoint_id"] == record.checkpoint_id
    assert resume["pending_action"] == "ask_user_for_clarification"
    assert resume["runtime_outcome"] == "waiting_for_user"


def test_resume_injects_clarification_resolution():
    store, record, _ = stored()
    rc = ResumeRuntime().resume(
        store, record.checkpoint_id,
        ResumeResolution(kind=ResumeKind.CLARIFICATION, value="the Q3 report", metadata={"src": "user"}),
    )
    resume = rc.metadata["resume"]
    assert resume["kind"] == "clarification"
    assert resume["value"] == "the Q3 report"
    assert resume["metadata"] == {"src": "user"}


def test_resume_returns_restored_run_context():
    store, record, rc = stored()
    restored = ResumeRuntime().resume(store, record.checkpoint_id,
                                      ResumeResolution(kind=ResumeKind.APPROVAL, value=True))
    assert restored.run_id == rc.run_id
    assert [w.content for w in restored.working_context] == ["prior", "do it"]


def test_resume_missing_checkpoint_raises():
    store = InMemoryCheckpointStore()
    with pytest.raises(CheckpointNotFoundError):
        ResumeRuntime().resume(store, "nope", ResumeResolution(kind=ResumeKind.APPROVAL, value=True))


def test_resume_does_not_mutate_stored_snapshot():
    store, record, _ = stored()
    snapshot_before = store.load(record.checkpoint_id).run_context_snapshot
    rc = ResumeRuntime().resume(store, record.checkpoint_id,
                                ResumeResolution(kind=ResumeKind.APPROVAL, value=True))
    rc.metadata["resume_side_effect"] = "x"
    # The stored snapshot must not have gained the resume/side-effect keys.
    stored_snapshot = store.load(record.checkpoint_id).run_context_snapshot
    assert "resume" not in stored_snapshot["metadata"]
    assert "resume_side_effect" not in stored_snapshot["metadata"]
    assert stored_snapshot == snapshot_before


def test_working_context_immutable_after_resume():
    store, record, _ = stored()
    rc = ResumeRuntime().resume(store, record.checkpoint_id,
                                ResumeResolution(kind=ResumeKind.APPROVAL, value=True))
    before = [w.content for w in rc.working_context]
    rc.metadata["resume"]["value"] = "changed"
    assert [w.content for w in rc.working_context] == before
    assert len(rc.working_context) == 2


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
    for module in (rehydrate_module, resume_module):
        targets = _module_level_import_targets(module)
        for banned in (
            "app.config", "app.services", "app.db", "motor", "pymongo", "redis",
            "qdrant", "openai", "anthropic", "genai", "llm",
        ):
            assert not any(banned in t for t in targets), (module.__name__, banned, targets)
