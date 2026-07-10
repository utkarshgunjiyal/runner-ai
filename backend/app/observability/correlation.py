"""Request correlation IDs (Phase 42A).

Accept a client-supplied correlation id only when it is *safe* (bounded length,
conservative charset), otherwise generate one. This id is for tracing/logging
only — it is independent of the runtime's ``run_id`` and confers no privilege, so
a client can never use it to invent a runtime identity.
"""

import re
import uuid

# Conservative: letters, digits, dash, underscore, dot; 8–128 chars.
_VALID_CORRELATION_ID = re.compile(r"^[A-Za-z0-9._-]{8,128}$")


def is_valid_correlation_id(value: str | None) -> bool:
    return bool(value) and bool(_VALID_CORRELATION_ID.match(value or ""))


def generate_correlation_id() -> str:
    return uuid.uuid4().hex


def resolve_correlation_id(incoming: str | None) -> str:
    """Honor a valid incoming id; otherwise mint a fresh one."""
    if is_valid_correlation_id(incoming):
        return incoming  # type: ignore[return-value]
    return generate_correlation_id()
