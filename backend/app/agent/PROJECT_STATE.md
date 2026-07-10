# Runner.ai V2 Current State

> **Purpose.** This file is the repository-level architecture handoff. It exists so
> the **repository — not any chat conversation — is the primary source of truth**.
> If you are a new contributor (human or a fresh Claude session), read this first.
> It is documentation only; it introduces no runtime behavior.
>
> Companion document: [`ARCHITECTURE.md`](./ARCHITECTURE.md) (the living
> architecture + phase-compatibility report). Where the two agree, both are
> authoritative; where a detail here is more recent, this file reflects the
> current commit.

---

## Project Vision

### What Runner.ai is
Runner.ai is a **RAG + autonomous-agent platform**. It has two layers:

- **V1.5 — the deterministic RAG platform** (`backend/app/services`,
  `backend/app/routes/{chat,documents,jobs,memory,health}.py`). A production
  FastAPI backend over MongoDB, Redis, Qdrant, and MinIO that ingests documents,
  performs semantic retrieval, stores per-user memory/preferences, and answers
  questions via a provider-agnostic LLM client (Anthropic / OpenRouter / stub).
- **V2 — the autonomous execution layer** (`backend/app/agent/`). A planner /
  executor agent runtime layered *on top of* V1.5. It decides whether a request
  needs a single tool call or a multi-step plan, retrieves the right
  capabilities, executes them through adapters that call back into V1.5, builds a
  grounded final prompt, generates (and optionally streams) an answer, evaluates
  it, repairs it within bounds, and exposes the whole thing over an HTTP + SSE
  API with durable pause/resume.

### What problems it solves
- **From "chatbot" to "operator."** V1.5 answers questions; V2 *acts* — it plans
  and runs capabilities (search documents, read/write memory, enqueue jobs) to
  satisfy a request.
- **Grounded, auditable answers.** Every answer is built from an explicit
  `FinalPrompt` (context + evidence + tool outputs + citations), then evaluated
  before it is returned.
- **Safe autonomy.** A behavior gate keeps simple requests on a cheap direct
  path; a planner handles multi-step work but **never executes tools itself**; a
  policy engine annotates risk; repair is bounded; provider failures degrade to
  safe outcomes instead of leaking vendor errors.
- **Durability.** Long-running or human-in-the-loop runs checkpoint their state
  and resume later (in-memory in dev, MongoDB in production).
- **Streaming UX.** Answers stream token-by-token as the provider produces them.

### High-level architecture
```
                    ┌──────────────────────────────────────────────┐
   HTTP / SSE  ───▶ │            V2 Agent Runtime (app.agent)         │
  /agent/run        │  context → gate → retrieval → plan/direct →     │
  /agent/resume     │  execute → final prompt → provider → evaluate → │
  /agent/run/stream │  repair → outcome                               │
                    └───────────────┬──────────────────────────────┘
                                    │  (one-way dependency: agent → services)
                    ┌───────────────▼──────────────────────────────┐
                    │        V1.5 Platform (app.services)            │
                    │  llm_client · retrieval · memory · documents   │
                    └───────────────┬──────────────────────────────┘
                                    │
                 MongoDB · Redis · Qdrant · MinIO · LLM vendors
```

**The one rule that governs the whole codebase:** the dependency is strictly
one-way. `app.agent` may import from `app.services`; `app.services` must never
import from `app.agent`. Inside the agent, V1.5 is reached only through **lazy
imports inside methods** and **thin adapters**, so the agent package stays
config-free and unit-testable without a database or credentials.

---

## Current Branch

| Field | Value |
|---|---|
| **Branch** | `v2-autonomous-platform` |
| **Latest commit** | `ed621c0 — V2 Phase 38: True token streaming` |
| **Test count** | **526 passing** (`481` under `tests/agent`, `45` under `tests/api`) |
| **Python** | 3.11 (developed on 3.11.15) |
| **Test command** | `cd backend && python -m pytest` |

---

## Completed Phases

Phases below are the **V2 agent** series (the earlier V1.5 platform work is
tracked separately as "Phase 0–6" — ingestion, Qdrant retrieval, real LLM
client, memory, chat streaming, deployment). Each V2 phase is one commit on this
branch and ships with tests.

> **Cross-cutting decisions that hold for _every_ phase:** deterministic and
> side-effect-free by default; no `app.config` / `MONGO_URL` needed at import;
> V1.5 reached only via lazy imports inside methods; tests inject fakes and drive
> async code with `asyncio.run`; no vendor SDK imported anywhere in `app.agent`.

### Phase 1 — ToolSpec model + Tool Registry
`ToolSpec` metadata model (`ToolKind`, `RiskLevel`, `SideEffectType`,
`LatencyClass`) plus a deterministic `ToolRegistry` and 10 **metadata-only**
internal specs mapping V1.5 capabilities. **Decision:** capabilities are
described by data, not code — everything downstream (retrieval, policy,
validation) reads the spec, so adding a capability is a registry entry.

### Phase 2 — Capability Retrieval Engine
Deterministic weighted keyword scoring behind a `CapabilityRetriever` ABC, with
a `KeywordCapabilityRetriever` (filters + evidence-priority fallback).
**Decision:** retrieval is an interface from day one; the deterministic keyword
scorer is the offline default, embeddings arrive later without changing callers.

