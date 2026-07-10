"""Phase 16 tests — Final Context Builder.

Config-free: builds FinalPrompts from hand-constructed RunContexts (as the
DIRECT and PLANNER runtimes would leave them). No Mongo/Qdrant/Redis, no
application settings, no LLM.
"""

import ast
import inspect

from app.agent.context import final_builder as builder_module
from app.agent.context.final_builder import FinalContextBuilder
from app.agent.models.execution import StepExecutionResult, StepStatus
from app.agent.models.final_prompt import FinalPrompt
from app.agent.runtime.context import (
    BehaviorPath,
    BehaviorProfile,
    EvidenceItem,
    RunContext,
    ToolOutput,
    WorkingContextItem,
)


def direct_run_context():
    rc = RunContext.create(
        "What does the resume say about pricing?",
        user_id="u",
        thread_id="t1",
        working_context=[
            WorkingContextItem(source="thread_summary", content="earlier we discussed billing"),
            WorkingContextItem(source="recent_message", content="tell me about pricing", metadata={"seq": 5}),
        ],
    )
    rc.attach_behavior_profile(BehaviorProfile(path=BehaviorPath.DIRECT, reason="doc qa", confidence=0.85))
    rc.attach_selected_capabilities(["search_documents"])
    rc.append_tool_output(
        ToolOutput(capability_id="search_documents", output={"hits": [{"text": "price is $10"}]},
                   metadata={"confidence": 0.9})
    )
    rc.append_evidence(
        EvidenceItem(source="document", content="The price is $10 per month.", score=0.9,
                     metadata={"page": 2, "document_id": "d1"})
    )
    rc.metadata["execution_status"] = "success"
    rc.metadata["direct_runtime"] = {"status": "success", "capability_id": "search_documents"}
    return rc


def planner_run_context():
    rc = RunContext.create(
        "Summarize the report and check the job status",
        user_id="u",
        working_context=[WorkingContextItem(source="thread_summary", content="prior context")],
    )
    rc.attach_behavior_profile(BehaviorProfile(path=BehaviorPath.PLANNER, reason="multi-step"))
    rc.append_tool_output(ToolOutput(capability_id="get_document_summary", output={"summary": "all good"}))
    rc.append_tool_output(ToolOutput(capability_id="get_job_status", output={"status": "completed"}))
    rc.append_evidence(EvidenceItem(source="document_summary", content="Report summary text.", score=0.7))
    rc.execution_state.record_result(
        StepExecutionResult(step_id="t1", capability_id="get_document_summary", status=StepStatus.SUCCEEDED)
    )
    rc.execution_state.record_result(
        StepExecutionResult(step_id="t2", capability_id="get_job_status", status=StepStatus.SUCCEEDED)
    )
    rc.metadata["planner_runtime"] = {
        "runtime_status": "completed",
        "completed_tasks": ["t1", "t2"],
        "failed_tasks": [],
        "partial_tasks": [],
        "execution_order": ["t1", "t2"],
    }
    rc.metadata["recovery_events"] = [{"task_id": "t1", "strategy": "retry"}]
    return rc


# --------------------------------------------------------------------------- #
# Both paths build a FinalPrompt
# --------------------------------------------------------------------------- #

def test_builds_from_direct_path():
    prompt = FinalContextBuilder().build(direct_run_context())
    assert isinstance(prompt, FinalPrompt)
    assert prompt.execution_summary.path == "direct"
    assert prompt.execution_summary.status == "success"
    assert prompt.execution_summary.selected_capabilities == ["search_documents"]


def test_builds_from_planner_path():
    prompt = FinalContextBuilder().build(planner_run_context())
    assert prompt.execution_summary.path == "planner"
    assert prompt.execution_summary.status == "completed"
    assert prompt.execution_summary.execution_order == ["t1", "t2"]
    assert prompt.execution_summary.recovery_event_count == 1
    assert prompt.execution_summary.details["planner_runtime"]["completed_tasks"] == ["t1", "t2"]


# --------------------------------------------------------------------------- #
# Content
# --------------------------------------------------------------------------- #

def test_includes_user_request():
    prompt = FinalContextBuilder().build(direct_run_context())
    assert prompt.user_request == "What does the resume say about pricing?"


def test_includes_working_context():
    prompt = FinalContextBuilder().build(direct_run_context())
    sources = {c.source for c in prompt.context_sections}
    assert {"thread_summary", "recent_message"} <= sources


