# Runner.ai V2 — Agent Architecture & Compatibility Report

Status: **living architecture document** for the V2 autonomous execution layer
(`backend/app/agent/`). Documentation only — this file describes the locked V2
architecture, maps it onto the Phase 1–9 code that already exists on this
branch, and defines the next implementation phases. It supersedes and extends
`docs/architecture/v2.md` (the frozen pre-implementation freeze); where the two
differ, this file is authoritative.

No runtime logic is introduced by this document. V1.5 services are not modified.

---

## 1. Current Phase 1–9 implementation summary

All of the following exist under `backend/app/agent/` and are covered by tests
in `backend/tests/agent/`. Each is deterministic, side-effect-free unless noted,
and imports only from `app.agent.*` (never from `app.services.*` yet — the
adapters that will bridge to V1.5 are still fakes).

| Phase | Module(s) | What exists |
|---|---|---|
| 1 | `models/tool_spec.py`, `registry/registry.py`, `registry/loader.py`, `tools/internal/specs.py` | `ToolSpec` metadata model + enums (`ToolKind`, `RiskLevel`, `SideEffectType`, `LatencyClass`); `ToolRegistry` (register/get/exists/list/filter, deterministic); `get_default_tool_registry()`; 10 **metadata-only** internal specs mapping V1.5 capabilities |
| 2 | `capabilities/models.py`, `scoring.py`, `retriever.py`, `keyword_retriever.py` | `CapabilityRetrievalRequest/Match/Response`; deterministic weighted keyword scoring; `CapabilityRetriever` ABC; `KeywordCapabilityRetriever` with filters + evidence-priority fallback |
| 3 | `models/plan.py` | `PlanStepType`, `FinalResponseMode`, `ArgBinding`, `PlanStep`, `Plan` (DAG validation, cycle detection, helpers `get_step`/`dependency_graph`/`root_steps`/`terminal_steps`) |
| 4 | `models/validation.py`, `validation/structural_validator.py` | `ValidationSeverity/Issue/Report`; `StructuralPlanValidator` (capability existence, enabled, arg schema, dependency + binding checks) |
| 5 | `models/policy.py`, `policy/engine.py` | `PolicyDecision`, `PolicyReasonCode`, `StepPolicyDecision`, `PolicyReport`; `PolicyEngine` (ALLOW / REQUIRE_APPROVAL / BLOCK, most-restrictive-wins) |
| 6 | `models/optimization.py`, `optimization/optimizer.py` | `OptimizationType/Note`, `ExecutionGroup`, `OptimizedPlan`, `OptimizationReport`; `PlanOptimizer` (DAG-level execution groups, duplicate detection, policy annotations) |
| 7 | `models/execution.py`, `execution/state.py`, `execution/runner.py`, `execution/executor.py` | `StepStatus`, `StepExecutionResult`; `ExecutionState` blackboard; `ToolRunner` ABC + `FakeToolRunner`; `PlanExecutor` (sequential group execution, binding resolution, policy-aware skip/block/await) |
| 8 | `tools/adapter.py`, `tools/adapter_registry.py` | `ToolAdapter` ABC (`execute(tool, args) -> dict`); `AdapterRegistry` (register/get/exists/list_kinds by `ToolKind`) |
| 9 | `execution/adapter_runner.py` | `AdapterToolRunner` (a `ToolRunner` that dispatches `ToolRegistry → ToolSpec → AdapterRegistry → adapter → output`) |

**What is deliberately NOT present yet:** any LLM call in the agent layer, any
real adapter that touches V1.5 services, the Context Engine, the Behavior Gate,
`RunContext`, the Token Budget Manager, the orchestrator, and the `/agent/run`
endpoint.

---

## 2. Locked V2 architecture