### Phase 3 — Plan / DAG models
`Plan`, `PlanStep`, `ArgBinding`, `PlanStepType`, `FinalResponseMode` with DAG
validation and cycle detection. **Decision:** the planner's output is a typed,
validated DAG — not free-form text — so it can be checked, optimized, and
executed deterministically.

### Phase 4 — Structural Plan Validator
`StructuralPlanValidator` checks capability existence/enabled state, arg schema,
and dependency/binding integrity, producing a severity-ranked report.
**Decision:** a plan is validated *structurally* before anything runs; invalid
plans never reach the executor.

### Phase 5 — Policy Engine (annotate-only)
`PolicyEngine` yields `ALLOW / REQUIRE_APPROVAL / BLOCK` per step with
most-restrictive-wins. **Decision:** policy *annotates*, it does not execute —
risk classification is separated from enforcement so the same report drives HITL,
logging, and UI.

### Phase 6 — Optimizer
`PlanOptimizer` builds DAG-level execution groups, detects duplicates, and
attaches policy annotations (`OptimizedPlan`). **Decision:** parallelizable work
is grouped at plan time, so the executor just walks groups.

### Phase 7 — Executor + Shared Execution State
`ExecutionState` blackboard + `PlanExecutor` (sequential group execution,
binding resolution, policy-aware skip/block/await) over a `ToolRunner` ABC.
**Decision:** execution reads/writes a single shared state object; tool invocation
is abstracted behind `ToolRunner`.

### Phase 8 — Tool Adapter interface + Adapter Registry
`ToolAdapter` ABC (`execute(tool, args) -> dict`) and an `AdapterRegistry` keyed
by `ToolKind`. **Decision:** the boundary between "agent decides" and "system
does" is a small adapter interface.

### Phase 9 — AdapterToolRunner
`AdapterToolRunner` bridges `PlanExecutor` to concrete adapters. **Decision:** the
executor stays adapter-agnostic; wiring happens at the edge.

### Phase 10A — RunContext foundation
`RunContext` — the single mutable object carried through the whole run
(`run_id`, `user_id`, `thread_id`, working context, behavior profile, selected
capabilities, tool outputs, evidence, `metadata`). **Decision:** one context
object threads the pipeline; stages append to it, never rebuild it.

### Phase 10B — Context providers + Context Engine
`ContextEngine.build(...)` assembles the working context from pluggable
providers (thread summary, recent messages, …). **Decision:** context assembly is
provider-based and injectable, so tests supply fakes and production supplies
V1.5-backed providers.

### Phase 11A — Hybrid Context Prioritizer
Deterministic tier that ranks/prunes working-context items. **Decision:**
prioritization is deterministic first; smarter tiers slot in behind the same
interface.

### Phase 11B — Token Budget Manager
Enforces a token budget over prioritized context. **Decision:** budgeting is an
explicit stage with deterministic, char/token-based accounting.

### Phase 12 — Behavior Gate
`BehaviorGate.decide(run_context)` routes DIRECT vs PLANNER deterministically and
attaches a `BehaviorProfile`. **Decision:** the cheap path is the default; the
planner is only engaged when the request needs multiple steps.

### Phase 13 — Execution Bridge
`AdapterResult` + the internal-adapter foundation that calls back into V1.5.
**Decision:** adapter results are a typed envelope (`ok`/error, output, evidence)
so downstream stages read a uniform shape.

### Phase 14 — Direct Runtime
`DirectRuntime.run(run_context)` — the non-planning path: retrieve one
capability, execute it, record evidence/outputs. **Decision:** the majority of
requests are handled here without a planner LLM call.

### Phase 15 — Planner Runtime + RunContext-aware retrieval
`PlannerRuntime.run(run_context, plan)` orchestrates the direct runtime per task;
capability retrieval becomes RunContext-aware. **Decision:** the planner
*orchestrates* the direct runtime — it composes execution, it does not itself
call tools.

### Phase 16 — Final Context Builder
`FinalContextBuilder.build(run_context) -> FinalPrompt` — a provider-agnostic
prompt of system/context/evidence/tool sections + citations. **Decision:** the
prompt is a typed artifact, decoupled from any vendor wire format.

### Phase 17 — Final LLM Provider Boundary
`FinalAnswerProvider` protocol + `FinalAnswer` model + `render_final_prompt` +
`DeterministicFinalProvider`. **Decision:** answer generation is an interface;
the deterministic provider is the offline default and the test oracle.

### Phase 18 — Runtime Orchestrator
`AgentOrchestrator.run()` chains every stage end-to-end in memory. **Decision:**
the orchestrator owns *sequencing only* — every dependency is injected; it holds
no construction, config, DB, or LLM logic.

### Phase 19 — Runtime Factory
`build_default_runtime(...)` — the composition root that wires the orchestrator
from real/fake parts. **Decision:** construction lives in one factory, separate
from the orchestrator.

