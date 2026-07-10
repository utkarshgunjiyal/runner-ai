"""Answer Evaluation & Repair Engine (Phase 20).

Given a FinalPrompt (the curated context) and a draft FinalAnswer, run a set of
*deterministic* checks and produce an EvaluationReport with a PASS/FAIL verdict
and an advisory RepairDecision. No LLM judge in this phase, and no actual repair
execution — the orchestrator does not act on the decision yet.

Deterministic-first (ARCHITECTURE.md §20): cheap, explainable checks cover the
common failure modes (empty/too-short, ungrounded, missing citations, undisclosed
partial/failure, unaddressed multi-part requests). Config-free: no LLM, no
database, no application settings.
"""

import re

from app.agent.evaluation.models import (
    CheckResult,
    CheckSeverity,
    EvaluationReport,
    RepairAction,
    RepairDecision,
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_CITATION_RE = re.compile(r"\[([A-Za-z]+\d+)\]")

_STOPWORDS = {
    "the", "and", "then", "also", "to", "of", "for", "with", "this", "that",
    "your", "you", "our", "its", "it", "in", "on", "at", "as", "an", "a", "is",
    "are", "be", "please", "well", "about", "from", "into", "me", "my", "we",
}
# Splitters that mark separate asks in a compound request.
_CLAUSE_SPLIT_RE = re.compile(r"\band then\b|\bthen\b|\band also\b|\band\b|[;]", re.IGNORECASE)

# Words that signal the answer discloses a partial/failed outcome.
_DISCLOSURE_TERMS = {
    "partial", "partially", "incomplete", "could", "couldn", "cannot", "can't",
    "unable", "failed", "failure", "not able", "missing", "unavailable", "error",
    "wasn", "was not", "did not", "didn", "no result", "unfinished",
}

_PARTIAL_OR_FAIL_STATUSES = {
    "partial",
    "needs_user",
    "stopped_required_failure",
    "stopped_policy_block",
    "stopped_awaiting_approval",
}

_STAGE_FOR_ACTION = {
    RepairAction.REGENERATE_WITH_SAME_CONTEXT: "final_provider",
    RepairAction.REGENERATE_WITH_STRONGER_INSTRUCTIONS: "final_provider",
    RepairAction.RETRIEVE_MORE_CONTEXT: "context_engine",
    RepairAction.RERUN_CAPABILITY: "executor",
    RepairAction.REPLAN: "planner",
    RepairAction.ASK_USER_FOR_CLARIFICATION: "orchestrator",
    RepairAction.HUMAN_REVIEW: "orchestrator",
    RepairAction.RETURN_PARTIAL_WITH_WARNING: "orchestrator",
    RepairAction.FAIL_GRACEFULLY: "orchestrator",
}


def _salient(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall((text or "").lower()) if len(t) >= 3 and t not in _STOPWORDS}


class AnswerEvaluationEngine:
    def __init__(self, *, min_chars: int = 24, min_words: int = 3) -> None:
        self._min_chars = min_chars
        self._min_words = min_words

    # -- Public API ----------------------------------------------------------

    def evaluate(self, final_prompt, final_answer, run_context=None) -> EvaluationReport:
        text = (final_answer.text or "").strip()
        answer_tokens = _salient(text)
        checks: list[CheckResult] = []

        has_evidence = bool(final_prompt.evidence_sections) or bool(final_prompt.citations)
        has_tool_outputs = bool(final_prompt.tool_output_sections)

        # 1. Non-empty.
        non_empty = bool(text)
        checks.append(CheckResult(name="non_empty", passed=non_empty,
                                  detail="answer is empty" if not non_empty else ""))

        # 2. Minimum length.
        long_enough = non_empty and len(text) >= self._min_chars and len(text.split()) >= self._min_words
        checks.append(CheckResult(name="min_length", passed=long_enough,
                                  detail=f"answer shorter than {self._min_chars} chars" if not long_enough else ""))

        # 3. Citation validity + usage (only when evidence exists).
        valid_ids = {c.id for c in final_prompt.citations} | {e.id for e in final_prompt.evidence_sections}
        referenced = set(_CITATION_RE.findall(text))
        invalid_refs = sorted(referenced - valid_ids)
        used = set(final_answer.used_citations or [])
        unsupported_claims = sorted(invalid_refs + [u for u in used if u not in valid_ids])

        citation_score = 1.0
        groundedness_score = 1.0
        if has_evidence:
            cited = bool((referenced & valid_ids) or (used & valid_ids))
            checks.append(CheckResult(
                name="citations_used", passed=cited,
                detail="" if cited else "evidence present but no citation referenced",
            ))
            citation_score = (1.0 if cited else 0.0)
            groundedness_score = (1.0 if cited else 0.0)
        if referenced or used:
            valid_ok = not unsupported_claims
            checks.append(CheckResult(
                name="valid_citations", passed=valid_ok,
                detail="" if valid_ok else f"cites unknown evidence: {unsupported_claims}",
            ))
            if not valid_ok:
                citation_score = min(citation_score, 0.5)
                groundedness_score = min(groundedness_score, 0.4)

        # 4. Tool outputs reflected (only when they carry requirable text).
        tool_tokens = self._tool_tokens(final_prompt)
        if has_tool_outputs and tool_tokens:
            reflected = bool(tool_tokens & answer_tokens)
            checks.append(CheckResult(
                name="tool_outputs_reflected", passed=reflected,
                detail="" if reflected else "tool outputs not reflected in the answer",
            ))

        # 5. Disclosure of partial/failed execution.
        if self._is_partial_or_failed(final_prompt):
            disclosed = self._discloses(text)
            checks.append(CheckResult(
                name="discloses_partial_or_failure", passed=disclosed,
                detail="" if disclosed else "partial/failed execution not disclosed",
            ))

        # 6. Multi-part completeness.
        requirements = self._requirements(final_prompt.user_request)
        missing_requirements: list[str] = []
        completeness_score = 1.0
        if len(requirements) >= 2:
            for clause, clause_tokens in requirements:
                if not (clause_tokens & answer_tokens):
                    missing_requirements.append(clause)
            addressed = len(requirements) - len(missing_requirements)
            completeness_score = round(addressed / len(requirements), 4)
            checks.append(CheckResult(
                name="addresses_all_requirements", passed=not missing_requirements,
                detail="" if not missing_requirements else f"unaddressed: {missing_requirements}",
            ))

        passed = all(c.passed for c in checks if c.severity == CheckSeverity.ERROR)
        overall_score = 0.0 if not non_empty else round(
            0.4 * groundedness_score + 0.4 * completeness_score + 0.2 * citation_score, 4
        )
        reason = self._reason(passed, checks)
        repair = self._decide_repair(
            checks, has_evidence=has_evidence,
            unsupported_claims=unsupported_claims, missing_requirements=missing_requirements,
        )

        return EvaluationReport(
            passed=passed,
            overall_score=overall_score,
            reason=reason,
            missing_requirements=missing_requirements,
            unsupported_claims=unsupported_claims,
            groundedness_score=groundedness_score,
            completeness_score=completeness_score,
            citation_score=citation_score,
            checks=checks,
            repair_decision=repair,
            metadata={
                "has_evidence": has_evidence,
                "has_tool_outputs": has_tool_outputs,
                "requirement_count": len(requirements),
            },
        )

    # -- Repair mapping ------------------------------------------------------

    def _decide_repair(self, checks, *, has_evidence, unsupported_claims, missing_requirements) -> RepairDecision:
        failed = {c.name for c in checks if c.severity == CheckSeverity.ERROR and not c.passed}
        if not failed:
            return RepairDecision(action=RepairAction.NONE, reason="all checks passed", max_attempts=0)

        # Deterministic priority: cheapest correct repair first.
        if "non_empty" in failed:
            return self._repair(RepairAction.REGENERATE_WITH_SAME_CONTEXT,
                                "draft answer was empty", max_attempts=2)
        if "min_length" in failed:
            return self._repair(RepairAction.REGENERATE_WITH_STRONGER_INSTRUCTIONS,
                                "draft answer too short", max_attempts=2)
        if "valid_citations" in failed and not has_evidence:
            return self._repair(RepairAction.RETRIEVE_MORE_CONTEXT,
                                "answer makes unsupported claims with no evidence", max_attempts=1)
        if "citations_used" in failed or "valid_citations" in failed:
            return self._repair(RepairAction.REGENERATE_WITH_STRONGER_INSTRUCTIONS,
                                "answer must cite the available evidence", max_attempts=2)
        if "discloses_partial_or_failure" in failed:
            return self._repair(RepairAction.REGENERATE_WITH_STRONGER_INSTRUCTIONS,
                                "answer must disclose partial/failed execution", max_attempts=2)
        if "tool_outputs_reflected" in failed:
            return self._repair(RepairAction.REGENERATE_WITH_STRONGER_INSTRUCTIONS,
                                "answer must reflect the tool outputs", max_attempts=2)
        if "addresses_all_requirements" in failed:
            return self._repair(RepairAction.REGENERATE_WITH_STRONGER_INSTRUCTIONS,
                                f"answer must address all requested parts: {missing_requirements}",
                                max_attempts=2)
        return self._repair(RepairAction.HUMAN_REVIEW, "unclassified evaluation failure", max_attempts=1)

    @staticmethod
    def _repair(action: RepairAction, reason: str, *, max_attempts: int) -> RepairDecision:
        return RepairDecision(
            action=action, reason=reason, max_attempts=max_attempts,
            target_stage=_STAGE_FOR_ACTION.get(action),
        )

    # -- Helpers -------------------------------------------------------------

    @staticmethod
    def _tool_tokens(final_prompt) -> set[str]:
        tokens: set[str] = set()
        for section in final_prompt.tool_output_sections:
            tokens |= _salient(_stringify(section.output))
        return tokens

    @staticmethod
    def _is_partial_or_failed(final_prompt) -> bool:
        summary = final_prompt.execution_summary
        return (
            summary.status in _PARTIAL_OR_FAIL_STATUSES
            or bool(summary.failed_tasks)
            or bool(summary.partial_tasks)
        )

    @staticmethod
    def _discloses(text: str) -> bool:
        low = text.lower()
        return any(term in low for term in _DISCLOSURE_TERMS)

    @staticmethod
    def _requirements(user_request: str):
        clauses = []
        for raw in _CLAUSE_SPLIT_RE.split(user_request or ""):
            clause = raw.strip(" ,.-")
            tokens = _salient(clause)
            if len(tokens) >= 2:
                clauses.append((clause, tokens))
        return clauses

    @staticmethod
    def _reason(passed: bool, checks) -> str:
        if passed:
            return "answer passed all deterministic checks"
        failed = [c.name for c in checks if c.severity == CheckSeverity.ERROR and not c.passed]
        return "failed checks: " + ", ".join(failed)


def _stringify(value) -> str:
    if isinstance(value, dict):
        return " ".join(_stringify(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(_stringify(v) for v in value)
    return str(value)


def attach_evaluation_report(run_context, report: EvaluationReport):
    """Record the evaluation on ``RunContext.metadata['answer_evaluation']``.

    Append-only metadata write; the working context is never touched.
    """

    run_context.metadata["answer_evaluation"] = report.model_dump()
    return run_context
