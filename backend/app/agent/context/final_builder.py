"""Final Context Builder (Phase 16).

Both the DIRECT and PLANNER paths converge here. After execution has left tool
outputs, evidence, execution state, and metadata on the RunContext, this stage
assembles a structured, provider-agnostic ``FinalPrompt`` for the *future* LLM
call — it does NOT call an LLM.

Design:
- Reuses the deterministic ``ContextPrioritizer`` (ranking) and ``BudgetManager``
  (selection/truncation) rather than re-implementing them.
- Evidence gets first claim on the token budget, so it is prioritized above
  older working context (a requirement of the final view).
- Provenance is preserved end-to-end: evidence carries citation ids + source +
  score, tool outputs carry their capability/step, and each retained context
  item keeps its source and priority score.
- Never mutates the RunContext — it reads copies and rewraps items via
  ``model_copy``. No LLM, no database, no application settings.

See ARCHITECTURE.md §21.
"""

from app.agent.context.budget import BudgetManager
from app.agent.context.prioritizer import (
    ContextPrioritizer,
    ContextScore,
    PriorityReport,
    RankedContextItem,
)
from app.agent.models.final_prompt import (
    Citation,
    ContextSection,
    EvidenceSection,
    ExecutionSummary,
    FinalPrompt,
    ToolOutputSection,
)
from app.agent.runtime.context import RunContext, WorkingContextItem

_SCORE_KEY = "_final_score"
_TRUNCATED_KEY = "truncated"

_STOP_STATUSES = {
    "needs_user",
    "stopped_required_failure",
    "stopped_policy_block",
    "stopped_awaiting_approval",
}
_PARTIAL_STATUSES = {"partial"}