### Phase 20 — Answer Evaluation & Repair Engine
Deterministic evaluation checks + `EvaluationReport` / `RepairDecision`.
**Decision:** answers are graded by explicit, deterministic checks before being
returned.

### Phase 21 — Repair Runtime
`RepairRuntime.repair(...)` maps a failed evaluation to a `RepairAction`
(regenerate / defer / partial / fail). **Decision:** repair is a pure decision
function; execution of the decision belongs to the orchestrator.

### Phase 22 — Evaluation + Repair integration
The orchestrator evaluates the draft and runs **bounded local regeneration**
(`max_repair_rounds`); deferred repairs are recorded, not executed. **Decision:**
only *local regeneration* repairs run inline; retrieve-more / replan / HITL are
surfaced, never faked.

### Phase 23 — RuntimeOutcome terminal state
`RuntimeOutcome` (`COMPLETED`, `COMPLETED_WITH_WARNING`, `FAILED`,
`WAITING_FOR_*`) derived onto `AgentRunResult`. **Decision:** every run ends in
one typed terminal state — the contract for API/UI/workers/HITL.

### Phase 24 — Checkpoint Store (in-memory)
`CheckpointStore` Protocol (`save/load/mark_resumed/cancel`) +
`InMemoryCheckpointStore`. **Decision:** persistence is a **synchronous**
Protocol; backends are swappable behind it.

### Phase 25 — Resume Runtime
Rehydrate a persisted `RunContext` and prepare it for continuation (data layer
only). **Decision:** resume rebuilds the context object faithfully; it never
mints a new `run_id`.

### Phase 26 — Resume Integration
`AgentOrchestrator.continue_run(run_context)` — `WAITING_FOR_USER/APPROVAL` fold
the resolution into a fresh `FinalPrompt` and regenerate; `WAITING_FOR_CONTEXT/
REPLAN` are surfaced as deferred. **Decision:** continuation reuses the normal
generate→evaluate→repair tail; it does not re-run context building or auth.

### Phase 27 — Resume Coordinator
`ResumeCoordinator` (start/resume) ties orchestrator + checkpoint store into a
pause/resume loop. **Decision:** the coordinator owns the save-on-pause /
load-on-resume choreography so routes don't.

### Phase 28 — Production Hybrid Retrieval pipeline
`HybridPipeline` (embedding retriever + reranker + keyword) under
`app/agent/retriever/`. **Decision:** production retrieval is a composable
pipeline; each stage is injectable and independently testable.

### Phase 29 — Integrate hybrid retrieval into the runtime
`HybridCapabilityRetriever` wraps the keyword retriever with the hybrid pipeline
and is wired through the factory. **Decision:** the runtime consumes retrieval
through one `retrieve_for_run_context` seam regardless of backend.

### Phase 30 — Agent Run API
`POST /agent/run` → authenticate → shared runtime → `AsyncResumeCoordinator.start`
→ API-safe `AgentRunResponse`. **Decision:** routes are **transport-only**;
business logic stays in the runtime; the orchestrator is a process singleton, not
per-request.

### Phase 31 — Agent Resume API
`POST /agent/resume` maps a caller resolution to a domain `ResumeResolution` and
drives the coordinator over the same store; unknown checkpoint → 404, conflict →
409. **Decision:** resume shares the exact orchestrator/store/coordinator
singletons as `/run`.

### Phase 32 — Runtime Streaming (internal)
`RuntimeStreamer.run_stream()` emits an internal `RuntimeEvent` stream alongside
the unchanged `run()`. **Decision:** streaming is an additive wrapper; no
orchestration is duplicated.

### Phase 33 — SSE streaming endpoint
`POST /agent/run/stream` serializes `RuntimeEvent`s as `text/event-stream`
(`event: <type>\ndata: <json>`), terminating with `runtime_failed` on error.
**Decision:** the route is pure transport; the streamer owns event
ordering/generation.

### Phase 34 — Mongo-backed CheckpointStore
`MongoCheckpointStore` behind the same Protocol, with atomic `mark_resumed`
(`find_one_and_update`), typed not-found/conflict errors, and indexes.
**Decision:** durability is a drop-in backend; call sites are unchanged.

### Phase 35 — Production Checkpoint Composition + Async-Safe boundary
A composition root selects the backend at startup; `AsyncCheckpointStoreAdapter`
offloads synchronous store I/O via `anyio.to_thread`; conflicts surface as 409.
**Decision:** the store stays synchronous; an async adapter keeps the event loop
unblocked — no async creep into the Protocol.

### Phase 36 — Real LLM Provider Integration + Planner Boundary
`V15FinalAnswerProvider` and `V15PlannerProvider` reuse the V1.5 LLM service via
lazy imports; strict structured-planner-output validation; factory
`use_real_llm` switch. **Decision:** real providers are adapters over V1.5 —
still **no vendor SDK in `app.agent`**; the planner runs only on the PLANNER
path.

### Phase 37 — Production LLM Composition + Graceful Provider Failure
`agent_use_real_llm` setting; only **domain** provider errors
(`FinalProviderError`, `ProviderUnavailableError`, `Planner*Error`) are caught and
converted to safe `RuntimeOutcome`s with `failure_stage`/`retryable` metadata;
programming bugs still propagate; no vendor text leaks. **Decision:** provider
failures are first-class safe outcomes, not 500s.

