# Architecture Walkthrough

How a request flows through Runner.ai V2, why each stage exists, and its main
trade-off. V2 is an autonomous **planner–executor** layer built additively on the
deterministic V1.5 RAG platform (FastAPI · MongoDB · Redis · Qdrant · MinIO).

## Deployment topology

```
Internet ──443──▶ Caddy (TLS, only public service)
                    ├─▶ frontend (nginx SPA)
                    └─▶ backend (FastAPI / uvicorn)
                          ├─ MongoDB  (durable checkpoints + app data)
                          ├─ Redis    (job queue + distributed rate limit)
                          ├─ Qdrant   (vector index)
                          └─ MinIO    (uploaded documents)
                        worker (async document ingestion)
```

One VM, Docker Compose, persistent named volumes. Infra is internal-only; Caddy
terminates HTTPS and is the single public surface. **Trade-off:** one VM is
simple and cheap but is a single point of failure — horizontal scale needs an
external MongoDB/Redis/Qdrant and multiple backend replicas behind the proxy.

## Request lifecycle

### 1. Edge & transport
Caddy forwards `/agent/*` to the backend with `flush_interval -1` so SSE tokens
are not buffered. FastAPI middleware (Phase 42A) assigns/validates a
**correlation id** (`X-Request-ID`), records HTTP metrics, enforces a body-size
limit, applies security headers, and (optionally) rate-limits per route.
*Why:* every request is traceable and bounded before any work starts.
*Trade-off:* middleware runs on every request; kept allocation-light and config-free.

### 2. Deterministic routing (Behavior Gate)
`BehaviorGate.classify` decides **path only** — DIRECT vs PLANNER — using keyword/
heuristic rules, *before* any LLM call. *Why:* most requests don't need a planner;
deterministic routing is free, instant, and testable, and it bounds cost/latency.
*Trade-off:* heuristics can misroute a novel phrasing; the planner path is the
safety net, and rules are cheap to extend.

### 3. Context construction (Context Engine)
Builds a `RunContext`: conversation/thread summary, long-term memory, and — via
Qdrant — the most relevant document chunks, under an explicit **context budget**.
*Why:* grounded answers need curated evidence, not the whole corpus; the budget
caps token cost. *Trade-off:* retrieval can miss evidence; the evaluator/repair
loop can request more context.

### 3b. Thread/document scoping + connector eligibility (Phase 43)
Before planning, the request is scoped to **one user's** conversation and
documents. Auth supplies `user_id` (never client-asserted); the thread is
validated as owned; a deterministic **interpreter** classifies intent and scope;
a **resolver** maps any document reference to owned `document_id`s (client
`selected_document_ids` are *hints*, revalidated against the thread's Mongo
document set). An early **Scope Gate** pauses `WAITING_FOR_USER` with a safe
candidate list when a document reference is ambiguous/unauthorized; otherwise it
attaches labelled document-chunk evidence. Retrieval filters Qdrant by `user_id`
plus the validated document-id set. In parallel, **connector eligibility** filters
the capability catalog so the planner never sees a tool whose connector is
missing/unhealthy or lacks the required scopes. *Why:* ownership and eligibility
are decided deterministically and early, so the planner only ever operates over
data and tools the user actually owns. *Trade-off:* an extra pre-planning stage,
in exchange for hard scope boundaries and no cross-thread/user leakage. See
[`THREAD_DOCUMENT_MODEL.md`](./THREAD_DOCUMENT_MODEL.md) and
[`CONNECTORS.md`](./CONNECTORS.md).

### 4. Capability retrieval (Unified Capability Registry)
Tools are **capabilities** from mounted *sources* (internal adapters, optional
MCP servers, future sources) unified into one registry. A hybrid retriever
(keyword + optional embedding rerank) selects the top-k `ToolSpec`s. *Why:* the
planner sees one catalog and never knows a capability's origin — internal vs MCP
is a composition detail. *Trade-off:* a uniform interface constrains
capability-specific features; the `ToolKind`-routed execution bridge recovers
per-source behavior.

### 5. Planning (Planner Runtime)
On the PLANNER path, the planner provider produces an `ExecutionPlan` (bounded
task list) over the retrieved capabilities. Deterministic by default; a real
LLM planner is swapped in by composition (`AGENT_USE_REAL_LLM`). *Why:* separating
*what to do* (plan) from *doing it* (execute) makes runs inspectable and
bounded. *Trade-off:* a plan-then-execute split can be less flexible than free-
form tool-calling, but it is far more controllable and debuggable.

