"""Provider-agnostic resource model (Phase 46.3.1).

A ``Resource`` is a single deterministically-resolved value a tool call needs —
a GitHub ``owner``/``repo``/``issue_number``, a Gmail ``thread_id``, a Slack
``channel`` — tagged with WHERE it came from (its deterministic source). The
framework never interprets a resource ``type``: the provider resolver and that
provider's argument builder share the vocabulary, which is exactly what keeps this
layer provider-agnostic.

Nothing here is GitHub-specific. New providers reuse these types unchanged.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class ResourceSource(str, Enum):
    """Deterministic sources, in resolution-priority order (lower ordinal wins).

    The LLM is never a source — resources are resolved deterministically or the
    request is rejected for clarification.
    """

    REQUEST = "request"                     # 1. explicit in the current user request
    PRIOR_OUTPUT = "prior_output"           # 2. a previous successful tool output (thread)
    THREAD_STATE = "thread_state"           # 3. thread execution state
    CONNECTOR_IDENTITY = "connector_identity"  # 4. trusted connector identity
    CACHED_CONTEXT = "cached_context"       # 5. cached execution context
    CLARIFICATION = "clarification"         # 6. explicit user clarification

    @property
    def priority(self) -> int:
        return _PRIORITY.index(self)


_PRIORITY = [
    ResourceSource.REQUEST,
    ResourceSource.PRIOR_OUTPUT,
    ResourceSource.THREAD_STATE,
    ResourceSource.CONNECTOR_IDENTITY,
    ResourceSource.CACHED_CONTEXT,
    ResourceSource.CLARIFICATION,
]


class Resource(BaseModel):
    """One resolved resource value with its deterministic provenance."""

    model_config = ConfigDict(frozen=True)

    type: str                # provider-vocabulary key, e.g. "owner", "repo", "issue_number"
    value: str | int
    source: ResourceSource
    provider: str


class ResolvedResources(BaseModel):
    """The resources a provider resolver produced for one request.

    ``resources`` holds the winning ``Resource`` per type. ``ambiguous`` reports a
    type that matched several candidates (→ clarify, never guess). ``flags`` carries
    provider-neutral booleans a builder needs (e.g. ``account_scoped``).
    """

    model_config = ConfigDict(frozen=True)

    provider: str
    resources: dict[str, Resource] = Field(default_factory=dict)
    ambiguous: dict[str, int] = Field(default_factory=dict)
    flags: dict[str, bool] = Field(default_factory=dict)

    def get(self, resource_type: str):
        r = self.resources.get(resource_type)
        return r.value if r is not None else None

    def source_of(self, resource_type: str) -> ResourceSource | None:
        r = self.resources.get(resource_type)
        return r.source if r is not None else None

    def flag(self, name: str) -> bool:
        return bool(self.flags.get(name))

    @property
    def is_ambiguous(self) -> bool:
        return any(count > 1 for count in self.ambiguous.values())

    def resolved_types(self) -> list[str]:
        return sorted(self.resources.keys())

    def source_map(self) -> dict[str, str]:
        """type -> source value (safe: names/provenance only, never used for values)."""
        return {t: r.source.value for t, r in self.resources.items()}


class ResolutionContext(BaseModel):
    """The deterministic inputs a resolver may read — assembled by the runtime.

    A resolver receives only these sources (never arbitrary runtime state), so it
    stays pure and testable. ``execution_state`` is a read-only, provider-namespaced
    view of prior resources/thread state (formalized into a store in Phase 46.3.2;
    a plain dict here). ``hints`` carries planner-supplied concrete args.
    """

    model_config = ConfigDict(frozen=True)

    provider: str
    capability_id: str
    user_request: str
    identity: dict | None = None
    execution_state: dict = Field(default_factory=dict)
    hints: dict = Field(default_factory=dict)