```mermaid
flowchart TD
    Req["User Request"] --> CE["Context Engine"]
    CE --> HP["Hybrid Context Prioritizer"]
    HP --> TB["Token Budget Manager"]
    TB --> RC[("RunContext")]
    RC --> BG{"Behavior Gate"}

    BG -->|simple| DIRECT["Direct Path"]
    BG -->|multi-step| PLAN["Planner Path"]

    subgraph PlannerPath["Planner Path"]
        CR["Capability Retrieval (uses RunContext)"] --> PL["Planner LLM → Plan (DAG)"]
        PL --> VA["Validator"] --> PO["Policy Engine"] --> OP["Optimizer"] --> EX["Executor"]
        EX --> ATR["AdapterToolRunner"]
        ATR --> ICR["Intelligent Capability Registry"]
        ICR --> AD["Internal / API / MCP Adapters"]
        AD --> V15["V1.5 services / tools"]
    end

    DIRECT --> V15
    V15 --> RCU["RunContext updated (tool outputs, evidence)"]
    EX --> RCU
    RCU --> FCB["Final Context Builder"]
    FCB --> FLLM["Final LLM"]
    FLLM --> Ans(["Answer + evidence + metadata"])

    RC -. carried through .-> RCU
```

Both paths write into the **same `RunContext`**, and both terminate at the
**Final Context Builder → Final LLM**. `RunContext` is created before the
Behavior Gate and is never discarded.

---

## 3. Runtime principles

1. **The LLM plans and writes prose; it never executes tools.** Planning and
   final answering are the only two LLM calls in the base pipeline (a reranker /
   judge is a future, optional third).
2. **Deterministic-first.** Every decision that can be made by rules is made by
   rules (behavior gating, context prioritization tier 1, validation, policy,
   optimization, execution scheduling). Semantic / LLM judgment is layered on
   top, never underneath.
3. **A plan is a DAG**, not a list — this is what makes the Optimizer and
   Executor able to parallelize and schedule.
4. **Context is first-class and durable.** Working context is assembled up
   front and travels through the whole run inside `RunContext`; it must never
   disappear after planning.
5. **Working context vs external retrieval are different things.** Recent
   messages, thread summary, pinned preferences, user knowledge, and active
   execution state are *always-loaded working context*. Document chunks, email,
   calendar, invoices are *external retrieval* — fetched on demand by
   capabilities, not preloaded.
6. **One-way dependency.** `app.agent.*` may import `app.services.*`;
   `app.services.*` must never import `app.agent.*`. The agent layer stays
   deletable and V1.5 stays intact.
7. **Fast path is a feature.** Simple operations bypass the planner to preserve
   latency and cost.
8. **Everything is observable.** Each run has stable ids and a durable
   `RunContext` that doubles as the audit record.

---

## 4. Runtime invariants

- `RunContext` exists from just after the Context Engine until the final
  response; no stage may drop or replace it (stages append/annotate).
- The Token Budget Manager is the **only** component that decides what text
  enters the planner prompt and the final-answer prompt.
- Capability Retrieval consumes `RunContext`, not the raw question alone.
- The Executor is the **only** component that invokes tools; the LLM output is
  always a `Plan`, never a side effect.
- A `BLOCK` policy decision means the step is recorded and never executed; a
  `REQUIRE_APPROVAL` decision means the step is recorded as awaiting approval and
  never executed until approval exists (HITL enforcement is a later phase).
- The Optimizer never changes plan semantics (no step removal, no arg rewrite,
  no dependency change) — only scheduling and annotation.
- Direct path and planner path produce the same `RunContext` shape so the Final
  Context Builder is path-agnostic.

---

## 5. End-to-end workflow

1. **Context Engine** assembles working context for `(user_id, thread_id)`.
2. **Hybrid Context Prioritizer** ranks working-context items (deterministic →
   semantic → future reranker).
3. **Token Budget Manager** selects the subset that fits the planner budget.
4. **`RunContext`** is created holding request + working + prioritized context.
5. **Behavior Gate** reads `RunContext` and chooses direct vs planner.
6a. **Direct path**: resolve the single capability, execute it (via the same
    adapter layer), write outputs/evidence into `RunContext`.
6b. **Planner path**: Capability Retrieval (RunContext-aware) → Planner LLM →
    Validator → Policy → Optimizer → Executor (via AdapterToolRunner → adapters
    → V1.5 services), writing each artifact into `RunContext`.
7. **Final Context Builder** assembles the final-answer prompt from prioritized
   working context + tool outputs (as evidence), within the answer budget.
8. **Final LLM** produces the grounded answer; response + evidence + metadata
   are returned and recorded on `RunContext`.

