"""Standard adapter result types for the Execution Bridge (Phase 13).

Every adapter — internal V1.5, external API, or MCP — returns an
``AdapterResult`` instead of a bare dict. This gives the Executor and the
Recovery Pipeline (ARCHITECTURE.md §20) one uniform shape to reason about:
success/failure, normalized evidence, a confidence signal, and a
retryable/partial classification for deterministic recovery.

Config-free: imports only pydantic and the (config-free) RunContext evidence
model. No application settings, no database, no V1.5 service imports.
"""

from pydantic import BaseModel, ConfigDict, Field

from app.agent.runtime.context import EvidenceItem


class ErrorCode:
    """Stable error-code constants adapters emit on failure.

    Deterministic recovery keys off these plus ``AdapterResult.retryable``.
    """

    UNKNOWN_CAPABILITY = "unknown_capability"
    INVALID_ARGS = "invalid_args"
    NOT_FOUND = "not_found"
    UPSTREAM_TIMEOUT = "upstream_timeout"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    UPSTREAM_ERROR = "upstream_error"


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


class AdapterResult(BaseModel):
    """Uniform result returned by every adapter execution.

    ``output`` is the raw service payload; ``evidence`` is the same payload
    normalized into grounding items the Final Context Builder can consume.
    ``confidence`` is a coarse [0, 1] signal, ``retryable``/``partial`` feed the
    Recovery Pipeline.
    """

    model_config = ConfigDict(frozen=True)

    success: bool
    output: dict = Field(default_factory=dict)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    confidence: float = 1.0
    error_code: str | None = None
    retryable: bool = False
    partial: bool = False
    metadata: dict = Field(default_factory=dict)

    @classmethod
    def ok(
        cls,
        output: dict | None = None,
        *,
        evidence: list[EvidenceItem] | None = None,
        confidence: float = 1.0,
        partial: bool = False,
        metadata: dict | None = None,
    ) -> "AdapterResult":
        return cls(
            success=True,
            output=output or {},
            evidence=evidence or [],
            confidence=_clamp01(confidence),
            partial=partial,
            metadata=metadata or {},
        )

    @classmethod
    def failure(
        cls,
        error_code: str,
        *,
        retryable: bool = False,
        output: dict | None = None,
        partial: bool = False,
        metadata: dict | None = None,
    ) -> "AdapterResult":
        return cls(
            success=False,
            output=output or {},
            evidence=[],
            confidence=0.0,
            error_code=error_code,
            retryable=retryable,
            partial=partial,
            metadata=metadata or {},
        )


def classify_exception(exc: Exception) -> tuple[str, bool]:
    """Map a raised exception to ``(error_code, retryable)``.

    Transient infrastructure faults (timeouts, connection loss) are retryable;
    programming/validation errors are not. Deterministic — no LLM involved.
    """

    if isinstance(exc, TimeoutError):
        return ErrorCode.UPSTREAM_TIMEOUT, True
    if isinstance(exc, ConnectionError):
        return ErrorCode.UPSTREAM_UNAVAILABLE, True
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return ErrorCode.INVALID_ARGS, False
    return ErrorCode.UPSTREAM_ERROR, False
