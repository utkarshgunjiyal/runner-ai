"""Planner providers (Phase 36).

Provider-agnostic contract that turns a PlannerPrompt into a typed
``ExecutionPlan`` for PlannerRuntime. The planner NEVER executes tools, touches
the registry, or sees the raw RunContext — it only produces a plan, which the
existing Validator/Policy/Optimizer/PlannerRuntime/Execution Bridge act on.

Provides a DeterministicPlannerProvider (tests/offline) and a V15PlannerProvider
that reuses the V1.5 LLM service (lazy — no vendor SDK, no credentials needed to
import). Structured output is strictly validated: malformed JSON, missing fields,
and unknown capability ids are rejected rather than silently coerced.
"""

import json
from typing import Protocol

from pydantic import ValidationError

from app.agent.llm.provider_adapter import (
    ProviderError,
    ProviderUnavailableError,
    resolve_v15_complete,
)
from app.agent.models.plan import FinalResponseMode
from app.agent.models.planner_prompt import PlannerPrompt
from app.agent.runtime.planner_runtime import ExecutionPlan, PlannerTask


class PlannerProviderError(ProviderError):
    """Base for planner provider failures."""

    error_code = "planner_error"
    stage = "planner_provider"
    safe_message = "The plan could not be generated."


class PlannerOutputParseError(PlannerProviderError):
    """The planner output was not valid JSON."""

    error_code = "planner_output_parse_error"
    safe_message = "The generated plan was not valid."


class PlannerOutputValidationError(PlannerProviderError):
    """The planner output was valid JSON but not a valid plan."""

    error_code = "planner_output_validation_error"
    safe_message = "The generated plan was invalid; a clarification may help."
    clarification_needed = True


class PlannerProvider(Protocol):
    async def plan(self, planner_prompt: PlannerPrompt) -> ExecutionPlan:
        ...


_FINAL_RESPONSE_MODES = {mode.value for mode in FinalResponseMode}


def _strip_code_fences(text: str) -> str:
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else stripped[3:]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    return stripped.strip()


