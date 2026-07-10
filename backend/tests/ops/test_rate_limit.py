"""Phase 42A tests — rate limiter primitives (config-free)."""

import asyncio

from app.rate_limit import InMemoryRateLimiter, RateLimits, bucket_for_path


def run(coro):
    return asyncio.run(coro)


def test_sliding_window_allows_up_to_limit_then_blocks():
    clk = [0.0]
    limiter = InMemoryRateLimiter(clock=lambda: clk[0])

    async def go():
        results = [await limiter.check("k", 3, 60) for _ in range(4)]
        return results

    results = run(go())
    assert [r.allowed for r in results] == [True, True, True, False]
    assert results[-1].retry_after >= 1


def test_window_slides_and_reallows():
    clk = [0.0]
    limiter = InMemoryRateLimiter(clock=lambda: clk[0])

    async def go():
        for _ in range(3):
            await limiter.check("k", 3, 60)
        blocked = await limiter.check("k", 3, 60)
        clk[0] = 61.0  # window elapsed
        allowed = await limiter.check("k", 3, 60)
        return blocked, allowed

    blocked, allowed = run(go())
    assert not blocked.allowed
    assert allowed.allowed


def test_keys_are_independent():
    limiter = InMemoryRateLimiter(clock=lambda: 0.0)

    async def go():
        a = await limiter.check("a", 1, 60)
        a2 = await limiter.check("a", 1, 60)
        b = await limiter.check("b", 1, 60)
        return a, a2, b

    a, a2, b = run(go())
    assert a.allowed and not a2.allowed and b.allowed


def test_bucket_for_path():
    assert bucket_for_path("POST", "/agent/run") == "run"
    assert bucket_for_path("POST", "/agent/run/stream") == "stream"
    assert bucket_for_path("POST", "/agent/resume") == "resume"
    assert bucket_for_path("GET", "/agent/run") is None      # only POST
    assert bucket_for_path("POST", "/health") is None
    # /run/stream must not be mistaken for /run
    assert bucket_for_path("POST", "/agent/run/stream") != "run"


def test_rate_limits_lookup():
    limits = RateLimits(run=5, stream=2, resume=9)
    assert limits.for_bucket("run") == 5
    assert limits.for_bucket("stream") == 2
    assert limits.for_bucket("resume") == 9
