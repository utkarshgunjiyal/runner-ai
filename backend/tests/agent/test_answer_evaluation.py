"""Phase 20 tests — Answer Evaluation & Repair Engine.

Config-free: FinalPrompts/FinalAnswers are hand-built; the engine is fully
deterministic. No Mongo/Qdrant/Redis, no application settings, no LLM.
"""

import ast
import inspect

from app.agent.evaluation import engine as engine_module
from app.agent.evaluation.engine import AnswerEvaluationEngine, attach_evaluation_report
from app.agent.evaluation.models import EvaluationReport, RepairAction
from app.agent.llm.final_provider import FinalAnswer
from app.agent.models.final_prompt import (
    Citation,
    ContextSection,
    EvidenceSection,
    ExecutionSummary,
    FinalPrompt,
    ToolOutputSection,
)
from app.agent.runtime.context import BehaviorPath, BehaviorProfile, RunContext, WorkingContextItem


def prompt(
    *,
    user_request="What does the document say about pricing?",
    evidence=None,
    citations=None,
    tool_outputs=None,
    context=None,
    status="success",
    failed_tasks=None,
    partial_tasks=None,
):
    return FinalPrompt(
        system_prompt="system",
        user_request=user_request,
        context_sections=context or [],
        evidence_sections=evidence or [],
        tool_output_sections=tool_outputs or [],
        execution_summary=ExecutionSummary(
            path="direct", status=status,
            failed_tasks=failed_tasks or [], partial_tasks=partial_tasks or [],
        ),
        final_instructions="answer using evidence",
        citations=citations or [],
    )


def answer(text, used_citations=None):
    return FinalAnswer(text=text, used_citations=used_citations or [], provider="deterministic", model="fake")


EV = [EvidenceSection(id="E1", source="document", content="The price is $10 per month.", score=0.9)]
CITE = [Citation(id="E1", source="document", score=0.9)]
TOOL = [ToolOutputSection(id="T1", capability_id="search_documents", output={"summary": "pricing tier alpha"})]


def evaluate(p, a):
    return AnswerEvaluationEngine().evaluate(p, a)


# --------------------------------------------------------------------------- #
# Pass / basic failures
# --------------------------------------------------------------------------- #

def test_good_grounded_answer_passes():
    p = prompt(evidence=EV, citations=CITE)
    a = answer("The pricing is $10 per month, as shown in the document [E1].", used_citations=["E1"])
    report = evaluate(p, a)
    assert report.passed is True
    assert report.overall_score > 0.8
    assert report.repair_decision.action == RepairAction.NONE


def test_empty_answer_fails():
    report = evaluate(prompt(), answer("   "))
    assert report.passed is False
    assert report.overall_score == 0.0
    assert any(c.name == "non_empty" and not c.passed for c in report.checks)


def test_too_short_answer_fails():
    report = evaluate(prompt(), answer("Yes."))
    assert report.passed is False
    assert any(c.name == "min_length" and not c.passed for c in report.checks)


# --------------------------------------------------------------------------- #
# Citations / groundedness
# --------------------------------------------------------------------------- #

def test_missing_citations_fails_when_evidence_exists():
    p = prompt(evidence=EV, citations=CITE)
    a = answer("The pricing is roughly ten dollars per month for the basic plan.")
    report = evaluate(p, a)
    assert report.passed is False
    assert report.citation_score == 0.0
    assert any(c.name == "citations_used" and not c.passed for c in report.checks)


def test_no_citation_requirement_when_no_evidence():
    p = prompt(user_request="Please introduce yourself briefly")
    a = answer("Hello! I am Runner.ai, here to help you with your documents and tasks.")
    report = evaluate(p, a)
    # No evidence → no citation check emitted, and it should pass.
    assert not any(c.name == "citations_used" for c in report.checks)
    assert report.passed is True


def test_unsupported_citation_with_no_evidence_triggers_retrieve_more():
    p = prompt(user_request="Explain the refund policy in detail for me please")
    a = answer("The refund policy allows returns within 30 days, per the contract [E7].")
    report = evaluate(p, a)
    assert report.passed is False
    assert "E7" in report.unsupported_claims
    assert report.repair_decision.action == RepairAction.RETRIEVE_MORE_CONTEXT
    assert report.repair_decision.target_stage == "context_engine"