### Phase 38 — True Token Streaming
The provider boundary gains `generate_stream` + `build_final_answer` additively;
`AgentOrchestrator.run(stream_sink=...)` emits pipeline events (incl.
`answer_chunk` per provider chunk) **live**; `RuntimeStreamer` drives the run via
a queue and emits the terminal event; non-streaming `/agent/run` is byte-identical.
**Decision:** streaming happens *as the provider produces tokens* — never
reconstructed after the fact; evaluation runs only on the fully-assembled answer;
regeneration repair produces a second bounded stream round.

---

## Runtime Pipeline

```
   HTTP request  (POST /agent/run | /agent/run/stream | /agent/resume)
        │
        ▼
  ┌─────────────────┐
  │  Context Engine  │  build RunContext from providers (thread summary, msgs…)
  └────────┬────────┘
           ▼
  ┌─────────────────┐
  │  Behavior Gate   │  deterministic DIRECT vs PLANNER decision
  └────────┬────────┘
           ▼
  ┌─────────────────┐
  │ Hybrid Retrieval │  embeddings + rerank + keyword → capability matches
  └────────┬────────┘
           ▼
     ┌─────┴──────────────────────────┐
     ▼ (PLANNER)                       ▼ (DIRECT)
  ┌────────────────┐            ┌────────────────┐
  │ Planner Runtime │            │ Direct Runtime │
  │  (LLM → Plan)   │            │  (1 capability)│
  └───────┬────────┘            └───────┬────────┘
          │  orchestrates per task       │
          ▼                              ▼
  ┌──────────────────────────────────────────────┐
  │   Execution (AdapterToolRunner → adapters →    │
  │              V1.5 services)                    │
  └────────┬─────────────────────────────────────┘
           ▼
  ┌─────────────────┐
  │  Final Context   │  FinalContextBuilder → FinalPrompt
  │     Builder      │  (system + context + evidence + tools + citations)
  └────────┬────────┘
           ▼
  ┌───────────────────────┐
  │  FinalAnswerProvider    │  generate()  OR  generate_stream() ──▶ answer_chunk…
  │ (Deterministic | V15)   │  → build_final_answer() → FinalAnswer
  └────────┬──────────────┘
           ▼
  ┌─────────────────┐
  │   Evaluation     │  deterministic checks on the COMPLETE answer only
  └────────┬────────┘
           ▼
  ┌─────────────────┐
  │     Repair       │  bounded local regeneration (max_repair_rounds)
  │                  │  → second bounded stream round when streaming
  └────────┬────────┘
           ▼
  ┌─────────────────┐
  │  RuntimeOutcome  │  COMPLETED | COMPLETED_WITH_WARNING | FAILED | WAITING_*
  └────────┬────────┘
           ▼
  ┌─────────────────────────────┐
  │  Streaming / API transport   │  AgentRunResponse (JSON)  |  SSE event stream
  └─────────────────────────────┘
                     │
              (WAITING_* → checkpoint saved → later /agent/resume)
```

**Provider-failure short-circuit:** a domain provider error at the planner or
final-answer stage skips evaluation/repair and returns a safe `FAILED`
(or `WAITING_FOR_USER` for a planner *validation* error) outcome. In a stream it
terminates with `runtime_failed` and no `runtime_completed`.

---

## Implemented Components

Locations are under `backend/app/agent/`.

### Context Engine — `context/engine.py`, `context/providers.py`
- **Responsibility:** assemble the working context for a request from injectable
  providers; create the `RunContext`.
- **Dependencies:** context providers (fakes in tests; V1.5-backed in prod).
- **Outputs:** a populated `RunContext` (working context + metadata).

### Context Prioritizer — `context/prioritizer.py`, `context/budget.py`
- **Responsibility:** deterministically rank/prune working-context items and
  enforce a token budget.
- **Dependencies:** none beyond the context items (deterministic).
- **Outputs:** a prioritized, budget-bounded working context.

### Hybrid Retrieval Pipeline — `retriever/hybrid_pipeline.py`, `embedding_retriever.py`, `reranker.py`, `context_retriever.py`
- **Responsibility:** production retrieval — embed the query, retrieve
  candidates, rerank, blend with keyword scoring.
- **Dependencies:** an embedding function + reranker (injected; V1.5/Qdrant in
  prod).
- **Outputs:** ranked retrieval results feeding capability retrieval.

### Capability Retrieval — `capabilities/*`, `retriever/capability_retriever.py`
- **Responsibility:** select the capabilities (`ToolSpec`s) relevant to the run.
- **Dependencies:** `ToolRegistry`, scoring, and (in prod) the hybrid pipeline
  via `HybridCapabilityRetriever`.
- **Outputs:** `CapabilityRetrievalResponse` (`CapabilityMatch` list).

### Planner Runtime — `runtime/planner_runtime.py`
- **Responsibility:** on the PLANNER path, turn a request into an
  `ExecutionPlan` (via a `PlannerProvider`) and orchestrate the direct runtime
  per task.
