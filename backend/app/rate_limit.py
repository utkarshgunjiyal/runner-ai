"""API rate limiting primitives (Phase 42A) — config-free.

A limiter behind a Protocol: a per-process sliding-window ``InMemoryRateLimiter``
(local/dev/tests only) and a Redis-backed fixed-window ``RedisRateLimiter`` for
real multi-process limits. Enforcement is wired as an HTTP middleware
(``app.http_middleware.RateLimitMiddleware``) at the transport boundary — never
inside runtime business logic. Off by default; distinct buckets per agent route.

Config-free at import so it stays testable without application settings.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after: int = 0


@dataclass(frozen=True)
class RateLimits:
    """Per-bucket request budgets (per 60s window)."""

    run: int = 30
    stream: int = 10
    resume: int = 60

    def for_bucket(self, bucket: str) -> int:
        return {"run": self.run, "stream": self.stream, "resume": self.resume}.get(bucket, self.run)


class RateLimiter(Protocol):
    async def check(self, key: str, limit: int, window_seconds: int) -> RateLimitResult: ...


class InMemoryRateLimiter:
    """Sliding-window limiter over in-process timestamps. Single-process only."""

    def __init__(self, *, clock=time.monotonic) -> None:
        self._clock = clock
        self._events: dict[str, deque[float]] = defaultdict(deque)

    async def check(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        now = self._clock()
        window_start = now - window_seconds
        events = self._events[key]
        while events and events[0] < window_start:
            events.popleft()
        if len(events) >= limit:
            retry_after = max(1, int(window_seconds - (now - events[0])) + 1)
            return RateLimitResult(allowed=False, retry_after=retry_after)
        events.append(now)
        return RateLimitResult(allowed=True)


class RedisRateLimiter:
    """Fixed-window limiter using Redis INCR + EXPIRE. Correct across processes.

    Never fails a request on a limiter/backend error (fails open) — availability
    over strictness at the edge.
    """

    def __init__(self, redis_client) -> None:
        self._redis = redis_client

    async def check(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        window = int(time.time()) // window_seconds
        bucket = f"ratelimit:{key}:{window}"
        try:
            count = await _maybe_await(self._redis.incr(bucket))
            await _maybe_await(self._redis.expire(bucket, window_seconds))
        except Exception:  # noqa: BLE001 - fail open on backend error
            return RateLimitResult(allowed=True)
        if count > limit:
            retry_after = window_seconds - (int(time.time()) % window_seconds)
            return RateLimitResult(allowed=False, retry_after=max(1, retry_after))
        return RateLimitResult(allowed=True)


async def _maybe_await(value):
    if hasattr(value, "__await__"):
        return await value
    return value


# Exact-path → bucket (distinct limits; /run/stream must not match /run).
_PATH_BUCKETS = {
    "/agent/run": "run",
    "/agent/run/stream": "stream",
    "/agent/resume": "resume",
}


def bucket_for_path(method: str, path: str) -> str | None:
    """Return the rate-limit bucket for a request, or None to skip limiting."""
    if method.upper() != "POST":
        return None
    return _PATH_BUCKETS.get(path.rstrip("/") or path)
