"""Phase 42A tests — health/readiness logic (config-free, injected checks)."""

import asyncio

from app.health import liveness, run_readiness


def run(coro):
    return asyncio.run(coro)


def test_liveness_is_static():
    assert liveness() == {"status": "alive"}


def test_readiness_all_ok():
    async def ok():
        return True

    report = run(run_readiness({"mongodb": ok, "redis": ok}))
    assert report["status"] == "ready"
    assert report["dependencies"] == {"mongodb": "ok", "redis": "ok"}


def test_readiness_marks_failed_dependency_unavailable_without_leaking():
    async def ok():
        return True

    async def boom():
        raise RuntimeError("connection string mongodb://user:pass@host leaked!")

    report = run(run_readiness({"mongodb": ok, "qdrant": boom}))
    assert report["status"] == "not_ready"
    assert report["dependencies"] == {"mongodb": "ok", "qdrant": "unavailable"}
    # the raw error / credentials never appear in the report
    assert "pass@host" not in str(report)


def test_readiness_falsy_result_is_unavailable():
    async def down():
        return False

    report = run(run_readiness({"redis": down}))
    assert report["dependencies"]["redis"] == "unavailable"
    assert report["status"] == "not_ready"
