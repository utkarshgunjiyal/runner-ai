"""Tool-argument build result (Phase 46.2.6).

A provider-neutral contract for translating a natural-language request into
schema-valid MCP tool arguments *before* execution. DirectRuntime calls an
injected argument builder and acts on this result:

- ``ok``        → execute with ``arguments``
- ``missing``   → a required resource could not be resolved; clarify, do NOT
                  execute (no silent global query / guess)
- ``ambiguous`` → more than one resource matched; clarify, do NOT execute

The builder is the only place provider-specific resolution lives; this module is
config-free and imports nothing beyond pydantic, so DirectRuntime stays
source-agnostic (it never imports GitHub).
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class ArgumentStatus(str, Enum):
    OK = "ok"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"


class ArgumentBuildResult(BaseModel):
    """Outcome of building tool arguments for one capability."""

    model_config = ConfigDict(frozen=True)

    status: ArgumentStatus = ArgumentStatus.OK
    #: Schema-valid arguments to execute with (only meaningful when status == OK).
    arguments: dict = Field(default_factory=dict)
    #: Required schema/resource fields that could not be resolved (MISSING).
    missing_fields: list[str] = Field(default_factory=list)
    #: How many candidates matched, when AMBIGUOUS (>1).
    ambiguity_count: int = 0
    #: A short, safe reason code for diagnostics/answers (never contains values).
    reason: str | None = None
    #: Safe, secret-free summary for diagnostics (resource TYPES + provenance
    #: only — never raw values). e.g. {"owner_source": "connector_identity"}.
    resource_summary: dict = Field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == ArgumentStatus.OK

    @classmethod
    def build_ok(cls, arguments: dict, *, resource_summary: dict | None = None) -> "ArgumentBuildResult":
        return cls(status=ArgumentStatus.OK, arguments=dict(arguments),
                   resource_summary=resource_summary or {})

    @classmethod
    def build_missing(
        cls, missing_fields: list[str], *, reason: str | None = None,
        resource_summary: dict | None = None,
    ) -> "ArgumentBuildResult":
        return cls(status=ArgumentStatus.MISSING, missing_fields=list(missing_fields),
                   reason=reason or "missing_required_resource",
                   resource_summary=resource_summary or {})

    @classmethod
    def build_ambiguous(
        cls, field: str, count: int, *, resource_summary: dict | None = None,
    ) -> "ArgumentBuildResult":
        return cls(status=ArgumentStatus.AMBIGUOUS, missing_fields=[field],
                   ambiguity_count=int(count), reason="ambiguous_resource",
                   resource_summary=resource_summary or {})