class FinalContextBuilder:
    DEFAULT_SYSTEM_PROMPT = (
        "You are Runner.ai's grounded assistant. Answer using only the working "
        "context, tool outputs, and evidence provided below. Cite supporting "
        "evidence by its id (for example [E1]). Do not rely on outside knowledge; "
        "if the information is insufficient, say so plainly."
    )
    _BASE_INSTRUCTIONS = (
        "Using only the working context, tool outputs, and evidence above, answer "
        "the user's request. Cite supporting evidence by its id (for example [E1])."
    )

    def __init__(
        self,
        *,
        system_prompt: str | None = None,
        final_instructions: str | None = None,
        budget: int = 4000,
        chars_per_token: int = 4,
        prioritizer: ContextPrioritizer | None = None,
        budget_manager: BudgetManager | None = None,
        hybrid_pipeline=None,
    ) -> None:
        self._system_prompt = system_prompt or self.DEFAULT_SYSTEM_PROMPT
        self._final_instructions_override = final_instructions
        self._budget = max(0, budget)
        self._prioritizer = prioritizer or ContextPrioritizer()
        self._budget_manager = budget_manager or BudgetManager(chars_per_token=chars_per_token)
        # Optional Phase 28 hybrid pipeline. When absent, behavior is unchanged;
        # when present (even with Null stages), it reorders the prioritized
        # working context before budgeting — deterministic Stage 1 is preserved.
        self._hybrid_pipeline = hybrid_pipeline

    # -- Public API ----------------------------------------------------------

    def build(self, run_context: RunContext) -> FinalPrompt:
        # Evidence is budgeted first (prioritized above older working context);
        # working context consumes whatever budget remains.
        evidence_sections, citations, evidence_tokens = self._build_evidence(run_context)
        context_sections, context_tokens = self._build_context(
            run_context, budget=self._budget - evidence_tokens
        )
        tool_output_sections = self._build_tool_outputs(run_context)
        summary = self._build_summary(run_context, evidence_sections, tool_output_sections)
        instructions = self._final_instructions(summary)

        # Comparison metadata (Phase 44.1) — carried on the FinalPrompt so the
        # answer layer (including the deterministic fallback) synthesizes a
        # source-separated comparison instead of a blended dump. Read from the
        # already-computed interpretation/scope; never re-inferred from raw text.
        interpretation = run_context.metadata.get("interpretation") or {}
        scope = run_context.metadata.get("document_scope")
        comparison_documents = list(scope.get("documents", [])) if isinstance(scope, dict) else []
        evidence_filenames = [
            fn for fn in dict.fromkeys(
                (s.metadata or {}).get("filename") for s in evidence_sections
            ) if fn
        ]
        is_comparison = (
            "document_comparison" in (interpretation.get("intents") or [])
            or len(comparison_documents) >= 2
            or len(evidence_filenames) >= 2
        )
        instructions = self._augment_for_documents(instructions, is_comparison)

        return FinalPrompt(
            system_prompt=self._system_prompt,
            user_request=run_context.user_request,
            context_sections=context_sections,
            evidence_sections=evidence_sections,
            tool_output_sections=tool_output_sections,
            execution_summary=summary,
            final_instructions=instructions,
            citations=citations,
            metadata={
                "run_id": run_context.run_id,
                "budget": self._budget,
                "evidence_tokens": evidence_tokens,
                "context_tokens": context_tokens,
                "tokens_used": evidence_tokens + context_tokens,
                "intents": list(interpretation.get("intents") or []),
                "is_comparison": is_comparison,
                "comparison_documents": comparison_documents,
            },
        )

    # -- Evidence ------------------------------------------------------------

    def _build_evidence(self, run_context: RunContext):
        evidence = list(run_context.evidence)
        if not evidence:
            return [], [], 0

        # Highest score first; unscored last; stable within ties.
        ordered = sorted(
            enumerate(evidence),
            key=lambda pair: (-(pair[1].score if pair[1].score is not None else -1.0), pair[0]),
        )
        n = len(ordered)
        ranked = [
            RankedContextItem(
                item=WorkingContextItem(
                    source=item.source,
                    content=item.content,
                    metadata={**item.metadata, _SCORE_KEY: item.score},
                ),
                score=ContextScore(final_score=float(n - position)),
                rank=position + 1,
            )
            for position, (_orig, item) in enumerate(ordered)
        ]
        budgeted = self._budget_manager.select(
            PriorityReport(ranked=ranked), budget=max(0, self._budget)
        )

        sections: list[EvidenceSection] = []
        citations: list[Citation] = []
        for i, kept in enumerate(budgeted.kept_items, start=1):
            cid = f"E{i}"
            score = kept.metadata.get(_SCORE_KEY)
            truncated = kept.metadata.get(_TRUNCATED_KEY) is True
            provenance = self._provenance(kept.metadata)
            sections.append(
                EvidenceSection(
                    id=cid,
                    source=kept.source,
                    content=kept.content,
                    score=score,
                    truncated=truncated,
                    metadata=provenance,
                )
            )
            citations.append(
                Citation(id=cid, source=kept.source, score=score, metadata=provenance)
            )
        return sections, citations, budgeted.used_tokens

    # -- Working context -----------------------------------------------------

    def _build_context(self, run_context: RunContext, budget: int):
        budget = max(0, budget)
        report = self._prioritizer.prioritize(
            run_context.working_context, run_context.user_request
        )
        if report.is_empty or budget == 0:
            return [], 0

        if self._hybrid_pipeline is not None:
            report = self._reorder_report(report, run_context.user_request)

        # Rewrap so each item's priority score survives budgeting for provenance.
        wrapped = [
            RankedContextItem(
                item=r.item.model_copy(
                    update={"metadata": {**r.item.metadata, _SCORE_KEY: r.score.final_score}}
                ),
                score=r.score,
                rank=r.rank,
            )
            for r in report.ranked
        ]
        budgeted = self._budget_manager.select(PriorityReport(ranked=wrapped), budget=budget)

        sections = [
            ContextSection(
                source=kept.source,
                content=kept.content,
                score=kept.metadata.get(_SCORE_KEY),
                truncated=kept.metadata.get(_TRUNCATED_KEY) is True,
                metadata=self._provenance(kept.metadata),
            )
            for kept in budgeted.kept_items
        ]
        return sections, budgeted.used_tokens

    def _reorder_report(self, report, user_request: str):
        """Reorder a PriorityReport through the hybrid pipeline (Stage 1 scores
        feed the deterministic tier, so Null stages leave the order unchanged)."""
        from app.agent.retriever.hybrid_pipeline import Candidate

        candidates = [
            Candidate(
                id=f"ctx-{i}", text=r.item.content, payload=r,
                deterministic_score=r.score.final_score,
            )
            for i, r in enumerate(report.ranked)
        ]
        result = self._hybrid_pipeline.retrieve(
            user_request, candidates, top_k=len(candidates)
        )
        return PriorityReport(ranked=[sc.candidate.payload for sc in result.ranked])

    # -- Tool outputs --------------------------------------------------------

    def _build_tool_outputs(self, run_context: RunContext) -> list[ToolOutputSection]:
        return [
            ToolOutputSection(
                id=f"T{i}",
                capability_id=output.capability_id,
                step_id=output.step_id,
                output=dict(output.output),
                metadata=dict(output.metadata),
            )
            for i, output in enumerate(run_context.tool_outputs, start=1)
        ]

    # -- Execution summary ---------------------------------------------------

    def _build_summary(
        self, run_context: RunContext, evidence_sections, tool_output_sections
    ) -> ExecutionSummary:
        metadata = run_context.metadata
        path = (
            run_context.behavior_profile.path.value
            if run_context.behavior_profile is not None
            else None
        )
        recovery = metadata.get("recovery_events") or []
        planner = metadata.get("planner_runtime")
        direct = metadata.get("direct_runtime")

        if planner:
            status = planner.get("runtime_status")
            completed = list(planner.get("completed_tasks", []))
            failed = list(planner.get("failed_tasks", []))
            partial = list(planner.get("partial_tasks", []))
            order = list(planner.get("execution_order", []))
            details = {"planner_runtime": planner}
        elif direct:
            status = direct.get("status") or metadata.get("execution_status")
            cap = direct.get("capability_id")
            ids = [cap] if cap else []
            succeeded = status in ("success", "partial")
            completed = ids if succeeded else []
            failed = [] if succeeded else ids
            partial = ids if status == "partial" else []
            order = ids
            details = {"direct_runtime": direct}
        else:
            status = metadata.get("execution_status")
            completed = list(run_context.execution_state.completed_steps)
            failed = list(run_context.execution_state.failed_steps)
            partial = []
            order = list(completed)
            details = {}

        return ExecutionSummary(
            path=path,
            status=status,
            selected_capabilities=list(run_context.selected_capabilities),
            completed_tasks=completed,
            failed_tasks=failed,
            partial_tasks=partial,
            execution_order=order,
            tool_output_count=len(tool_output_sections),
            evidence_count=len(evidence_sections),
            recovery_event_count=len(recovery),
            details=details,
        )

    # -- Final instructions --------------------------------------------------

    @staticmethod
    def _augment_for_documents(instructions: str, is_comparison: bool) -> str:
        """Phase 44.1: when the request compares multiple documents, require a
        source-separated, comparison-structured answer with source-aware citations
        (never merge identities/facts across documents, never cite bare [E#]).

        ``is_comparison`` is decided by the builder from the already-computed
        interpretation/scope (intents, resolved documents, evidence filenames) —
        it is never re-inferred from raw text here."""
        if not is_comparison:
            return instructions
        return (
            instructions
            + "\n\nThis request involves multiple documents. Structure the answer with a"
            " separate labelled section per document (use the document filename as the"
            " heading), then a 'Similarities' section and a 'Differences' section, and"
            " note any gaps where relevant. Do NOT merge facts or identities across"
            " documents. Cite evidence by its source filename and page (for example"
            " 'my_resume.pdf p.1'), not by a bare evidence id."
        )

    def _final_instructions(self, summary: ExecutionSummary) -> str:
        if self._final_instructions_override is not None:
            return self._final_instructions_override
        if summary.status in _STOP_STATUSES or summary.failed_tasks:
            return (
                self._BASE_INSTRUCTIONS
                + " If required information or an action could not be completed, "
                "clearly state what is missing and ask the user how to proceed."
            )
        if summary.partial_tasks or summary.status in _PARTIAL_STATUSES:
            return (
                self._BASE_INSTRUCTIONS
                + " Some results are partial; note any gaps or missing pieces in "
                "your answer."
            )
        return self._BASE_INSTRUCTIONS

    # -- Helpers -------------------------------------------------------------

    @staticmethod
    def _provenance(metadata: dict) -> dict:
        return {k: v for k, v in metadata.items() if k not in (_SCORE_KEY, _TRUNCATED_KEY)}
