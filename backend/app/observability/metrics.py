"""Provider-neutral metrics boundary (Phase 42A).

A small ``MetricsSink`` protocol the app calls; the default is a no-op so tests
and dev need no metrics backend. ``InMemoryMetrics`` aggregates counters, gauges,
and latency summaries for a text ``/metrics`` endpoint; a Prometheus adapter is
isolated behind an optional import.

Safety: labels are sanitized. High-cardinality/sensitive keys (user_id,
thread_id, run_id, prompts, arguments, credentials, …) are dropped, and the
number of distinct label-sets per metric is capped — so metrics can never leak
identifiers or explode cardinality.
"""

from __future__ import annotations

import threading
from typing import Protocol, runtime_checkable

# Label keys that must never become metric dimensions (cardinality + privacy).
_FORBIDDEN_LABELS = frozenset({
    "user_id", "thread_id", "run_id", "checkpoint_id", "trace_id", "request_id",
    "correlation_id", "prompt", "query", "user_request", "answer", "text",
    "capability_args", "arguments", "args", "email", "token", "api_key",
    "authorization", "headers", "environment", "path_params",
})
# Cap distinct label-sets per metric so a stray dynamic label cannot explode.
_MAX_SERIES_PER_METRIC = 256


def sanitize_labels(labels: dict) -> dict:
    """Drop forbidden/high-cardinality keys and coerce values to short strings."""
    clean: dict[str, str] = {}
    for key, value in labels.items():
        lk = str(key).lower()
        if lk in _FORBIDDEN_LABELS:
            continue
        text = str(value)
        if len(text) > 64:
            text = text[:64]
        clean[lk] = text
    return clean


def _series_key(labels: dict) -> tuple:
    return tuple(sorted(labels.items()))


@runtime_checkable
class MetricsSink(Protocol):
    """The metrics surface the app depends on."""

    def incr(self, name: str, value: float = 1.0, **labels: object) -> None: ...
    def observe(self, name: str, value: float, **labels: object) -> None: ...
    def gauge(self, name: str, value: float, **labels: object) -> None: ...


class NoOpMetrics:
    """Default sink: records nothing. Zero cost, no backend required."""

    def incr(self, name: str, value: float = 1.0, **labels: object) -> None:
        return None

    def observe(self, name: str, value: float, **labels: object) -> None:
        return None

    def gauge(self, name: str, value: float, **labels: object) -> None:
        return None


class InMemoryMetrics:
    """Thread-safe in-process aggregation for a text ``/metrics`` endpoint."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple], float] = {}
        self._gauges: dict[tuple[str, tuple], float] = {}
        # name+labels -> (count, sum, min, max)
        self._summaries: dict[tuple[str, tuple], list[float]] = {}
        self._series_per_metric: dict[str, set[tuple]] = {}

    def _admit(self, name: str, labels: dict) -> tuple | None:
        key = _series_key(labels)
        seen = self._series_per_metric.setdefault(name, set())
        if key not in seen:
            if len(seen) >= _MAX_SERIES_PER_METRIC:
                return None  # cardinality cap reached — drop this label-set
            seen.add(key)
        return key

    def incr(self, name: str, value: float = 1.0, **labels: object) -> None:
        clean = sanitize_labels(labels)
        with self._lock:
            key = self._admit(name, clean)
            if key is None:
                return
            self._counters[(name, key)] = self._counters.get((name, key), 0.0) + value

    def gauge(self, name: str, value: float, **labels: object) -> None:
        clean = sanitize_labels(labels)
        with self._lock:
            key = self._admit(name, clean)
            if key is None:
                return
            self._gauges[(name, key)] = float(value)

    def observe(self, name: str, value: float, **labels: object) -> None:
        clean = sanitize_labels(labels)
        with self._lock:
            key = self._admit(name, clean)
            if key is None:
                return
            summary = self._summaries.get((name, key))
            if summary is None:
                self._summaries[(name, key)] = [1.0, float(value), float(value), float(value)]
            else:
                summary[0] += 1
                summary[1] += value
                summary[2] = min(summary[2], value)
                summary[3] = max(summary[3], value)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "counters": {self._fmt(n, k): v for (n, k), v in self._counters.items()},
                "gauges": {self._fmt(n, k): v for (n, k), v in self._gauges.items()},
                "summaries": {
                    self._fmt(n, k): {"count": s[0], "sum": s[1], "min": s[2], "max": s[3]}
                    for (n, k), s in self._summaries.items()
                },
            }

    def render_text(self) -> str:
        """A simple, Prometheus-compatible exposition (counter/gauge + summary)."""
        lines: list[str] = []
        snap = self.snapshot()
        for series, value in sorted(snap["counters"].items()):
            lines.append(f"{series} {value}")
        for series, value in sorted(snap["gauges"].items()):
            lines.append(f"{series} {value}")
        for series, agg in sorted(snap["summaries"].items()):
            base = series.split("{")[0]
            suffix = series[len(base):]
            lines.append(f"{base}_count{suffix} {agg['count']}")
            lines.append(f"{base}_sum{suffix} {agg['sum']}")
        return "\n".join(lines) + ("\n" if lines else "")

    @staticmethod
    def _fmt(name: str, key: tuple) -> str:
        if not key:
            return name
        labels = ",".join(f'{k}="{v}"' for k, v in key)
        return f"{name}{{{labels}}}"


# ------------------------------------------------------------------------- #
# Global sink (composition root sets it; app code reads it)
# ------------------------------------------------------------------------- #

_sink: MetricsSink = NoOpMetrics()


def get_metrics() -> MetricsSink:
    return _sink


def configure_metrics(sink: MetricsSink) -> None:
    """Composition-root hook: install the process metrics sink."""
    global _sink
    _sink = sink


def build_metrics_sink(backend: str) -> MetricsSink:
    """Build a sink for the configured backend. Falls back to in-memory if a
    Prometheus client is not installed (never fails startup)."""
    if backend == "prometheus":
        try:
            from app.observability.prometheus_metrics import PrometheusMetrics

            return PrometheusMetrics()
        except Exception:  # noqa: BLE001 - degrade gracefully, never crash startup
            return InMemoryMetrics()
    return InMemoryMetrics()
