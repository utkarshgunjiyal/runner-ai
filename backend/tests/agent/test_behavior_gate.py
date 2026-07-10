"""Phase 12 tests — Behavior Gate."""

from app.agent.gate.behavior_gate import (
    BehaviorDecision,
    BehaviorGate,
    BehaviorType,
    EstimatedComplexity,
)
from app.agent.runtime.context import BehaviorPath, RunContext, WorkingContextItem


def classify(text, working_context=None):
    return BehaviorGate().classify(text, working_context=working_context)


# --------------------------------------------------------------------------- #
# Direct path
# --------------------------------------------------------------------------- #

def test_document_qa_routes_direct():
    d = classify("What does the document say about the projects?")
    assert d.path == BehaviorPath.DIRECT
    assert d.behavior_type == BehaviorType.DOCUMENT_QA
    assert d.requires_planner is False


def test_job_status_routes_direct():
    d = classify("Is my document done processing yet?")
    assert d.path == BehaviorPath.DIRECT
    assert d.behavior_type == BehaviorType.JOB_STATUS


def test_preference_update_routes_direct():
    d = classify("Remember that I prefer concise answers")
    assert d.path == BehaviorPath.DIRECT
    assert d.behavior_type == BehaviorType.PREFERENCE_UPDATE


def test_general_chat_routes_direct():
    d = classify("Hello, how are you today?")
    assert d.path == BehaviorPath.DIRECT
    assert d.behavior_type == BehaviorType.GENERAL_CHAT
    assert d.estimated_steps == 1
    assert d.estimated_complexity == EstimatedComplexity.LOW


def test_simple_memory_question_routes_direct():
    d = classify("What did we discuss earlier?")
    assert d.path == BehaviorPath.DIRECT
    assert d.behavior_type == BehaviorType.MEMORY_QUESTION


# --------------------------------------------------------------------------- #
# Planner path
# --------------------------------------------------------------------------- #

def test_multi_step_goal_routes_planner():
    d = classify("Summarize the report and then email it to the team")
    assert d.path == BehaviorPath.PLANNER
    assert d.behavior_type == BehaviorType.MULTI_STEP
    assert d.requires_planner is True
    assert d.requires_external_capabilities is True
    assert d.estimated_steps >= 2


def test_email_action_routes_planner():
    d = classify("Send an email to John about the meeting")
    assert d.path == BehaviorPath.PLANNER
    assert d.behavior_type == BehaviorType.ACTION
    assert d.requires_external_capabilities is True


def test_compare_and_draft_routes_planner():
    d = classify("Compare Q1 and Q2 revenue and draft a summary email")
    assert d.path == BehaviorPath.PLANNER
    assert d.behavior_type == BehaviorType.COMPARE_ACTION
    assert d.estimated_complexity == EstimatedComplexity.HIGH


def test_ambiguous_high_complexity_routes_planner():
    text = (
        "I have a situation where several unrelated things are happening across "
        "different areas at once, and I am not sure how it all connects, so help me "
        "figure out the overall picture, what matters most, what matters least, and "
        "how the pieces fit together across everything I mentioned"
    )
    d = classify(text)
    assert d.path == BehaviorPath.PLANNER
    assert d.behavior_type == BehaviorType.AMBIGUOUS_COMPLEX
    assert d.estimated_complexity == EstimatedComplexity.HIGH


def test_active_execution_biases_planner_when_uncertain():
    wc = [WorkingContextItem(source="active_execution_state", content="plan running")]
    d = classify("keep going", working_context=wc)
    assert d.path == BehaviorPath.PLANNER
    assert d.signals.get("active_execution") is True


# --------------------------------------------------------------------------- #
# Decision shape + RunContext attachment
# --------------------------------------------------------------------------- #

def test_decision_includes_confidence_reason_signals():
    d = classify("Send an email to the team")
    assert 0.0 < d.confidence <= 1.0
    assert isinstance(d.reason, str) and d.reason
    assert isinstance(d.signals, dict)
    assert "action_verbs" in d.signals


def test_run_context_gets_decision_attached():
    rc = RunContext.create("Remember that I prefer dark mode", user_id="u")
    decision = BehaviorGate().decide(rc)
    # BehaviorProfile (Phase 10A shape) attached
    assert rc.behavior_profile is not None
    assert rc.behavior_profile.path == BehaviorPath.DIRECT
    assert rc.behavior_profile.confidence == decision.confidence
    # richer decision stored in metadata
    assert "behavior_decision" in rc.metadata
    assert rc.metadata["behavior_decision"]["behavior_type"] == BehaviorType.PREFERENCE_UPDATE.value


def test_decide_without_attach_does_not_touch_run_context():
    rc = RunContext.create("Hello there", user_id="u")
    BehaviorGate().decide(rc, attach=False)
    assert rc.behavior_profile is None
    assert "behavior_decision" not in rc.metadata


def test_decide_returns_behavior_decision_type():
    rc = RunContext.create("What is X?", user_id="u")
    assert isinstance(BehaviorGate().decide(rc), BehaviorDecision)