def parse_execution_plan(
    raw: str,
    allowed_capability_ids=None,
    *,
    default_goal: str = "",
) -> ExecutionPlan:
    """Strictly parse raw planner output into an ExecutionPlan.

    capability_id / depends_on / final_response_mode (which the ExecutionPlan /
    PlannerTask models don't field directly) are validated and preserved in task
    metadata. Anything malformed raises a PlannerProvider* error — never a
    silently coerced plan.
    """

    try:
        data = json.loads(_strip_code_fences(raw))
    except (json.JSONDecodeError, TypeError) as exc:
        raise PlannerOutputParseError(f"planner output is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise PlannerOutputValidationError("plan must be a JSON object")

    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise PlannerOutputValidationError("plan.tasks must be a non-empty list")

    final_response_mode = data.get("final_response_mode")
    if final_response_mode is not None and final_response_mode not in _FINAL_RESPONSE_MODES:
        raise PlannerOutputValidationError(f"unknown final_response_mode: {final_response_mode!r}")

    allowed = set(allowed_capability_ids or [])

    task_ids: list[str] = []
    for task in raw_tasks:
        if not isinstance(task, dict):
            raise PlannerOutputValidationError("each task must be a JSON object")
        tid = task.get("id")
        request = task.get("request")
        if not tid or not isinstance(tid, str):
            raise PlannerOutputValidationError("task.id is required and must be a string")
        if not request or not isinstance(request, str):
            raise PlannerOutputValidationError("task.request is required and must be a string")
        task_ids.append(tid)

    tasks: list[PlannerTask] = []
    seen: set[str] = set()
    for task in raw_tasks:
        tid = task["id"]
        if tid in seen:
            raise PlannerOutputValidationError(f"duplicate task id: {tid!r}")
        seen.add(tid)

        metadata = dict(task.get("metadata") or {})

        capability_id = task.get("capability_id")
        if capability_id is not None:
            if allowed and capability_id not in allowed:
                raise PlannerOutputValidationError(f"unknown capability id: {capability_id!r}")
            metadata["capability_id"] = capability_id

        depends_on = task.get("depends_on")
        if depends_on is not None:
            if not isinstance(depends_on, list):
                raise PlannerOutputValidationError("task.depends_on must be a list")
            for dep in depends_on:
                if dep not in task_ids:
                    raise PlannerOutputValidationError(f"depends_on references unknown task: {dep!r}")
            metadata["depends_on"] = list(depends_on)

        if final_response_mode is not None:
            metadata.setdefault("final_response_mode", final_response_mode)

        try:
            tasks.append(
                PlannerTask(
                    id=tid,
                    request=task["request"],
                    optional=bool(task.get("optional", False)),
                    metadata=metadata,
                )
            )
        except ValidationError as exc:
            raise PlannerOutputValidationError(str(exc)) from exc

    plan_id = str(data.get("id") or "plan")
    goal = str(data.get("goal") or default_goal or tasks[0].request)
    try:
        return ExecutionPlan(id=plan_id, goal=goal, tasks=tasks)
    except ValidationError as exc:
        raise PlannerOutputValidationError(str(exc)) from exc


class DeterministicPlannerProvider:
    """Deterministic planner for tests/offline. Produces a valid ExecutionPlan
    from the prompt's capabilities (one task each, referencing valid ids)."""

    def __init__(self, *, max_tasks: int = 3) -> None:
        self._max_tasks = max(1, max_tasks)

    async def plan(self, planner_prompt: PlannerPrompt) -> ExecutionPlan:
        capabilities = planner_prompt.capabilities[: self._max_tasks]
        if capabilities:
            tasks = [
                PlannerTask(
                    id=f"t{i + 1}",
                    request=f"Use {cap.name or cap.id} to address: {planner_prompt.user_request}",
                    optional=i > 0,
                    metadata={"capability_id": cap.id},
                )
                for i, cap in enumerate(capabilities)
            ]
        else:
            tasks = [PlannerTask(id="t1", request=planner_prompt.user_request)]
        return ExecutionPlan(id="plan-deterministic", goal=planner_prompt.user_request, tasks=tasks)


class V15PlannerProvider:
    """Real planner over the V1.5 LLM service. ``complete`` is injectable for
    tests; otherwise resolved lazily at invocation time."""

    def __init__(self, *, complete=None, max_tokens: int | None = None) -> None:
        self._complete = complete
        self._max_tokens = max_tokens

    async def plan(self, planner_prompt: PlannerPrompt) -> ExecutionPlan:
        complete = self._complete or await resolve_v15_complete()
        system, user = self._render(planner_prompt)
        try:
            if self._max_tokens is not None:
                raw = await complete(system, user, max_tokens=self._max_tokens)
            else:
                raw = await complete(system, user)
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - wrap raw LLM/vendor errors
            raise ProviderUnavailableError(f"planner generation failed: {exc}") from exc

        return parse_execution_plan(
            raw,
            planner_prompt.allowed_capability_ids(),
            default_goal=planner_prompt.user_request,
        )

    @staticmethod
    def _render(planner_prompt: PlannerPrompt) -> tuple[str, str]:
        capability_lines = "\n".join(
            f"- {c.id}: {c.name} — {c.description}" for c in planner_prompt.capabilities
        )
        system = (
            "You are Runner.ai's planner. Break the user's request into a small "
            "ordered list of tasks and return ONLY JSON matching this schema:\n"
            f"{json.dumps(planner_prompt.output_schema)}\n"
            "Rules: reference only these capability ids; do not execute anything; "
            "mark non-critical tasks optional; keep the plan minimal.\n"
            f"Available capabilities:\n{capability_lines or '(none)'}"
        )
        context_lines = "\n".join(
            f"- {item.get('source')}: {item.get('content')}"
            for item in planner_prompt.working_context
        )
        user = (
            f"User request: {planner_prompt.user_request}\n"
            f"Working context:\n{context_lines or '(none)'}\n"
            f"Constraints: {json.dumps(planner_prompt.planning_constraints)}"
        )
        return system, user
