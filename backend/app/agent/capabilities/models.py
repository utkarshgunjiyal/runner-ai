"""Request/response models for the Capability Retrieval Engine.

Phase 2: deterministic keyword retrieval only. See docs/architecture/v2.md §4.
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.agent.models.tool_spec import RiskLevel, ToolKind, ToolSpec


class CapabilityRetrievalRequest(BaseModel):
    query: str
    top_k: int = Field(default=8, gt=0)
    include_disabled: bool = False
    allowed_kinds: list[ToolKind] | None = None
    allowed_risk_levels: list[RiskLevel] | None = None
    required_tags: list[str] = Field(default_factory=list)
    excluded_tool_ids: list[str] = Field(default_factory=list)

    @field_validator("query")
    @classmethod
    def _query_non_empty(cls, value: str) -> str:
        if value is None or not value.strip():
            raise ValueError("query must be a non-empty string")
        return value


class CapabilityMatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    tool: ToolSpec
    score: float
    matched_fields: list[str] = Field(default_factory=list)
    matched_terms: list[str] = Field(default_factory=list)
    reason: str = ""


class CapabilityRetrievalResponse(BaseModel):
    query: str
    matches: list[CapabilityMatch] = Field(default_factory=list)
