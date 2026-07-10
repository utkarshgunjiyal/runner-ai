"""Deterministic plan optimizer.

Builds an execution strategy (DAG levels → execution groups) from a logical Plan
and annotates notes. Phase 6 does NOT rewrite the plan: no steps removed, no args
rewritten, no dependencies changed, no reordering except via execution_groups.
Never mutates the Plan or the ToolRegistry. See docs/architecture/v2.md §9.
"""

import json
from collections import defaultdict

from app.agent.models.optimization import (
    ExecutionGroup,
    OptimizationNote,
    OptimizationReport,
    OptimizationType,
    OptimizedPlan,
)
from app.agent.models.plan import Plan, PlanStepType
from app.agent.models.policy import PolicyReport
from app.agent.registry.registry import ToolRegistry


class PlanOptimizer:
    def __init__(self, registry: ToolRegistry | None = None) -> None:
        self._registry = registry

    def optimize(
        self,
        plan: Plan,
        policy_report: PolicyReport | None = None,
    ) -> tuple[OptimizedPlan, OptimizationReport]:
        groups = self._build_execution_groups(plan)

        notes: list[OptimizationNote] = []
        notes.extend(self._parallel_notes(groups))
        notes.extend(self._duplicate_notes(plan))
        notes.extend(self._policy_notes(policy_report))

        if not notes:
            notes.append(
                OptimizationNote(
                    type=OptimizationType.NO_OP,
                    message="no optimizations applied",
                    step_ids=[],
                )
            )

        optimized = OptimizedPlan(
            original_plan_id=plan.id,
            steps=list(plan.steps),  # reuse the immutable PlanStep objects
            execution_groups=groups,
        )
        return optimized, OptimizationReport(notes=notes)

    # -- Execution grouping (DAG levels) ------------------------------------

    def _build_execution_groups(self, plan: Plan) -> list[ExecutionGroup]:
        # Plan steps are topologically ordered (no dependency on a later step),
        # so each dependency's depth is known before the step is processed.
        depth: dict[str, int] = {}
        for step in plan.steps:
            depth[step.id] = (
                0
                if not step.depends_on
                else 1 + max(depth[dep] for dep in step.depends_on)
            )

        levels: dict[int, list[str]] = defaultdict(list)
        for step in plan.steps:
            levels[depth[step.id]].append(step.id)  # preserves plan order

        groups: list[ExecutionGroup] = []
        for i, level in enumerate(sorted(levels), start=1):
            step_ids = levels[level]
            groups.append(
                ExecutionGroup(
                    group_id=f"group_{i}",
                    step_ids=step_ids,
                    parallel=len(step_ids) > 1,
                )
            )
        return groups

    # -- Notes --------------------------------------------------------------

    @staticmethod
    def _parallel_notes(groups: list[ExecutionGroup]) -> list[OptimizationNote]:
        return [
            OptimizationNote(
                type=OptimizationType.PARALLEL_GROUPING,
                message=f"{group.group_id}: {len(group.step_ids)} steps can run in parallel",
                step_ids=list(group.step_ids),
            )
            for group in groups
            if group.parallel
        ]

    @staticmethod
    def _duplicate_notes(plan: Plan) -> list[OptimizationNote]:
        # Group TOOL steps by (capability_id, canonical args). Detection only —
        # no deduplication (safe rebinding of dependents is a later phase).
        buckets: dict[tuple, list[str]] = defaultdict(list)
        for step in plan.steps:
            if step.step_type != PlanStepType.TOOL or not step.capability_id:
                continue
            args_key = json.dumps(step.args, sort_keys=True, default=str)
            buckets[(step.capability_id, args_key)].append(step.id)

        notes = []
        for (capability_id, _), step_ids in buckets.items():
            if len(step_ids) > 1:
                notes.append(
                    OptimizationNote(
                        type=OptimizationType.DUPLICATE_STEP_DETECTED,
                        message=f"duplicate calls to '{capability_id}' with identical args",
                        step_ids=step_ids,
                    )
                )
        return notes

    @staticmethod
    def _policy_notes(policy_report: PolicyReport | None) -> list[OptimizationNote]:
        if policy_report is None:
            return []

        notes = []
        for decision in policy_report.blocked_steps:
            notes.append(
                OptimizationNote(
                    type=OptimizationType.BLOCKED_STEP_PRESERVED,
                    message=f"step '{decision.step_id}' is BLOCK; preserved for audit",
                    step_ids=[decision.step_id],
                )
            )
        for decision in policy_report.approval_steps:
            notes.append(
                OptimizationNote(
                    type=OptimizationType.APPROVAL_STEP_MARKED,
                    message=f"step '{decision.step_id}' requires approval",
                    step_ids=[decision.step_id],
                )
            )
        return notes