---

## 6. Context Engine design

**Purpose.** Produce the *working context* — the always-relevant, user-scoped
state that any request may need — independent of the specific question.

**Location (planned).** `app/agent/context/engine.py`.

**Inputs.** `user_id`, `thread_id`, the raw request, and a handle to the active
`ExecutionState`/`RunContext` (for follow-up turns).

**Assembles (working context):**
- recent messages — reuse `app.services.message_service.get_recent_messages`
- thread summary — reuse `app.services.thread_summary_service.get_thread_summary`
- pinned / recent user preferences — reuse `app.services.preference_service.get_preferences`
- user knowledge — reuse `app.services.knowledge_service` (list/search)
- active execution state — from a prior/ongoing `RunContext`

**Explicitly does NOT load:** document chunks, email, calendar, invoices, or
other corpora. Those are *external retrieval* surfaced through capabilities /
tools during execution, and are pulled only when a step needs them. This keeps
the working context small, cheap, and bounded.

**Output.** A list of `ContextItem`s (proposed model), each with `source`,
`content`, `provenance` (ids/seq), and raw deterministic signals (recency,
role, pinned flag). It reuses the *intent* of V1.5's `ContextEvidence` but is an
agent-layer model so the agent package stays self-contained.

**Reuse note.** V1.5 already has these retrieval functions; the Context Engine
is an orchestration layer over them, not a reimplementation.

---

## 7. Hybrid Context Prioritizer design

**Purpose.** Order working-context items by usefulness, using a tiered strategy
so the cheap deterministic signal dominates and expensive judgment is optional.

**Location (planned).** `app/agent/context/prioritizer.py`.

**Tiers (in order of authority):**
1. **Deterministic signals (implemented-first).** Recency, message role,
   pinned/explicitly-referenced items, source-type weight (reuse the
   `evidence_priority` / `context_weight` fields already on `ToolSpec`, and the
   priority concept from V1.5 `ContextPolicy`). This tier alone produces a valid
   ordering.
2. **Semantic similarity (next).** Embed the request and each item; blend the
   similarity score with the deterministic score (e.g. weighted sum or
   tie-break). Reuse `app.services.embedding_service`.
3. **Reranker / LLM judge (future — documented only, not implemented now).** A
   cross-encoder or LLM scores the top-N candidates for final ordering.

**Output.** Prioritized `ContextItem`s carrying `score`, the contributing
signals, and a short reason (for observability), mirroring how
`CapabilityMatch` already records `matched_fields` / `reason`.

**Principle.** Deterministic first, semantic second, reranker last — never
invert the order.

---

## 8. Token Budget Manager design

**Purpose.** Decide precisely what text enters each LLM prompt. It is the single
authority over prompt composition size.

**Location (planned).** `app/agent/context/budget.py`.

**Two budgets:**
- **Planner budget** — prioritized working context + retrieved capabilities that
  fit into the Planner LLM prompt.
- **Final-answer budget** — prioritized working context + tool outputs
  (evidence) that fit into the Final LLM prompt.

**Mechanism.** Reuse V1.5's proven approach in
`app.services.context_composer` (`_apply_token_budget`, chars-per-token
estimate via `settings.context_chars_per_token`): keep items in priority order
until the budget is spent, truncate the boundary item, drop the remainder, and
record what was dropped. The agent-layer manager wraps that logic and reports a
`BudgetReport` (kept / dropped / truncated) onto `RunContext`.

**Why separate from the Prioritizer.** Prioritization decides *order*; budgeting
decides *cut-off*. Keeping them separate lets the same ordering feed two
different budgets (planner vs final) without recomputation.

---

## 9. RunContext design

**Purpose.** The spine of a run. Created after context assembly, carried through
every stage, and never discarded. It is both the working state and the audit
record.

**Location (planned).** `app/agent/models/run_context.py` (+ a small
`app/agent/context/run_context.py` for mutation helpers if needed).