# --------------------------------------------------------------------------- #
# Completeness / disclosure / tool reflection
# --------------------------------------------------------------------------- #

def test_missing_requested_section_fails():
    p = prompt(user_request="Summarize the report and email the results to the team")
    a = answer("Here is a concise summary of the report covering the main findings and totals.")
    report = evaluate(p, a)
    assert report.passed is False
    assert any("email" in req for req in report.missing_requirements)
    assert report.completeness_score < 1.0


def test_partial_execution_not_disclosed_fails():
    p = prompt(status="partial", partial_tasks=["t1"])
    a = answer("The document indicates the price is around ten dollars per month for the plan.")
    report = evaluate(p, a)
    assert report.passed is False
    assert any(c.name == "discloses_partial_or_failure" and not c.passed for c in report.checks)


def test_partial_execution_disclosed_passes():
    p = prompt(status="partial", partial_tasks=["t1"])
    a = answer("I could only partially complete this; the pricing lookup is incomplete and missing data.")
    report = evaluate(p, a)
    assert any(c.name == "discloses_partial_or_failure" and c.passed for c in report.checks)


def test_tool_outputs_not_reflected_fails():
    p = prompt(tool_outputs=TOOL)
    a = answer("I looked into your request and here is a general response without specifics.")
    report = evaluate(p, a)
    assert report.passed is False
    assert any(c.name == "tool_outputs_reflected" and not c.passed for c in report.checks)


def test_tool_outputs_reflected_passes():
    p = prompt(tool_outputs=TOOL)
    a = answer("According to the search, the pricing tier alpha is the relevant summary for you.")
    report = evaluate(p, a)
    assert any(c.name == "tool_outputs_reflected" and c.passed for c in report.checks)


# --------------------------------------------------------------------------- #
# Repair decision mapping
# --------------------------------------------------------------------------- #

def test_repair_decision_empty_is_regenerate_same_context():
    report = evaluate(prompt(), answer(""))
    assert report.repair_decision.action == RepairAction.REGENERATE_WITH_SAME_CONTEXT
    assert report.repair_decision.target_stage == "final_provider"
    assert report.repair_decision.max_attempts >= 1


def test_repair_decision_missing_citation_is_stronger_instructions():
    p = prompt(evidence=EV, citations=CITE)
    a = answer("The pricing is about ten dollars a month for the basic plan tier offered.")
    report = evaluate(p, a)
    assert report.repair_decision.action == RepairAction.REGENERATE_WITH_STRONGER_INSTRUCTIONS
    assert report.repair_decision.target_stage == "final_provider"


# --------------------------------------------------------------------------- #
# RunContext integration
# --------------------------------------------------------------------------- #

def test_attach_evaluation_report_stores_metadata():
    rc = RunContext.create("q", user_id="u")
    report = evaluate(prompt(evidence=EV, citations=CITE),
                      answer("Pricing is $10/month per the document [E1].", used_citations=["E1"]))
    attach_evaluation_report(rc, report)
    stored = rc.metadata["answer_evaluation"]
    assert stored["passed"] is True
    assert stored["repair_decision"]["action"] == RepairAction.NONE.value


def test_attach_does_not_mutate_working_context():
    rc = RunContext.create(
        "q", user_id="u",
        working_context=[WorkingContextItem(source="thread_summary", content="prior")],
    )
    rc.attach_behavior_profile(BehaviorProfile(path=BehaviorPath.DIRECT))
    before = [w.content for w in rc.working_context]
    attach_evaluation_report(rc, evaluate(prompt(), answer("A sufficiently long grounded response here.")))
    assert [w.content for w in rc.working_context] == before
    assert len(rc.working_context) == 1


def test_report_is_evaluation_report_type():
    assert isinstance(evaluate(prompt(), answer("A long enough answer for the check.")), EvaluationReport)


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
    for module in (engine_module, __import__("app.agent.evaluation.models", fromlist=["x"])):
        targets = _module_level_import_targets(module)
        for banned in (
            "app.config", "app.services", "app.db", "motor", "redis", "qdrant",
            "openai", "anthropic", "google.generativeai", "genai", "llm",
        ):
            assert not any(banned in t for t in targets), (module.__name__, banned, targets)