- **Dependencies:** `PlannerProvider`, `DirectRuntime`, capability retriever.
- **Outputs:** an updated `RunContext` with per-task tool outputs; **never calls
  tools directly**.

### Direct Runtime — `runtime/direct_runtime.py`
- **Responsibility:** the non-planning path — retrieve one capability, execute
  it, record outputs/evidence.
- **Dependencies:** capability retriever, an executor.
- **Outputs:** updated `RunContext` (tool outputs + evidence).

### Orchestrator — `runtime/orchestrator.py`
- **Responsibility:** sequence the entire pipeline (context → gate → retrieval →
  plan/direct → final prompt → provider → evaluate → repair → outcome);
  `continue_run` for resume; optional live `stream_sink`.
- **Dependencies:** every stage is injected (context engine, gate, runtimes,
  final builder, providers, evaluator, repair runtime).
- **Outputs:** `AgentRunResult` (answer, final prompt, run context, outcome,
  safe metadata).

### Evaluation Runtime — `evaluation/engine.py`, `evaluation/models.py`
- **Responsibility:** grade the **complete** answer with deterministic checks.
- **Dependencies:** the `FinalPrompt` + `FinalAnswer` (+ `RunContext`).
- **Outputs:** `EvaluationReport` (passed, score, `RepairDecision`).

### Repair Runtime — `repair/runtime.py`, `repair/models.py`
- **Responsibility:** map a failed evaluation to a bounded `RepairAction`.
- **Dependencies:** the report + current prompt/answer.
- **Outputs:** a `RepairResult` (possibly an `updated_final_prompt` for
  regeneration).

### Runtime Outcomes — `runtime/outcome.py`
- **Responsibility:** derive the terminal `RuntimeOutcome` and pending
  action/reason.
- **Dependencies:** evaluation report + terminal repair.
- **Outputs:** `RuntimeOutcome` + pending fields.

### Checkpoint Store — `checkpoint/store.py`, `mongo_store.py`, `composition.py`, `models.py`
- **Responsibility:** persist/load run state behind a **synchronous** Protocol
  (`save/load/mark_resumed/cancel`); in-memory and Mongo backends;
  `select_checkpoint_store` composition.
- **Dependencies:** none (in-memory) / a pymongo collection (Mongo).
- **Outputs:** persisted checkpoints; typed not-found/conflict errors.

### Resume Runtime — `checkpoint/rehydrate.py`, `checkpoint/resume.py`
- **Responsibility:** rehydrate a persisted `RunContext` and model the
  `ResumeResolution`.
- **Dependencies:** the checkpoint payload.
- **Outputs:** a faithfully rebuilt `RunContext` ready for `continue_run`.

### Resume Coordinator — `runtime/resume_coordinator.py`
- **Responsibility:** the start/resume choreography over orchestrator + store;
  `AsyncResumeCoordinator` offloads sync store I/O off the event loop
  (`anyio.to_thread`).
- **Dependencies:** orchestrator + checkpoint store.
- **Outputs:** `ResumeCoordinatorResult` (result + checkpoint id).

### Runtime Streamer — `runtime/streaming.py`, `runtime/events.py`
- **Responsibility:** run the orchestrator with a queue-backed `stream_sink` and
  yield `RuntimeEvent`s live; emit the terminal `runtime_completed`/`failed`.
- **Dependencies:** the orchestrator (nothing else).
- **Outputs:** an async iterator of `RuntimeEvent`.

### Provider Adapters — `llm/final_provider.py`, `llm/planner_provider.py`, `llm/provider_adapter.py`
- **Responsibility:** the LLM boundary — `FinalAnswerProvider` /
  `PlannerProvider` protocols; deterministic providers (offline default) and
  V1.5-backed real providers (lazy import, no vendor SDK); provider error
  taxonomy.
- **Dependencies:** none (deterministic) / lazily-resolved V1.5 `complete` /
  `stream` (real).
- **Outputs:** `FinalAnswer` / `ExecutionPlan`; streamed chunks via
  `generate_stream`.

---

## API Surface

All V2 endpoints are under the `/agent` router (`app/routes/agent.py`), which is
**transport-only**.

| Method & Path | Returns |
|---|---|
| `POST /agent/run` | `AgentRunResponse` (JSON). Completed runs carry the `answer`; `WAITING_*` runs carry a `checkpoint_id` + `pending_action`/`pending_reason` (no answer). Always includes API-safe `metadata` (behavior path, provider/model, evaluation flags, and — on failure — `failure_stage`/`error_code`/`retryable`). Never exposes the internal `RunContext` or `FinalPrompt`. |
| `POST /agent/resume` | `AgentRunResponse` for a paused run identified by `checkpoint_id` + a `resolution`. `404` if the checkpoint is unknown; `409` on conflict (already resumed/cancelled or a lost atomic claim). |
| `POST /agent/run/stream` | `text/event-stream` (SSE). One `RuntimeEvent` per frame (`event: <type>\ndata: <json>`): `runtime_started` → stage events → `answer_started`/`answer_chunk`…/`answer_completed` → evaluation/repair → terminal `runtime_completed` (or `runtime_failed`). No internal objects leak. |

