"""Phase 42A tests — correlation ids + metrics (config-free)."""

from app.observability.correlation import (
    generate_correlation_id,
    is_valid_correlation_id,
    resolve_correlation_id,
)
from app.observability.metrics import InMemoryMetrics, NoOpMetrics, sanitize_labels


# --------------------------------------------------------------------------- #
# Correlation ids
# --------------------------------------------------------------------------- #

def test_valid_correlation_id_is_preserved():
    assert is_valid_correlation_id("abc12345")
    assert resolve_correlation_id("valid-Id_123.4") == "valid-Id_123.4"


def test_invalid_correlation_ids_are_replaced():
    for bad in (None, "", "short", "has space", "x" * 200, "inject;drop", "a/b"):
        assert not is_valid_correlation_id(bad)
        replaced = resolve_correlation_id(bad)
        assert is_valid_correlation_id(replaced)  # a fresh, valid id
        assert replaced != bad


def test_generated_ids_are_valid_and_unique():
    a, b = generate_correlation_id(), generate_correlation_id()
    assert a != b
    assert is_valid_correlation_id(a) and is_valid_correlation_id(b)


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #

def test_noop_metrics_record_nothing():
    m = NoOpMetrics()
    m.incr("x")
    m.gauge("y", 1)
    m.observe("z", 2)  # no error, no state


def test_counters_gauges_summaries():
    m = InMemoryMetrics()
    m.incr("runs_total", 1, outcome="completed")
    m.incr("runs_total", 2, outcome="completed")
    m.gauge("active", 3)
    m.observe("latency_ms", 10)
    m.observe("latency_ms", 20)
    snap = m.snapshot()
    assert snap["counters"]['runs_total{outcome="completed"}'] == 3
    assert snap["gauges"]["active"] == 3
    assert snap["summaries"]["latency_ms"] == {"count": 2, "sum": 30, "min": 10, "max": 20}


def test_forbidden_labels_are_dropped():
    clean = sanitize_labels({"outcome": "ok", "user_id": "u1", "prompt": "secret", "run_id": "r"})
    assert clean == {"outcome": "ok"}
    m = InMemoryMetrics()
    m.incr("runs_total", 1, user_id="u1", thread_id="t1", outcome="ok")
    series = list(m.snapshot()["counters"])
    assert series == ['runs_total{outcome="ok"}']  # no user_id/thread_id dimension
    assert all("u1" not in s and "t1" not in s for s in series)


def test_cardinality_is_capped():
    m = InMemoryMetrics()
    for i in range(1000):
        m.incr("wide", 1, kind=f"k{i}")
    # capped well below 1000 distinct series
    assert len(m.snapshot()["counters"]) <= 256


def test_render_text_is_prometheus_shaped():
    m = InMemoryMetrics()
    m.incr("http_requests_total", 1, status_group="2xx")
    m.observe("http_request_duration_ms", 5)
    text = m.render_text()
    assert 'http_requests_total{status_group="2xx"} 1.0' in text
    assert "http_request_duration_ms_count" in text
    assert "http_request_duration_ms_sum" in text
