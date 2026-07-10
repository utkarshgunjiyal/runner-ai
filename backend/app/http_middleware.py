"""Operational HTTP middleware (Phase 42A) — config-free, testable in isolation.

Each middleware takes its configuration via constructor args (not global
settings), so tests exercise them on a bare app and ``main.py`` wires them with
values from ``settings``. None of these change runtime/agent behavior.
"""

from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.logging_config import get_logger, request_id_ctx
from app.observability.correlation import resolve_correlation_id
from app.observability.metrics import MetricsSink, NoOpMetrics
from app.rate_limit import RateLimiter, RateLimits, bucket_for_path

_logger = get_logger("http")


def _status_group(status_code: int) -> str:
    return f"{status_code // 100}xx"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Correlation id + structured request logging + HTTP metrics.

    Honors a *valid* incoming correlation header, else generates one; binds it to
    the log context and echoes it on the response. Records request count, latency,
    status group, and active-request gauge (no high-cardinality labels).
    """

    def __init__(self, app, *, header_name: str = "X-Request-ID", metrics: MetricsSink | None = None) -> None:
        super().__init__(app)
        self._header = header_name
        self._metrics = metrics or NoOpMetrics()
        self._active = 0

    async def dispatch(self, request: Request, call_next):
        correlation_id = resolve_correlation_id(request.headers.get(self._header))
        token = request_id_ctx.set(correlation_id)
        request.state.correlation_id = correlation_id
        # Real auth would set request.state.user_id upstream; default unknown.
        if not hasattr(request.state, "user_id"):
            request.state.user_id = None

        method = request.method
        self._active += 1
        self._metrics.gauge("http_active_requests", self._active)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            self._metrics.incr("http_requests_total", 1.0, method=method, status_group="5xx")
            self._metrics.observe("http_request_duration_ms", duration_ms, method=method, status_group="5xx")
            self._active -= 1
            self._metrics.gauge("http_active_requests", self._active)
            _logger.exception("request.failed", extra={"method": method, "path": request.url.path, "duration_ms": duration_ms})
            request_id_ctx.reset(token)
            raise

        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        group = _status_group(response.status_code)
        self._metrics.incr("http_requests_total", 1.0, method=method, status_group=group)
        self._metrics.observe("http_request_duration_ms", duration_ms, method=method, status_group=group)
        self._active -= 1
        self._metrics.gauge("http_active_requests", self._active)
        _logger.info(
            "request.completed",
            extra={"method": method, "path": request.url.path, "status_code": response.status_code, "duration_ms": duration_ms},
        )
        response.headers[self._header] = correlation_id
        request_id_ctx.reset(token)
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds conservative security response headers (safe for an API)."""

    def __init__(self, app, *, enabled: bool = True, csp: str = "default-src 'none'; frame-ancestors 'none'") -> None:
        super().__init__(app)
        self._enabled = enabled
        self._csp = csp

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if self._enabled:
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("Referrer-Policy", "no-referrer")
            response.headers.setdefault("X-Frame-Options", "DENY")
            response.headers.setdefault("Content-Security-Policy", self._csp)
        return response


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Rejects oversized request bodies (413) based on Content-Length."""

    def __init__(self, app, *, max_bytes: int) -> None:
        super().__init__(app)
        self._max = max_bytes

    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > self._max:
                    return JSONResponse({"detail": "Request body too large."}, status_code=413)
            except ValueError:
                pass
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Enforces per-route rate limits at the transport boundary. No-op when disabled."""

    def __init__(self, app, *, enabled: bool, limiter: RateLimiter, limits: RateLimits, metrics: MetricsSink | None = None) -> None:
        super().__init__(app)
        self._enabled = enabled
        self._limiter = limiter
        self._limits = limits
        self._metrics = metrics or NoOpMetrics()

    async def dispatch(self, request: Request, call_next):
        if not self._enabled:
            return await call_next(request)
        bucket = bucket_for_path(request.method, request.url.path)
        if bucket is None:
            return await call_next(request)
        identity = getattr(request.state, "user_id", None) or (request.client.host if request.client else "anonymous")
        key = f"{bucket}:{identity}"
        result = await self._limiter.check(key, self._limits.for_bucket(bucket), 60)
        if not result.allowed:
            self._metrics.incr("http_rate_limited_total", 1.0, route=bucket)
            return JSONResponse(
                {"detail": "Rate limit exceeded. Please slow down."},
                status_code=429,
                headers={"Retry-After": str(result.retry_after)},
            )
        return await call_next(request)