### 6. Execution (Execution Bridge)
Each task runs through a by-kind executor that calls the capability adapter
(internal V1.5 adapter, or an MCP tool via the transport) and normalizes the
result into an `AdapterResult` with **evidence**. *Why:* one uniform result type
regardless of source; MCP is an adapter boundary, not a second runtime.
*Trade-off:* normalization loses some source-specific richness in exchange for a
single downstream contract.

### 7. Answer generation + streaming
The Final Context Builder assembles a `FinalPrompt` from evidence; the provider
generates the answer. Over `/agent/run/stream` the provider **streams tokens**
(Phase 38) that surface immediately to the UI as `answer_chunk` events. *Why:*
tool outputs are evidence, **not** the final answer — the model synthesizes a
grounded response. *Trade-off:* a synthesis step adds latency vs. echoing a tool
result, but yields grounded, cited answers.

### 8. Evaluation & repair (bounded)
If an evaluator is present, the draft answer is judged; a failing verdict maps to
a **repair action**. Only *local* regenerations run, bounded by
`max_repair_rounds`. Deferred actions (retrieve-more, replan, **HITL**) are
recorded and surfaced as runtime outcomes. *Why:* self-correction without
unbounded reflection loops. *Trade-off:* bounded repair may return a
"completed-with-warning" instead of a perfect answer — deliberately, to guarantee
termination.

### 9. Runtime outcomes & HITL
The run ends in one terminal `RuntimeOutcome`: `completed`,
`completed_with_warning`, `failed`, or a **waiting** state
(`waiting_for_user`, `waiting_for_approval`, `waiting_for_context`,
`waiting_for_replan`). Waiting outcomes persist a **checkpoint** and return a
`checkpoint_id`. *Why:* an autonomous agent must be able to stop and ask a human.
*Trade-off:* checkpoint storage + resume complexity, in exchange for safe
human-in-the-loop control.

### 10. Checkpoint & resume
Waiting runs are written to a `CheckpointStore` (in-memory in dev, **Mongo** in
prod). `POST /agent/resume` maps the caller's resolution
(clarification/approval/rejection) to a `ResumeResolution`, folds it into a fresh
`FinalPrompt`, and continues the **same** `run_id` — no new run, no rebuilt
context. *Why:* resume must be faithful to the paused state. *Trade-off:* durable
checkpoints add Mongo writes; they are forward-only (a restore may expire old
waiting runs).

### 11. SSE streaming & disconnect safety
The streaming endpoint emits heartbeat comments on idle and **cancels the
background run when the client disconnects** (Phase 42A), so no orphaned work or
`runtime_completed` after disconnect. *Why:* long-lived streams must not leak
tasks. *Trade-off:* cancellation logic in the streamer, for resource safety.

### 12. MCP lifecycle
When enabled, the composition root connects to **trusted** MCP servers
(stdio/streamable-HTTP JSON-RPC), discovers tools, registers them as capabilities,
and owns the connection lifecycle (pooling, reconnect, health). The runtime stays
transport-agnostic. *Why:* extend capabilities without embedding a vendor SDK in
the runtime. *Trade-off:* transport/lifecycle code, isolated behind a Protocol so
the runtime never sees it.

### 13. Frontend state machine
The SPA is a thin transport + presentation layer: a pure reducer maps
`RuntimeEvent`s to an explicit run state (`idle → streaming →
waiting/completed/failed`). It renders only **safe** metadata and drives
`/agent/resume`. *Why:* no business logic in React — the runtime owns behavior.
*Trade-off:* the UI can only show what the backend curates (by design — internals
never leak).

### 14. Observability, rate limiting, security
Injectable `MetricsSink` (NoOp default; in-memory or isolated Prometheus) with a
**label guard** dropping high-cardinality/sensitive keys; per-route rate limiting
(in-memory or **Redis** for multi-process); validated correlation ids; safe
health/readiness (no leak); no prompts/secrets logged by default. *Why:* run it
in production without leaking data or blowing up metric cardinality.
*Trade-off:* opt-in features default off to keep dev/tests byte-identical.

## The one-way dependency rule

`app.agent.*` depends on `app.services.*` (V1.5), **never** the reverse, and is
**config-free at import** (settings/DB imported lazily inside methods). *Why:* the
agent layer is unit-testable with only pydantic + pytest, and V1.5 stays intact.
This constraint shaped every phase and is the reason the test suite runs without
Mongo/Redis/Qdrant or credentials.
