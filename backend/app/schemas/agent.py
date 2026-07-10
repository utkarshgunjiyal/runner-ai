"""Agent API schemas (Phase 30).

Request/response models for POST /agent/run. Deliberately API-safe: the internal
RunContext and the full FinalPrompt are never exposed — only the final answer
text, the terminal runtime outcome, and (for waiting outcomes) the checkpoint id
and pending fields.
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AgentRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_request: str = Field(min_length=1)
    thread_id: str | None = None
    metadata: dict | None = None

    @field_validator("user_request")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("user_request must be a non-empty string")
        return value


class AgentRunResponse(BaseModel):
    run_id: str
    thread_id: str | None = None
    runtime_outcome: str
    answer: str | None = None
    checkpoint_id: str | None = None
    pending_action: str | None = None
    pending_reason: str | None = None
    metadata: dict = Field(default_factory=dict)
