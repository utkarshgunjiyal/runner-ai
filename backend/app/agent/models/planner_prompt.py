"""Provider-agnostic planner prompt (Phase 36).

The structured input a ``PlannerProvider`` turns into an ExecutionPlan. It
carries only what the planner is allowed to see — the user request, the
prioritized/budgeted working context, the behavior profile, and the retrieved
**top-k** capability views — never the full capability registry and never the
raw RunContext object.

Config-free: pydantic only. No LLM, no registry, no settings.
"""

from pydantic import BaseModel, ConfigDict, Field

# Default JSON contract the planner is asked to satisfy (used by adapters and
# documented for the LLM). Kept small and vendor-neutral.
DEFAULT_PLAN_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "goal": {"type": "string"},
        "final_response_mode": {
            "type": "string",
            "enum": ["answer", "summarize_results", "action_result", "ask_clarification"],
        },
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "request": {"type": "string"},
                    "optional": {"type": "boolean"},
                    "capability_id": {"type": "string"},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "request"],
            },
        },
    },
    "required": ["tasks"],
}


class CapabilityView(BaseModel):
    """A compact, planner-facing view of one capability (never the full ToolSpec)."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str = ""
    description: str = ""
    tags: list[str] = Field(default_factory=list)


class PlannerPrompt(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_request: str
    working_context: list[dict] = Field(default_factory=list)
    behavior_profile: dict | None = None
    capabilities: list[CapabilityView] = Field(default_factory=list)
    planning_constraints: dict = Field(default_factory=dict)
    output_schema: dict = Field(default_factory=dict)
    metadata: dict = Field(default_factory=dict)

    def allowed_capability_ids(self) -> set[str]:
        return {capability.id for capability in self.capabilities}


def build_planner_prompt(
    run_context,
    capability_matches,
    *,
    constraints: dict | None = None,
    output_schema: dict | None = None,
    max_capabilities: int | None = None,
) -> PlannerPrompt:
    """Assemble a PlannerPrompt from a RunContext + retrieved top-k capabilities.

    ``capability_matches`` are the top-k CapabilityMatch objects from retrieval —
    NOT the registry. Only their id/name/description/tags are surfaced.
    """

    matches = capability_matches[:max_capabilities] if max_capabilities else list(capability_matches)
    capabilities = [
        CapabilityView(
            id=m.tool.id, name=m.tool.name, description=m.tool.description, tags=list(m.tool.tags)
        )
        for m in matches
    ]

    profile = None
    if run_context.behavior_profile is not None:
        profile = {
            "path": run_context.behavior_profile.path.value,
            "reason": run_context.behavior_profile.reason,
            "confidence": run_context.behavior_profile.confidence,
        }

    working_context = [
        {"source": item.source, "content": item.content}
        for item in run_context.working_context  # a copy; the RunContext is never sent
    ]

    return PlannerPrompt(
        user_request=run_context.user_request,
        working_context=working_context,
        behavior_profile=profile,
        capabilities=capabilities,
        planning_constraints=constraints or {},
        output_schema=output_schema or DEFAULT_PLAN_SCHEMA,
        metadata={
            "run_id": run_context.run_id,
            "user_id": run_context.user_id,
            "thread_id": run_context.thread_id,
        },
    )