def test_includes_tool_outputs_with_provenance():
    prompt = FinalContextBuilder().build(direct_run_context())
    assert len(prompt.tool_output_sections) == 1
    section = prompt.tool_output_sections[0]
    assert section.capability_id == "search_documents"
    assert section.output == {"hits": [{"text": "price is $10"}]}
    assert section.metadata == {"confidence": 0.9}


def test_includes_evidence_and_preserves_provenance():
    prompt = FinalContextBuilder().build(direct_run_context())
    assert len(prompt.evidence_sections) == 1
    ev = prompt.evidence_sections[0]
    assert ev.content == "The price is $10 per month."
    assert ev.score == 0.9
    assert ev.metadata == {"page": 2, "document_id": "d1"}  # provenance intact
    assert ev.id == "E1"


def test_citations_link_to_evidence():
    prompt = FinalContextBuilder().build(direct_run_context())
    assert [c.id for c in prompt.citations] == [e.id for e in prompt.evidence_sections]
    assert prompt.citations[0].source == "document"
    assert prompt.citations[0].metadata == {"page": 2, "document_id": "d1"}


def test_includes_execution_summary_counts():
    prompt = FinalContextBuilder().build(planner_run_context())
    assert prompt.execution_summary.tool_output_count == 2
    assert prompt.execution_summary.evidence_count == 1


def test_includes_final_instructions_and_system_prompt():
    prompt = FinalContextBuilder().build(direct_run_context())
    assert prompt.system_prompt
    assert prompt.final_instructions
    assert "[E1]" in prompt.final_instructions or "evidence" in prompt.final_instructions.lower()


def test_failure_status_changes_final_instructions():
    rc = direct_run_context()
    rc.metadata["direct_runtime"] = {"status": "needs_user", "capability_id": "search_documents"}
    rc.metadata["execution_status"] = "needs_user"
    prompt = FinalContextBuilder().build(rc)
    assert "ask the user" in prompt.final_instructions.lower()
    assert prompt.execution_summary.failed_tasks == ["search_documents"]


def test_final_instructions_override():
    prompt = FinalContextBuilder(final_instructions="CUSTOM").build(direct_run_context())
    assert prompt.final_instructions == "CUSTOM"


# --------------------------------------------------------------------------- #
# Prioritization + budget
# --------------------------------------------------------------------------- #

def test_evidence_prioritized_above_context_under_tight_budget():
    rc = direct_run_context()
    # A tiny budget: evidence claims it first, so context gets nothing.
    prompt = FinalContextBuilder(budget=8).build(rc)
    assert len(prompt.evidence_sections) >= 1
    assert prompt.context_sections == []


def test_respects_budget():
    rc = direct_run_context()
    budget = 6
    prompt = FinalContextBuilder(budget=budget).build(rc)
    assert prompt.metadata["tokens_used"] <= budget
    assert prompt.metadata["evidence_tokens"] + prompt.metadata["context_tokens"] == prompt.metadata["tokens_used"]


def test_evidence_ordered_by_score():
    rc = RunContext.create("q", user_id="u")
    rc.attach_behavior_profile(BehaviorProfile(path=BehaviorPath.DIRECT))
    rc.append_evidence(EvidenceItem(source="a", content="low", score=0.2))
    rc.append_evidence(EvidenceItem(source="b", content="high", score=0.95))
    prompt = FinalContextBuilder().build(rc)
    assert [e.content for e in prompt.evidence_sections] == ["high", "low"]
    assert [e.id for e in prompt.evidence_sections] == ["E1", "E2"]


# --------------------------------------------------------------------------- #
# Immutability + hygiene
# --------------------------------------------------------------------------- #

def test_does_not_mutate_run_context():
    rc = direct_run_context()
    wc_before = [w.content for w in rc.working_context]
    ev_before = list(rc.evidence)
    to_before = list(rc.tool_outputs)
    meta_keys_before = set(rc.metadata.keys())

    FinalContextBuilder().build(rc)

    assert [w.content for w in rc.working_context] == wc_before
    assert list(rc.evidence) == ev_before
    assert list(rc.tool_outputs) == to_before
    assert set(rc.metadata.keys()) == meta_keys_before  # no priority_report injected, etc.


def _module_level_import_targets(module):
    tree = ast.parse(inspect.getsource(module))
    targets = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            targets += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            targets.append(node.module or "")
    return targets


def test_no_config_db_or_llm_imports():
    targets = _module_level_import_targets(builder_module)
    for banned in ("app.config", "app.services", "app.db", "motor", "redis", "qdrant", "llm"):
        assert not any(banned in t for t in targets), (banned, targets)
    src = inspect.getsource(builder_module).lower()
    assert "llm_provider" not in src
    assert "llm_client" not in src
