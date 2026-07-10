"""Phase 42A tests — operational HTTP middleware on a bare app (config-free)."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.http_middleware import (
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from app.observability.metrics import InMemoryMetrics
from app.rate_limit import InMemoryRateLimiter, RateLimits


def _app(*, metrics=None, rate_limit_enabled=False, limits=None, clock=None):
    app = FastAPI()

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    @app.post("/agent/run")
    async def run():
        return {"ran": True}

    @app.post("/agent/run/stream")
    async def stream():
        return {"streamed": True}

    @app.get("/boom")
    async def boom():
        raise RuntimeError("super-secret internal detail 42")

    app.add_middleware(
        RateLimitMiddleware,
        enabled=rate_limit_enabled,
        limiter=InMemoryRateLimiter(clock=clock or (lambda: 0.0)),
        limits=limits or RateLimits(run=2, stream=1, resume=5),
        metrics=metrics,
    )
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=1000)
    app.add_middleware(SecurityHeadersMiddleware, enabled=True)
    app.add_middleware(RequestContextMiddleware, header_name="X-Request-ID", metrics=metrics)
    return app


# --------------------------------------------------------------------------- #
# Correlation
# --------------------------------------------------------------------------- #

def test_correlation_id_generated_and_returned():
    client = TestClient(_app())
    resp = client.get("/ping")
    assert resp.status_code == 200
    cid = resp.headers.get("X-Request-ID")
    assert cid and len(cid) >= 8


def test_valid_incoming_correlation_id_preserved():
    client = TestClient(_app())
    resp = client.get("/ping", headers={"X-Request-ID": "client-Trace_123"})
    assert resp.headers["X-Request-ID"] == "client-Trace_123"


def test_unsafe_incoming_correlation_id_replaced():
    client = TestClient(_app())
    resp = client.get("/ping", headers={"X-Request-ID": "bad id with spaces"})
    assert resp.headers["X-Request-ID"] != "bad id with spaces"
    assert " " not in resp.headers["X-Request-ID"]


# --------------------------------------------------------------------------- #
# Security headers
# --------------------------------------------------------------------------- #

def test_security_headers_present():
    resp = TestClient(_app()).get("/ping")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert "Content-Security-Policy" in resp.headers


# --------------------------------------------------------------------------- #
# Body size limit
# --------------------------------------------------------------------------- #

def test_oversized_body_is_rejected_413():
    client = TestClient(_app())
    resp = client.post("/agent/run", content=b"x" * 2000, headers={"content-type": "application/json"})
    assert resp.status_code == 413


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #

def test_rate_limit_disabled_is_passthrough():
    client = TestClient(_app(rate_limit_enabled=False))
    for _ in range(5):
        assert client.post("/agent/run").status_code == 200


def test_rate_limit_returns_429_with_retry_after():
    metrics = InMemoryMetrics()
    client = TestClient(_app(rate_limit_enabled=True, limits=RateLimits(run=2), metrics=metrics))
    assert client.post("/agent/run").status_code == 200
    assert client.post("/agent/run").status_code == 200
    blocked = client.post("/agent/run")
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1
    # 429 still carries the correlation + security headers
    assert "X-Request-ID" in blocked.headers
    assert blocked.headers["X-Content-Type-Options"] == "nosniff"


def test_rate_limit_buckets_are_distinct():
    client = TestClient(_app(rate_limit_enabled=True, limits=RateLimits(run=1, stream=1)))
    assert client.post("/agent/run").status_code == 200
    assert client.post("/agent/run").status_code == 429
    # a different bucket has its own budget
    assert client.post("/agent/run/stream").status_code == 200


# --------------------------------------------------------------------------- #
# Metrics + safe errors
# --------------------------------------------------------------------------- #

def test_http_metrics_recorded():
    metrics = InMemoryMetrics()
    client = TestClient(_app(metrics=metrics))
    client.get("/ping")
    snap = metrics.snapshot()
    assert any("http_requests_total" in s for s in snap["counters"])
    assert any("http_request_duration_ms" in s for s in snap["summaries"])
    # no sensitive/high-cardinality labels anywhere
    blob = str(snap)
    assert "user_id" not in blob and "correlation" not in blob


def test_server_error_does_not_leak_internal_detail():
    client = TestClient(_app(), raise_server_exceptions=False)
    resp = client.get("/boom")
    assert resp.status_code == 500
    assert "super-secret internal detail 42" not in resp.text
