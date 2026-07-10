"""Health/readiness logic (Phase 42A) — config-free and dependency-injected.

Liveness = "the process is up" (no dependencies). Readiness = "required
dependencies are reachable". Checks are injected async callables so this is
unit-testable without live services; results are coarse ("ok" / "unavailable")
and never carry error detail, stack traces, or credentials.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

# A readiness check returns truthy when healthy, or raises/returns falsy otherwise.
HealthCheck = Callable[[], Awaitable[object]]


async def _run_one(check: HealthCheck) -> str:
    try:
        result = await check()
    except Exception:  # noqa: BLE001 - never surface dependency error detail
        return "unavailable"
    return "ok" if result or result is None else "unavailable"


async def run_readiness(checks: dict[str, HealthCheck]) -> dict:
    """Run all checks concurrently; return a safe, leak-free readiness report."""
    names = list(checks)
    statuses = await asyncio.gather(*(_run_one(checks[name]) for name in names))
    dependencies = dict(zip(names, statuses))
    ready = all(status == "ok" for status in statuses)
    return {"status": "ready" if ready else "not_ready", "dependencies": dependencies}


def liveness() -> dict:
    return {"status": "alive"}
