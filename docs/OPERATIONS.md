# Operations

How to observe and operate Runner.ai in production. All operational features are
opt-in with safe defaults (see `.env.example`).

## Health

| Endpoint         | Meaning                          | Use for                    |
| ---------------- | -------------------------------- | -------------------------- |
| `GET /health`    | mongo summary (back-compat)      | quick manual check         |
| `GET /health/live`  | process is serving            | liveness probe             |
| `GET /health/ready` | Mongo/Redis/Qdrant/MinIO up   | readiness probe (gate LB)  |

Readiness reports each dependency as `"ok"` / `"unavailable"` — never an error
message, stack trace, or connection string. It makes **no paid LLM calls**.

## Metrics

Set `METRICS_ENABLED=true` to expose `GET /metrics` (Prometheus text format).
`METRICS_BACKEND=prometheus` uses `prometheus_client` if installed, else falls
back to the built-in in-memory sink.

Tracked (low-cardinality only — never user/thread/run ids, prompts, or args):

- **HTTP**: `http_requests_total{method,status_group}`,
  `http_request_duration_ms` (count/sum), `http_active_requests`,
  `http_rate_limited_total{route}`.
- **Runtime / tools / streaming / provider**: emitted through the injectable
  `MetricsSink` boundary (`app/observability/metrics.py`); wire additional
  counters by calling `get_metrics().incr(...)` at the relevant seam.

The sink **drops forbidden label keys** (`user_id`, `thread_id`, `run_id`,
`prompt`, …) and caps distinct label-sets per metric, so metrics cannot leak
identifiers or explode cardinality.

## Logs

Structured JSON on stdout (`app/logging_config.py`). Every line carries
`timestamp`, `level`, `logger`, `message`, and the request `request_id`
(correlation id). Request lifecycle lines: `request.completed` /
`request.failed` with `method`, `path`, `status_code`, `duration_ms`.

**Never logged** (by default): prompts, document contents, API keys, auth
headers, MCP secrets, environment variables, raw provider payloads.
`LOG_SENSITIVE=true` is a local-debug-only opt-in and must stay off in prod.

Ship stdout to your log system (Loki/CloudWatch/…); filter by `request_id` to
trace one request end-to-end.

## Request correlation

Send `X-Request-ID` (configurable via `CORRELATION_ID_HEADER`) to correlate a
client request with server logs; the value is honored only when safe
(8–128 chars, `[A-Za-z0-9._-]`), otherwise a fresh id is generated. It is echoed
on the response and is independent of the runtime `run_id` (a client cannot
invent a privileged runtime id).

## Rate limiting

`RATE_LIMIT_ENABLED=true` enforces per-route budgets (per authenticated user, or
client host as a fallback):

| Route                | Env                          | Default |
| -------------------- | ---------------------------- | ------- |
| `POST /agent/run`        | `RATE_LIMIT_RUN_PER_MINUTE`    | 30 |
| `POST /agent/run/stream` | `RATE_LIMIT_STREAM_PER_MINUTE` | 10 |
| `POST /agent/resume`     | `RATE_LIMIT_RESUME_PER_MINUTE` | 60 |

Use `RATE_LIMIT_BACKEND=redis` in production (multi-process correct). Over-limit
requests get `429` + `Retry-After`. The limiter **fails open** on a backend
error (availability over strictness at the edge).

## SSE streaming

`POST /agent/run/stream` sends heartbeat comments every `SSE_HEARTBEAT_SECONDS`
of idle time (set `0` to disable). On client disconnect the server cancels the
background runtime/provider work — no orphaned tasks, and no `runtime_completed`
after a disconnect. Keep reverse-proxy buffering **off** for this route (the
provided nginx config does).

## Scaling notes

- Backend is stateless except for the checkpoint store — run N replicas behind a
  load balancer with `AGENT_CHECKPOINT_BACKEND=mongo` and `RATE_LIMIT_BACKEND=redis`.
- The worker scales independently (document ingestion).
- In-memory metrics/rate-limit are per-process; use Redis (rate limit) and a
  scrape-per-replica or a push gateway (metrics) across replicas.