**Fields (proposed):**
- **Identity:** `run_id`, `trace_id`, `user_id`, `thread_id`
- **Request:** raw question, `document_id`, and request options
- **Working context:** the Context Engine output
- **Prioritized context:** the Prioritizer output + `BudgetReport`s
- **Behavior profile:** direct vs planner + the reasons (from the Behavior Gate)
- **Selected capabilities:** the Capability Retrieval result (planner path)
- **Plan:** the `Plan` (Phase 3)
- **Validation report:** `ValidationReport` (Phase 4)
- **Policy report:** `PolicyReport` (Phase 5)
- **Optimized plan:** `OptimizedPlan` (Phase 6)
- **Execution state:** the Phase 7 `ExecutionState` (composed, not replaced)
- **Evidence:** normalized tool outputs used for the final answer
- **Final response metadata:** provider/model, token usage, timings

**Relationship to `ExecutionState` (Phase 7).** `RunContext` *composes*
`ExecutionState` (holds it as a field). Phase 7 stays untouched; `ExecutionState`
remains the per-step result store, and `RunContext` is the superset that also
holds context, plan, and policy artifacts.

**Mutability.** Append/annotate only; earlier artifacts are never overwritten,
which is what makes the run auditable and (later) resumable.

---

## 10. Behavior Gate design

**Purpose.** Decide **direct path vs planner path** using `RunContext`.

**Location (planned).** `app/agent/gate/behavior_gate.py`.

**Position.** After the Context Engine (so it can use working context), before
the path split.

**Strategy (deterministic-first).** Reuse V1.5's `behavior_router` heuristics
plus signals from `RunContext`:
- Simple, single-capability requests → **direct**: document Q&A, job status,
  preference update, simple memory question.
- Multi-step goals, actions with side effects, external-tool needs, or genuinely
  ambiguous requests → **planner**.
- (Future) a small LLM classifier only when the deterministic gate is
  low-confidence — never the planner itself.

**Output.** A `behavior_profile` written onto `RunContext` (chosen path + reason
+ confidence), so the decision is inspectable.

**Why after context, not before.** The gate benefits from knowing what working
context exists (e.g. an active plan, a referenced document) — context is cheap
and shared by both paths, so it is assembled first.

---

## 11. Direct path vs planner path

**Direct path** (latency- and cost-preserving):
- For the four simple operations. Each maps to a single capability executed
  through the **same adapter layer** the planner path uses (so there is one
  execution mechanism, not two).
  - document Q&A → the V1.5 composite `answer_from_context` (wraps
    `chat_service.handle_chat`)
  - job status → `get_job_status` (wraps `job_service.get_job`)
  - preference update → `save_user_preference` (wraps `preference_service.save_preference`)
  - simple memory question → `get_thread_summary` / `get_recent_messages` / knowledge
- No planner, validator, or optimizer. The result is written to `RunContext`,
  then the Final Context Builder + Final LLM produce the answer (for Q&A the
  composite already answers; the builder can pass it through).

**Planner path** (for multi-step goals):
- Capability Retrieval → Planner LLM → Validator → Policy → Optimizer → Executor,
  exactly the Phase 1–9 pipeline, all writing into `RunContext`.

Both paths converge on the Final Context Builder, guaranteeing a uniform
response contract.

---

## 12. Capability Registry evolution (Intelligent Capability Registry)

The existing `ToolRegistry` (Phase 1) remains the single source of truth and is
**not replaced**; it *evolves* by adding two projections and (later) retrieval
intelligence. **There is no separate Capability Binder.**

- **Planner view.** A projection of each `ToolSpec` containing only what the
  Planner LLM needs: `name`, `description`, `input_schema`, `examples`,
  `typical_user_questions`, `capability_tags`. Keeps the planner prompt small and
  hallucination-resistant.
- **Executor view.** A projection containing only dispatch metadata: `kind`,
  `handler_ref`, `timeout_seconds`, `max_retries`, `idempotent`, `side_effects`.
  Consumed by `AdapterToolRunner`.
- **Retrieval intelligence (later).** Capability embeddings stored in a dedicated
  Qdrant collection (reusing `embedding_service` + `vector_store_service`
  patterns) to move capability retrieval from keyword → hybrid.

These are additive methods/adapters over the existing registry; the Phase 1 API
(`register`/`get`/`list_*`/`filter_*`) is unchanged.

---

## 13. Capability Retrieval using RunContext