**V1.5 endpoints still present** (unchanged by V2): `POST /chat/ask`,
`POST /chat/stream`, `POST /documents/upload`, `GET /documents/{id}`,
`GET /jobs/{id}`, `GET/POST /memory/*`, `GET /health`.

---

## Streaming

### Internal `RuntimeStreamer`
`RuntimeStreamer.run_stream(...)` is the internal engine. It creates a
queue-backed async `stream_sink`, runs `orchestrator.run(stream_sink=sink)` as a
background task, and yields each `RuntimeEvent` off the queue **as it is
produced**. It owns only the envelope: `runtime_started` up front and the single
terminal event (`runtime_completed` on success, `runtime_failed` on a raised
error or a provider-failure outcome). Everything between — context, retrieval,
planner, tools, `answer_*`, evaluation, repair — is emitted by the orchestrator
in true pipeline order.

### SSE
The `/agent/run/stream` route is a thin transport: it serializes each
`RuntimeEvent` to the SSE wire format and returns a `StreamingResponse`
(`text/event-stream`, `Cache-Control: no-cache`, `X-Accel-Buffering: no`). It
contains no streaming logic.

### True token streaming (Phase 38)
Answer chunks are emitted **as the provider yields them**, not reconstructed
after the answer already exists. The provider boundary supports
`generate_stream(final_prompt) -> AsyncIterator[str]`; the orchestrator's single
answer seam emits `answer_started`, one `answer_chunk` per provider chunk live,
assembles the complete draft, calls `build_final_answer`, and emits
`answer_completed`. **Evaluation never sees partial chunks** — only the assembled
answer. A regeneration repair produces a *second* bounded stream round. A
mid-stream failure terminates with `runtime_failed` and no `runtime_completed`.

### `/run` vs `/run/stream`
- **`/agent/run`** — non-streaming JSON. `stream_sink` is `None`, the emitter is
  a no-op, and the answer is produced via `generate()`. This path is
  **byte-identical** to its pre-streaming behavior.
- **`/agent/run/stream`** — same runtime, driven with a live `stream_sink`, so
  the same decisions/plan/retrieval happen but the answer streams token-by-token.

Both use the **same shared orchestrator singleton**; streaming adds no second
pipeline.

---

## Provider Architecture

### Deterministic providers
`DeterministicFinalProvider` and `DeterministicPlannerProvider` produce fixed,
grounded output with no randomness, clock, or network. They are the **offline
default** (config-free) and the **test oracle**. The deterministic final provider
streams its composed text in fixed-size chunks whose concatenation reproduces
`generate()` exactly — so streamed and non-streamed answers match byte-for-byte.

### Real V1.5 providers
`V15FinalAnswerProvider` and `V15PlannerProvider` are adapters over the existing
V1.5 LLM service. They **lazily import** `app.services.llm_client.complete` /
`stream` inside methods, wrap raw errors in the domain error taxonomy
(`FinalProviderError`, `ProviderUnavailableError`, `Planner*Error`), and never
leak vendor text. `generate_stream` reuses V1.5's streaming service and
gracefully falls back to `generate()` when streaming is unavailable. Selected via
`use_real_llm` / the `agent_use_real_llm` setting.

### Why providers are interfaces
- **No vendor lock-in in the runtime.** The orchestrator depends on a protocol,
  never on OpenAI/Anthropic/Gemini SDKs — those live outside `app.agent`.
- **Offline, credential-free tests.** The deterministic provider lets the entire
  pipeline run in unit tests without a network or keys.
- **Config-free imports.** Because real providers resolve V1.5 lazily, importing
  `app.agent.llm` needs no settings or database.
- **Swappability.** Deterministic ↔ real is a one-line factory switch; adding a
  new backend is a new adapter, not a runtime change.

---

## Checkpoint Architecture

### Memory store
`InMemoryCheckpointStore` — the config-free default used by dev and tests. Full
Protocol implementation (`save/load/mark_resumed/cancel`), no external
dependencies.

### Mongo store
`MongoCheckpointStore` — durable production backend behind the **same
synchronous Protocol**. `mark_resumed` is atomic (`find_one_and_update`), with
typed `CheckpointNotFoundError` / conflict errors and indexes on the checkpoint
collection. Selected at startup by `select_checkpoint_store("mongo", ...)`;
because the store is synchronous, the app owns a dedicated pymongo client and an
`AsyncCheckpointStoreAdapter` offloads its I/O off the event loop via
`anyio.to_thread`.

### Resume lifecycle
```
run() reaches a WAITING_* outcome
   → coordinator saves a checkpoint (state = active/pending)
   → API returns checkpoint_id + pending_action/pending_reason
        … time passes; a human or system resolves the pending action …
   → POST /agent/resume {checkpoint_id, resolution}
   → coordinator load()s + mark_resumed() (atomic claim; 409 if lost)
   → orchestrator.continue_run(rehydrated RunContext)
   → generate → evaluate → repair → new RuntimeOutcome
```
`WAITING_FOR_USER` / `WAITING_FOR_APPROVAL` fold the resolution into a fresh
`FinalPrompt` and regenerate; `WAITING_FOR_CONTEXT` / `WAITING_FOR_REPLAN` are
surfaced as deferred (never faked).

