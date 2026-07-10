"""RunContext — the central runtime object for a V2 agent run (Phase 10A).

Created before the behavior gate and carried through planning, execution, and
final response generation. It composes (does not replace) the Phase 7
``ExecutionState``. Accumulated state (tool outputs, evidence) is append-only,
and the original working context is never mutated by later stages.

Phase 10A is models + helper methods only: deterministic, no providers, no LLM,
no retrievers, no adapters. See backend/app/agent/ARCHITECTURE.md §9.
"""

import uuid
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.agent.execution.state import ExecutionState
from app.agent.models.plan import Plan


class BehaviorPath(str, Enum):
    DIRECT = "direct"
    PLANNER = "planner"


class BehaviorProfile(BaseModel):
    """The Behavior Gate's decision: which path this run takes, and why."""

    model_config = ConfigDict(frozen=True)

    path: BehaviorPath
    reason: str = ""
    confidence: float = 1.0


class WorkingContextItem(BaseModel):
    """One piece of always-loaded working context (e.g. a recent message,
    the thread summary, a preference, a knowledge entry)."""

    model_config = ConfigDict(frozen=True)

    source: str
    content: str
    metadata: dict = Field(default_factory=dict)


class ToolOutput(BaseModel):
    """A normalized output produced by executing a capability/tool."""

    model_config = ConfigDict(frozen=True)

    capability_id: str | None = None
    step_id: str | None = None
    output: dict = Field(default_factory=dict)
    metadata: dict = Field(default_factory=dict)


class EvidenceItem(BaseModel):
    """A piece of grounding evidence surfaced for the final answer."""

    model_config = ConfigDict(frozen=True)

    source: str
    content: str
    score: float | None = None
    metadata: dict = Field(default_factory=dict)


def _new_run_id() -> str:
    return uuid.uuid4().hex


class RunContext:
    """Mutable, append-only run state. Use :meth:`create` to construct one."""

    def __init__(
        self,
        *,
        user_request: str,
        user_id: str,
        run_id: str,
        thread_id: str | None = None,
        working_context: list[WorkingContextItem] | None = None,
        execution_state: ExecutionState | None = None,
        metadata: dict | None = None,
    ) -> None:
        self.user_request = user_request
        self.user_id = user_id
        self.thread_id = thread_id
        self.run_id = run_id

        # Snapshot the working context so neither the caller's list nor later
        # stages can mutate the preserved context.
        self._working_context: list[WorkingContextItem] = list(working_context or [])

        self.behavior_profile: BehaviorProfile | None = None
        self.selected_capabilities: list[str] = []
        self.plan: Plan | None = None
        self.execution_state: ExecutionState = execution_state or ExecutionState(
            run_id=run_id, plan_id=""
        )
        self.tool_outputs: list[ToolOutput] = []
        self.evidence: list[EvidenceItem] = []
        self.metadata: dict = dict(metadata or {})

    # -- Construction --------------------------------------------------------

    @classmethod
    def create(
        cls,
        user_request: str,
        user_id: str,
        thread_id: str | None = None,
        working_context: list[WorkingContextItem] | None = None,
        execution_state: ExecutionState | None = None,
        metadata: dict | None = None,
    ) -> "RunContext":
        """Build a RunContext from a user request, generating a fresh run_id.

        If no ExecutionState is provided, an empty one is initialized and bound
        to this run (its plan_id is filled in when a plan is attached).
        """
        return cls(
            user_request=user_request,
            user_id=user_id,
            run_id=_new_run_id(),
            thread_id=thread_id,
            working_context=working_context,
            execution_state=execution_state,
            metadata=metadata,
        )

    # -- Working context (read-only after creation) --------------------------

    @property
    def working_context(self) -> list[WorkingContextItem]:
        # A copy, so callers cannot mutate the preserved working context.
        return list(self._working_context)

    # -- Append-only accumulation -------------------------------------------

    def append_tool_output(self, output: ToolOutput) -> None:
        self.tool_outputs.append(output)

    def append_evidence(self, item: EvidenceItem) -> None:
        self.evidence.append(item)

    # -- Attach single-value artifacts --------------------------------------

    def attach_behavior_profile(self, profile: BehaviorProfile) -> None:
        self.behavior_profile = profile

    def attach_selected_capabilities(self, capability_ids: list[str]) -> None:
        self.selected_capabilities = list(capability_ids)

    def attach_plan(self, plan: Plan) -> None:
        self.plan = plan
        if not self.execution_state.plan_id:
            self.execution_state.plan_id = plan.id

    def attach_execution_state(self, state: ExecutionState) -> None:
        self.execution_state = state

    # -- Views ---------------------------------------------------------------

    def planner_view(self) -> dict:
        """What the planner needs: request + working context + capabilities."""
        return {
            "run_id": self.run_id,
            "user_id": self.user_id,
            "thread_id": self.thread_id,
            "user_request": self.user_request,
            "working_context": [item.model_dump() for item in self._working_context],
            "selected_capabilities": list(self.selected_capabilities),
        }

    def final_response_view(self) -> dict:
        """What the final answer needs: request + working context + outputs + evidence."""
        return {
            "run_id": self.run_id,
            "user_id": self.user_id,
            "thread_id": self.thread_id,
            "user_request": self.user_request,
            "working_context": [item.model_dump() for item in self._working_context],
            "tool_outputs": [o.model_dump() for o in self.tool_outputs],
            "evidence": [e.model_dump() for e in self.evidence],
        }