**Change from Phase 2.** `CapabilityRetrievalRequest.query` is a raw string
today. In V2, capability retrieval must be driven by `RunContext`, not the bare
question — it should use the request plus prioritized working-context signals
(e.g. an actively referenced `document_id`, recent intents, an in-flight plan).

**Approach (additive, non-breaking).** Add a `RunContext → CapabilityRetrievalRequest`
builder rather than changing the Phase 2 model. The builder composes a richer
query and sets filters (`allowed_kinds`, `allowed_risk_levels`, `required_tags`,
`excluded_tool_ids`) from `RunContext`. `KeywordCapabilityRetriever` continues to
work unchanged; the embedding/hybrid retrievers slot in behind the same
`CapabilityRetriever` interface.

**Output.** Top-K capabilities recorded on `RunContext.selected_capabilities`,
rendered to the planner as the **planner view** (§12).

---

## 14. Planner Engine

**Status:** not yet implemented (Phase 3 provides only the `Plan` output model).

**Location (planned).** `app/agent/planner/planner.py` (+ `prompt.py`).

**Input.** System prompt (decompose into a DAG using only the provided
capabilities; do not answer; output must match schema) + budgeted prioritized
context + the retrieved capabilities (planner view).

**Output.** A `Plan` (Phase 3) via schema-guided decoding, produced by reusing
the V1.5 `llm_client.complete`. The planner never executes anything.

**Loop (later).** On `ValidationReport.has_errors`, feed the issues back for a
bounded number of replans before failing the run.

---

## 15. Validator

**Status:** implemented (Phase 4), unchanged. `StructuralPlanValidator.validate(plan)`
→ `ValidationReport`. It answers "is the plan well-formed and executable against
the registry?" (capability existence, enabled, arg schema, dependency + binding
integrity). Runs after the planner, before policy. Collects all issues; never
fails fast; mutates nothing.

---

## 16. Policy Engine

**Status:** implemented (Phase 5), unchanged. `PolicyEngine(registry,
user_permissions).evaluate(plan)` → `PolicyReport` with per-step
`ALLOW`/`REQUIRE_APPROVAL`/`BLOCK` and reason codes, most-restrictive-wins.
Answers "should this be allowed here, and does it need approval?" — distinct
from the Validator. Annotation only; HITL enforcement lives later in the
Executor.

---

## 17. Optimizer

**Status:** implemented (Phase 6), unchanged. `PlanOptimizer.optimize(plan,
policy_report)` → `(OptimizedPlan, OptimizationReport)`. Builds DAG-level
execution groups (parallel where independent), detects duplicate tool calls
(annotation only), and preserves/annotates blocked and approval steps. Never
rewrites plan semantics.

---

## 18. Executor

**Status:** implemented (Phase 7), unchanged in contract. `PlanExecutor(tool_runner).execute(optimized_plan,
policy_report)` → `ExecutionState`. Runs execution groups in order (sequential
within a group for now), resolves `${step.output.field}` bindings from state,
skips dependents of failed/blocked/awaiting steps, and records every result.

**V2 integration (additive).** In the full pipeline the Executor is driven by
`AdapterToolRunner` (Phase 9) instead of `FakeToolRunner`, and its
`ExecutionState` is the one composed inside `RunContext`. No change to the
Executor's own logic is required for the base wiring; parallel execution,
retries, timeouts, and HITL pause/resume are later enhancements.

---

## 19. Adapter layer

**Status:** interfaces implemented (Phases 8–9); real adapters not yet written.

- `ToolAdapter` (Phase 8): `execute(tool, args) -> dict`, one per `ToolKind`.
- `AdapterRegistry` (Phase 8): maps `ToolKind → ToolAdapter`.
- `AdapterToolRunner` (Phase 9): `ToolRunner` that resolves `ToolSpec` from the
  registry and dispatches to the adapter for `tool.kind`.

**What must be added:** the **real internal adapter** that implements
`ToolAdapter` by calling the V1.5 service behind each internal `ToolSpec`
(`handler_ref` already records the intended target, e.g.
`app.agent.tools.internal.documents:search_documents`). This is the first point
where the agent layer touches live V1.5 code — a thin translation of typed args
to an existing service call, with zero duplicated business logic. API and MCP
adapters follow later.

