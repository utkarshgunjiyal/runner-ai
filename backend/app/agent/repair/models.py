"""Repair result model (Phase 21).

The Answer Evaluation Engine (Phase 20) emits an ``EvaluationReport`` carrying a
``RepairDecision``. The Repair Runtime turns that decision into a concrete
``RepairResult`` — either a prompt/context modification ready to feed back to the
provider, or a deferred hand-off that names the stage responsible (without
executing it in this phase).

Config-free: pydantic only, plus the (config-free) RepairAction / FinalPrompt /
RunContext types. No LLM, no database, no application settings.
"""

from pydantic import BaseModel, ConfigDict, Field

from app.agent.evaluation.models import RepairAction
from app.agent.models.final_prompt import FinalPrompt
from app.agent.runtime.context import RunContext


class RepairResult(BaseModel):
    """Outcome of one repair attempt.

    ``applied`` is True when the runtime actually produced a repaired artifact
    this phase (prompt modification / warning / graceful failure) and False when
    the action is a no-op or a deferred hand-off to another stage.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    action: RepairAction
    applied: bool = False
    reason: str = ""
    target_stage: str | None = None
    updated_final_prompt: FinalPrompt | None = None
    updated_run_context: RunContext | None = None
    metadata: dict = Field(default_factory=dict)
