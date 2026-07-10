"""Prometheus adapter (Phase 42A), isolated behind an optional dependency.

Imported lazily only when ``metrics_backend=prometheus``. If ``prometheus_client``
is not installed the import fails and the composition root falls back to the
in-memory sink — so Prometheus is strictly opt-in and never a hard dependency.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Summary, generate_latest  # type: ignore

from app.observability.metrics import sanitize_labels

_ALLOWED_LABEL_NAMES = ("route", "method", "status_group", "outcome", "path", "kind", "result")


def _labels(clean: dict) -> dict:
    # Prometheus requires a fixed label set per metric; keep only known-safe keys.
    return {k: clean.get(k, "") for k in _ALLOWED_LABEL_NAMES}


class PrometheusMetrics:
    """A ``MetricsSink`` backed by prometheus_client collectors."""

    def __init__(self) -> None:
        self._counters: dict[str, Counter] = {}
        self._gauges: dict[str, Gauge] = {}
        self._summaries: dict[str, Summary] = {}

    def incr(self, name: str, value: float = 1.0, **labels: object) -> None:
        clean = sanitize_labels(labels)
        c = self._counters.get(name)
        if c is None:
            c = self._counters[name] = Counter(name, name, _ALLOWED_LABEL_NAMES)
        c.labels(**_labels(clean)).inc(value)

    def gauge(self, name: str, value: float, **labels: object) -> None:
        clean = sanitize_labels(labels)
        g = self._gauges.get(name)
        if g is None:
            g = self._gauges[name] = Gauge(name, name, _ALLOWED_LABEL_NAMES)
        g.labels(**_labels(clean)).set(value)

    def observe(self, name: str, value: float, **labels: object) -> None:
        clean = sanitize_labels(labels)
        s = self._summaries.get(name)
        if s is None:
            s = self._summaries[name] = Summary(name, name, _ALLOWED_LABEL_NAMES)
        s.labels(**_labels(clean)).observe(value)

    def render_text(self) -> str:
        return generate_latest().decode("utf-8")