---

## 20. Final Context Builder

**Purpose.** Assemble the final-answer prompt from `RunContext` after the direct
or planner path completes.

**Location (planned).** `app/agent/context/final_builder.py`.

**Inputs.** Prioritized working context + tool outputs (normalized to evidence)
+ the original question, within the final-answer token budget (§8).

**Mechanism.** Normalize tool outputs into evidence items and reuse V1.5's
`context_composer` (priority ordering + budget) to build the grounded prompt,
then call the V1.5 `llm_provider` (streaming-capable) for the answer. This is
where the agent and V1.5 share one grounding-and-generation path.

**Output.** The final answer plus the evidence actually used, recorded on
`RunContext.final_response_metadata` and returned to the caller.

---

## 21. Observability plan

**Id hierarchy** (each id owned by the layer that creates it):

```
request_id      — per HTTP request      (exists: logging_config contextvar + middleware)
  run_id/trace_id — per agent run        (RunContext; set by the orchestrator)
    plan_id        — per generated plan
      tool_execution_id — per tool invocation (== StepExecutionResult identity)
```

- Reuse V1.5 structured JSON logging (`logging_config`); add `run_id`/`trace_id`
  as contextvars so every agent log line carries them (additive).
- `RunContext` is the durable run artifact: persisted (later) to a Mongo
  `executions` collection for audit and resume.
- **Audit log** (later): append-only record of tool calls (redacted args), policy
  decisions, approval decisions, risk flags, and cost — distinct from debug logs.
- Name spans in an OpenTelemetry-compatible way now so traces can be exported
  later without renaming.

---

## 22. V3 roadmap (documentation only)

Beyond V2's plan-execute loop:

- **Reflection** — the agent critiques its own results and decides whether to
  continue, redo, or ask for help.
- **Dynamic replanning** — revise the plan mid-run based on tool outputs, not
  only on validation errors.
- **Execution learning** — persist run outcomes to improve future planning.
- **Capability success scoring** — track per-capability reliability/latency/cost
  and feed it into capability retrieval and optimization.
- **Adaptive planning** — choose plan shape (direct vs shallow vs deep) from
  historical signals rather than a fixed gate.

---

## 23. Architecture Compatibility Report

### 23.1 What Phase 1–9 already satisfies

- **Plan contract** — `Plan`/`PlanStep`/`ArgBinding` fully model the planner's
  output DAG, including bindings the Executor resolves. ✅
- **Validator** — structural validation is complete and matches the locked
  pipeline position (after planner, before policy). ✅
- **Policy Engine** — decision model + engine match the locked "Policy after
  Validator" ordering and the annotate-only rule. ✅
- **Optimizer** — execution-group scheduling + annotations match the locked
  "Optimizer before Executor" position. ✅
- **Executor + ExecutionState** — deterministic group execution, binding
  resolution, and policy-aware skipping exist; `ExecutionState` is a ready-made
  sub-component of `RunContext`. ✅
- **Adapter layer + AdapterToolRunner** — the `Executor → ToolRunner → Registry →
  AdapterRegistry → adapter` dispatch path is fully wired for fakes; the shape
  matches the locked "Executor → AdapterToolRunner → Intelligent Capability
  Registry → Adapters" segment. ✅
- **Capability Retrieval** — a working deterministic retriever with a stable
  `CapabilityRetriever` interface exists (keyword; hybrid slots in later). ✅
- **Registry** — the source-of-truth `ToolRegistry` + internal specs exist and
  are the base the Intelligent Capability Registry evolves from. ✅

### 23.2 What needs to be added (new modules)

- **Context Engine** (`context/engine.py`) — assemble working context from V1.5
  services.
- **Hybrid Context Prioritizer** (`context/prioritizer.py`) — deterministic +
  semantic ordering.
- **Token Budget Manager** (`context/budget.py`) — planner + final budgets.
- **RunContext** (`models/run_context.py`) — the durable run spine.
- **Behavior Gate** (`gate/behavior_gate.py`) — direct vs planner.
- **Direct-path handlers** — thin mappers from simple intents to a single
  capability.
