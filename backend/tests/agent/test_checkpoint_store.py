"""Phase 24 tests — in-memory Checkpoint Store.

Config-free: RunContexts are hand-built; the store is in-memory with injectable
clock/id for determinism. No Mongo/Qdrant/Redis, no application settings, no LLM.
"""

import ast
import inspect
from datetime import datetime, timezone

import pytest

from app.agent.checkpoint import store as store_module
from app.agent.checkpoint.models import CheckpointRecord, CheckpointStatus
from app.agent.checkpoint.store import (
    CheckpointNotFoundError,
    InMemoryCheckpointStore,
    NonCheckpointableOutcomeError,
    is_checkpointable,
)
from app.agent.runtime.context import (
    BehaviorPath,
    BehaviorProfile,
    EvidenceItem,
    RunContext,
    ToolOutput,
    WorkingContextItem,
)
from app.agent.runtime.outcome import RuntimeOutcome


def waiting_run_context():
    rc = RunContext.create(
        "Summarize and email the team", user_id="u", thread_id="t1",
        working_context=[WorkingContextItem(source="thread_summary", content="prior")],
    )
    rc.attach_behavior_profile(BehaviorProfile(path=BehaviorPath.PLANNER, reason="multi"))
    rc.attach_selected_capabilities(["get_document_summary"])
    rc.append_tool_output(ToolOutput(capability_id="get_document_summary", output={"summary": "ok"}))
    rc.append_evidence(EvidenceItem(source="document_summary", content="summary text", score=0.7))
    rc.metadata["runtime_outcome"] = "waiting_for_user"
    return rc


class SeqClock:
    """Deterministic increasing clock."""

    def __init__(self):
        self._n = 0

    def __call__(self):
        self._n += 1
        return datetime(2026, 1, 1, 0, 0, self._n, tzinfo=timezone.utc)


def store():
    counter = {"n": 0}

    def ids():
        counter["n"] += 1
        return f"cp-{counter['n']}"

    return InMemoryCheckpointStore(clock=SeqClock(), id_factory=ids)


# --------------------------------------------------------------------------- #
# Save / load
# --------------------------------------------------------------------------- #

def test_save_returns_checkpoint_id():
    record = store().save(
        waiting_run_context(), RuntimeOutcome.WAITING_FOR_USER,
        pending_action="ask_user_for_clarification", pending_reason="need info",
    )
    assert isinstance(record, CheckpointRecord)
    assert record.checkpoint_id
    assert record.status == CheckpointStatus.ACTIVE


def test_load_returns_same_identity():
    s = store()
    rc = waiting_run_context()
    saved = s.save(rc, RuntimeOutcome.WAITING_FOR_USER)
    loaded = s.load(saved.checkpoint_id)
    assert loaded.run_id == rc.run_id
    assert loaded.user_id == "u"
    assert loaded.thread_id == "t1"
    assert loaded.run_context_snapshot["user_request"] == "Summarize and email the team"
    assert loaded.run_context_snapshot["working_context"][0]["content"] == "prior"


def test_pending_action_and_reason_preserved():
    s = store()
    saved = s.save(
        waiting_run_context(), RuntimeOutcome.WAITING_FOR_APPROVAL,
        pending_action="human_review", pending_reason="risky action",
    )
    loaded = s.load(saved.checkpoint_id)
    assert loaded.pending_action == "human_review"
    assert loaded.pending_reason == "risky action"
    assert loaded.runtime_outcome == RuntimeOutcome.WAITING_FOR_APPROVAL


def test_snapshot_includes_execution_and_evidence():
    saved = store().save(waiting_run_context(), RuntimeOutcome.WAITING_FOR_REPLAN)
    snap = saved.run_context_snapshot
    assert snap["selected_capabilities"] == ["get_document_summary"]
    assert snap["tool_outputs"][0]["capability_id"] == "get_document_summary"
    assert snap["evidence"][0]["content"] == "summary text"
    assert "execution_state" in snap
    assert snap["metadata"]["runtime_outcome"] == "waiting_for_user"


# --------------------------------------------------------------------------- #
# Lifecycle transitions
# --------------------------------------------------------------------------- #

def test_mark_resumed_updates_status():
    s = store()
    saved = s.save(waiting_run_context(), RuntimeOutcome.WAITING_FOR_USER)
    resumed = s.mark_resumed(saved.checkpoint_id)
    assert resumed.status == CheckpointStatus.RESUMED
    assert resumed.updated_at > saved.created_at
    assert s.load(saved.checkpoint_id).status == CheckpointStatus.RESUMED


def test_cancel_updates_status_and_reason():
    s = store()
    saved = s.save(waiting_run_context(), RuntimeOutcome.WAITING_FOR_USER)
    cancelled = s.cancel(saved.checkpoint_id, reason="user abandoned")
    assert cancelled.status == CheckpointStatus.CANCELLED
    assert cancelled.metadata["cancel_reason"] == "user abandoned"


# --------------------------------------------------------------------------- #
# Guarantees
# --------------------------------------------------------------------------- #

def test_save_does_not_mutate_working_context():
    rc = waiting_run_context()
    before = [w.content for w in rc.working_context]
    store().save(rc, RuntimeOutcome.WAITING_FOR_USER)
    assert [w.content for w in rc.working_context] == before
    assert len(rc.working_context) == 1


def test_snapshot_is_isolated_from_later_mutation():
    s = store()
    rc = waiting_run_context()
    saved = s.save(rc, RuntimeOutcome.WAITING_FOR_USER)
    rc.metadata["late_key"] = "added after save"
    assert "late_key" not in saved.run_context_snapshot["metadata"]


def test_missing_checkpoint_raises():
    with pytest.raises(CheckpointNotFoundError):
        store().load("does-not-exist")


def test_terminal_outcomes_are_not_checkpointable():
    assert is_checkpointable(RuntimeOutcome.WAITING_FOR_USER) is True
    assert is_checkpointable(RuntimeOutcome.COMPLETED) is False
    with pytest.raises(NonCheckpointableOutcomeError):
        store().save(waiting_run_context(), RuntimeOutcome.COMPLETED)
    with pytest.raises(NonCheckpointableOutcomeError):
        store().save(waiting_run_context(), RuntimeOutcome.FAILED)


def test_all_waiting_outcomes_checkpointable():
    for outcome in (
        RuntimeOutcome.WAITING_FOR_CONTEXT, RuntimeOutcome.WAITING_FOR_USER,
        RuntimeOutcome.WAITING_FOR_APPROVAL, RuntimeOutcome.WAITING_FOR_REPLAN,
    ):
        assert is_checkpointable(outcome) is True
        assert store().save(waiting_run_context(), outcome).status == CheckpointStatus.ACTIVE


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
    for module in (store_module, __import__("app.agent.checkpoint.models", fromlist=["x"])):
        targets = _module_level_import_targets(module)
        for banned in (
            "app.config", "app.services", "app.db", "motor", "pymongo", "redis",
            "qdrant", "openai", "anthropic", "genai", "llm",
        ):
            assert not any(banned in t for t in targets), (module.__name__, banned, targets)