### Runtime outcomes
The terminal contract for every run (and the signal for whether to checkpoint):

| Outcome | Meaning |
|---|---|
| `COMPLETED` | Answer produced and passed evaluation. |
| `COMPLETED_WITH_WARNING` | Answer produced; evaluation flagged a non-fatal issue. |
| `FAILED` | Unrecoverable (e.g. a domain provider failure). Safe message only. |
| `WAITING_FOR_USER` | Needs user clarification (e.g. planner validation error). |
| `WAITING_FOR_APPROVAL` | A policy step requires approval before proceeding. |
| `WAITING_FOR_CONTEXT` | Needs more context/retrieval before continuing (deferred). |
| `WAITING_FOR_REPLAN` | Needs a re-plan before continuing (deferred). |

`WAITING_*` outcomes are checkpointed and resumable; terminal outcomes are not.

---

## Locked Architecture Decisions

These are **load-bearing**. Do not change them without an explicit, documented
reason — most of the codebase relies on them.

1. **Provider abstraction.** Answer/plan generation is always behind a protocol
   (`FinalAnswerProvider` / `PlannerProvider`). The runtime never depends on a
   concrete vendor.
2. **The planner never executes tools.** `PlannerRuntime` composes and
   orchestrates the `DirectRuntime`; only the executor/adapters invoke
   capabilities.
3. **Transport-only routes.** `app/routes/agent.py` authenticates, delegates, and
   serializes. No business logic, no persistence, no streaming logic in routes.
4. **Deterministic defaults.** The default runtime is fully deterministic and
   config-free, so the whole pipeline runs in unit tests without a DB or
   credentials.
5. **Hybrid retrieval pipeline.** Retrieval is a composable, injectable pipeline
   (embeddings + rerank + keyword) consumed through one `retrieve_for_run_context`
   seam.
6. **Evaluation only on the complete answer.** Never evaluate partial/streamed
   chunks — assemble the full draft first.
7. **Repair is bounded.** Local regeneration is capped by `max_repair_rounds`;
   non-local repairs (retrieve-more/replan/HITL) are surfaced, not executed.
8. **No vendor SDK in `app.agent`.** OpenAI/Anthropic/Gemini SDKs never appear in
   the agent package; V1.5's LLM client is the only bridge.
9. **Lazy imports of V1.5.** `app.services` (and `app.config`) are imported
   *inside methods*, never at module top level in the agent package.
10. **The runtime owns orchestration.** `AgentOrchestrator` owns sequencing;
    construction lives in the factory; routes/coordinator only drive it.
11. **Routes never own business logic.** (Corollary of 3 & 10.) The HTTP layer is
    a thin adapter over the runtime.
12. **One-way dependency.** `app.agent` → `app.services`, never the reverse.
13. **Synchronous checkpoint Protocol + async adapter.** The store stays sync;
    async-safety is added at the edge (`anyio.to_thread`), not by making the
    Protocol async.
14. **Provider failures are safe outcomes.** Only *domain* provider errors are
    caught and mapped to `RuntimeOutcome`s; programming bugs still propagate; no
    vendor detail leaks to the API.
15. **Shared, process-level singletons.** One orchestrator / coordinator / store
    per process — never rebuilt per request; `/run`, `/resume`, `/run/stream`
    share them.
16. **V1.5 is never modified by V2.** The agent layers on top; it does not rewrite
    platform services.

---

## Remaining Roadmap

| Phase | Milestone | Sketch |
|---|---|---|
| **Phase 39** | **MCP Integration** | Expose/consume capabilities over the Model Context Protocol so external MCP tools become first-class adapters — behind the same `ToolAdapter` / provider boundaries, no vendor SDK creep. |
| **Phase 40** | **Unified Tool Registry** | Merge internal `ToolSpec`s, V1.5 capabilities, and MCP tools into one registry + retrieval surface, so the planner/direct runtimes see a single capability namespace. |
| **Phase 41** | **Frontend + HITL** | A UI over the streaming API and the `WAITING_*` outcomes: render live token streams, surface pending approvals/clarifications, and drive `/agent/resume`. |
| **Phase 42** | **Production hardening** | Streaming transport hardening (keep-alive, client-disconnect cancellation), observability/metrics, rate limiting, load/soak testing, and deployment/runbook polish. |

---

## Test Status

- **Current:** **526 passing** (1 benign Starlette deprecation warning),
  `python -m pytest`, ~1–2s.
  - `tests/agent/` — **481** (unit tests for every runtime stage; config-free,
    fakes + `asyncio.run`).
  - `tests/api/` — **45** (FastAPI `TestClient` over the routers with injected
    fakes; no DB/LLM).

### Major test categories
- **Models & registries:** `test_tool_registry`, `test_plan_models`,
  `test_adapter_registry`, `test_run_context`.
- **Retrieval:** `test_capability_retrieval`, `test_hybrid_retrieval`,
  `test_hybrid_integration`.
- **Context:** `test_context_engine`, `test_context_prioritizer`,
  `test_budget_manager`, `test_final_context_builder`.