- **Real internal adapters** (`tools/internal/documents.py`, `memory.py`,
  `jobs.py`, `chat.py`) — implement `ToolAdapter` over V1.5 services.
- **Planner Engine** (`planner/planner.py`, `prompt.py`) — LLM → `Plan`.
- **Final Context Builder** (`context/final_builder.py`) — assemble + Final LLM.
- **Orchestrator** (`orchestrator.py`) — chain the whole pipeline.
- **`/agent/run` route + agent DB collections** — additive `main.py`
  router include and `database.py` collections (`executions`, later `audit_logs`,
  `approvals`, `tool_capabilities`), both behind a feature flag.

### 23.3 What needs to be refactored (additively, non-breaking)

- **Capability Retrieval input** — add a `RunContext → CapabilityRetrievalRequest`
  builder; do **not** change the Phase 2 model. Keyword retriever unchanged.
- **Registry projections** — add planner-view / executor-view accessors to (or
  alongside) `ToolRegistry`; existing methods unchanged.
- **Executor wiring** — drive `PlanExecutor` with `AdapterToolRunner` and let
  `RunContext` own the `ExecutionState`; the Executor's internal logic is
  unchanged.
- **Loader** — extend `get_default_tool_registry` (or add a companion) to also
  build a default `AdapterRegistry` with the real internal adapter registered.

None of these require breaking edits to Phase 1–9 public contracts.

### 23.4 What must not be touched

- All Phase 1–9 **model contracts**: `ToolSpec`, `Plan`/`PlanStep`/`ArgBinding`,
  `ValidationReport`, `PolicyReport`, `OptimizedPlan`/`ExecutionGroup`,
  `StepExecutionResult`, and the capability retrieval models.
- The **Validator, Policy Engine, Optimizer, and Executor logic**.
- All **V1.5 services** (`app/services/*`) and V1.5 routes — the agent layer
  imports them; they never import the agent layer.
- Existing **tests** under `backend/tests/` — new phases add tests; they do not
  modify existing ones.

---

## 24. Next implementation phases (from this branch onward)

Ordered to keep each phase independently testable and to reach an end-to-end
`/agent/run` as directly as possible.

- **Phase 10 — RunContext + Context Engine.** Define `RunContext` (composing the
  Phase 7 `ExecutionState`) and the Context Engine that assembles working context
  from V1.5 services. Deterministic, no LLM. **Recommended next phase.**
- **Phase 11 — Hybrid Context Prioritizer + Token Budget Manager.** Deterministic
  prioritization (tier 1) + budgeting reusing `context_composer`. Semantic tier
  behind a flag; reranker documented only.
- **Phase 12 — Behavior Gate + Direct path.** Deterministic gate over
  `RunContext`; direct handlers for document Q&A, job status, preference update,
  simple memory — executed through the adapter layer.
- **Phase 13 — Real internal adapters + registry/adapter wiring.** Implement
  `ToolAdapter` over V1.5 services; extend the loader to build a default
  `AdapterRegistry`. First live agent → V1.5 execution (direct path works
  end-to-end).
- **Phase 14 — Planner Engine + RunContext-aware Capability Retrieval.** LLM →
  `Plan`; `RunContext → CapabilityRetrievalRequest` builder; registry planner
  view.
- **Phase 15 — Orchestrator + Final Context Builder + Final LLM.** Chain the full
  planner path and converge both paths on the builder; grounded final answer.
- **Phase 16 — `/agent/run` endpoint (+ SSE).** Feature-flagged route; agent DB
  collections; observability ids on `RunContext`.
- **Phase 17 — Persistence, audit, and HITL enforcement.** Persist `RunContext`;
  append-only audit log; executor approval pause/resume.

Later, independently: hybrid/embedding capability retrieval and reranker (§7,
§12), parallel execution + retries/timeouts in the Executor, and the V3 items
(§22).

### Recommended next implementation phase

**Phase 10 — RunContext + Context Engine.** It unblocks every downstream stage
(the Behavior Gate, Prioritizer, Budget Manager, and both paths all read/write
`RunContext`), it is fully deterministic and unit-testable without an LLM or live
infrastructure, and it composes the existing Phase 7 `ExecutionState` without
touching any Phase 1–9 or V1.5 code.
