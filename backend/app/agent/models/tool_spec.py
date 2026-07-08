"""ToolSpec — the metadata contract for every tool in the V2 registry.

Phase 1: model + enums + validation only. No execution, no adapters.
See docs/architecture/v2.md §5.
"""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ToolKind(str, Enum):
    INTERNAL = "internal"
    API = "api"
    MCP = "mcp"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SideEffectType(str, Enum):
    READ = "read"
    WRITE = "write"
    EXTERNAL = "external"


class LatencyClass(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ToolSpec(BaseModel):
    """Immutable metadata describing a tool. Adapters/handlers come later."""

    model_config = ConfigDict(frozen=True)

    # -- Identity ------------------------------------------------------------
    id: str
    name: str
    version: str = "1.0.0"
    kind: ToolKind
    enabled: bool = True
    deprecated: bool = False
    owner: str | None = None
    tags: list[str] = Field(default_factory=list)

    # -- Retrieval / planner metadata ---------------------------------------
    description: str
    capability_tags: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    typical_user_questions: list[str] = Field(default_factory=list)
    success_examples: list[str] = Field(default_factory=list)
    failure_examples: list[str] = Field(default_factory=list)

    # -- Schemas -------------------------------------------------------------
    input_schema: dict
    output_schema: dict
    output_fields: list[str] = Field(default_factory=list)

    # -- Governance ----------------------------------------------------------
    required_permissions: list[str] = Field(default_factory=list)
    risk_level: RiskLevel
    side_effects: SideEffectType
    requires_approval: bool
    idempotent: bool = True
    pii_touched: bool = False
    data_egress: bool = False

    # -- Execution metadata --------------------------------------------------
    handler_ref: str | None = None
    timeout_seconds: int = Field(default=30, gt=0)
    max_retries: int = Field(default=1, ge=0)
    latency_class: LatencyClass = LatencyClass.LOW
    expected_latency_ms: int | None = None
    estimated_token_cost: int | None = None
    estimated_external_cost: float | None = None
    supports_parallel_execution: bool = True
    cacheable: bool = False
    fallback_policy: str | None = None

    # -- Context / observability --------------------------------------------
    context_weight: float = 1.0
    evidence_priority: int = 0
    emit_audit: bool = False
    redact_fields: list[str] = Field(default_factory=list)

    @field_validator("id", "name", "description")
    @classmethod
    def _non_empty(cls, value: str, info):
        if value is None or not value.strip():
            raise ValueError(f"{info.field_name} must be a non-empty string")
        return value

    @model_validator(mode="after")
    def _governance_invariants(self):
        # HIGH risk tools must always require approval.
        if self.risk_level == RiskLevel.HIGH and not self.requires_approval:
            raise ValueError("HIGH risk tools must set requires_approval=True")

        # Tools that mutate state or reach external systems must not be cached.
        if (
            self.side_effects in (SideEffectType.WRITE, SideEffectType.EXTERNAL)
            and self.cacheable
        ):
            raise ValueError(
                "WRITE/EXTERNAL side-effect tools must not be cacheable"
            )

        return self