- **Routing & execution:** `test_behavior_gate`, `test_direct_runtime`,
  `test_planner_runtime`, `test_executor`, `test_execution_bridge`,
  `test_adapter_tool_runner`, `test_optimizer`, `test_policy_engine`,
  `test_structural_validator`.
- **Providers:** `test_final_provider`, `test_planner_provider`,
  `test_real_provider_adapter`.
- **Orchestration:** `test_orchestrator`, `test_orchestrator_evaluation`,
  `test_orchestrator_resume`, `test_runtime_factory`,
  `test_provider_failure_outcomes`.
- **Evaluation & repair:** `test_answer_evaluation`, `test_repair_runtime`,
  `test_runtime_outcome`.
- **Checkpoint & resume:** `test_checkpoint_store`, `test_mongo_checkpoint_store`,
  `test_async_checkpoint_store`, `test_resume_runtime`, `test_resume_coordinator`.
- **Streaming:** `test_runtime_streaming`.
- **API:** `test_agent_run`, `test_agent_resume`, `test_agent_stream`,
  `test_checkpoint_wiring`, `test_runtime_provider_wiring`,
  `test_provider_failure_api`.

---

## Repository Navigation

Where things live (all paths relative to `backend/`):

```
app/
  agent/                     ← V2 autonomous execution layer (this document's subject)
    ARCHITECTURE.md          ← living architecture + phase-compatibility report
    PROJECT_STATE.md         ← THIS FILE (start here)
    capabilities/            ← capability retrieval (keyword scorer, models)
    checkpoint/              ← store Protocol, in-memory + Mongo, async adapter, resume, composition
    context/                 ← Context Engine, providers, prioritizer, budget, final-prompt builder
    evaluation/              ← deterministic answer evaluation engine + models
    execution/               ← executor, shared state, adapter runner
    gate/                    ← BehaviorGate (direct vs planner)
    llm/                     ← provider boundary: final_provider, planner_provider, provider_adapter
    models/                  ← typed models: tool_spec, plan, final_prompt, planner_prompt, policy…
    optimization/            ← plan optimizer (execution groups)
    policy/                  ← policy engine (annotate-only)
    registry/                ← ToolRegistry + loader
    repair/                  ← repair runtime + models
    retriever/               ← hybrid retrieval pipeline (embeddings, reranker, capability, context)
    runtime/                 ← orchestrator, direct/planner runtimes, outcome, factory,
                               streaming, events, resume_coordinator, context (RunContext)
    tools/                   ← ToolAdapter ABC, adapter registry, internal V1.5 adapters
    validation/              ← structural plan validator
  routes/                    ← FastAPI routers (agent.py = V2; chat/documents/jobs/memory/health = V1.5)
  services/                  ← V1.5 platform services (llm_client, retrieval, memory, …) — DO NOT import from agent
  schemas/                   ← request/response models (agent.py = AgentRun/Resume request+response)
  config.py                  ← Settings (agent_use_real_llm, agent_checkpoint_backend, …)
  main.py                    ← app factory + lifespan (composition root: wires stores + providers)
tests/
  agent/                     ← 481 unit tests (config-free)
  api/                       ← 45 API tests (TestClient, injected fakes)
```

**Composition roots** (where real wiring happens): `app/main.py` (lifespan) and
`app/agent/runtime/factory.py` (`build_default_runtime`). Everywhere else,
dependencies are injected.

---

## If Starting a New Claude Session

The repository is the source of truth. To reconstruct the project's state before
touching anything, do this **in order**:

1. **Read `backend/app/agent/PROJECT_STATE.md`** (this file) — the current-state
   handoff: branch, commit, phases, components, locked decisions, roadmap.
2. **Read `backend/app/agent/ARCHITECTURE.md`** — the deeper living architecture
   and phase-compatibility report.
3. **Read the git log** — `git log --oneline -40`. Each `V2 Phase N` commit is one
   phase; the latest commit is the current frontier.
4. **Inspect the latest phase** — read the source + tests for the most recent
   phase(s) to see the exact current contracts (e.g. `runtime/streaming.py`,
   `runtime/orchestrator.py`, `llm/*.py`, and their tests).
5. **Only then modify code** — and only within the locked-decisions constraints
   above.

### Working rules for this repository
- **Do not modify V1.5 services** (`app/services/*`). V2 layers on top.
- **Keep the agent config-free at import** — lazy-import `app.services` /
  `app.config` inside methods; tests must run with only pydantic + pytest
  (+ fastapi/httpx for API tests).
- **No vendor SDK in `app.agent`.** Reach LLMs only through V1.5's `llm_client`.
- **Routes stay transport-only.** Business logic belongs to the runtime.
- **Tests use injected fakes and drive async with `asyncio.run`.** No real DB,
  no credentials in unit tests.

### For a documentation-only change like this one
1. Make the documentation change (no runtime code touched).
2. Run the suite and confirm it is unchanged:
   ```
   cd backend && python -m pytest
   ```
   Expect **526 passed** (unchanged — docs do not affect tests).
3. Commit the change.
4. Generate the single-commit patch for delivery:
   ```
   git format-patch -1 --stdout > projectstate.patch
   ```
