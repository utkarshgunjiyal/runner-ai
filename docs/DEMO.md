# Demo Guide

A deterministic, interview-ready walkthrough of Runner.ai V2. It uses the
**deterministic runtime** (no paid LLM required) and a **demo mode** that makes a
genuine human-in-the-loop pause reproducible from the UI. Everything shown flows
through the real runtime → checkpoint → resume — nothing is faked in the UI.

## What the demo proves

1. A request is routed, context is built, capabilities are retrieved, a tool
   executes, and the answer **streams token-by-token** over SSE.
2. A second request **pauses for human approval**, persists a checkpoint, and
   **resumes the same run** from that checkpoint after you approve.
3. Requests are **correlatable** across logs by `request_id` and `run_id`.
4. Health and (optionally) metrics can be shown safely.

## Prerequisites

- Docker Engine + Compose on the host, **or** the local host run (backend +
  frontend). No LLM API key is needed — demo mode uses the deterministic
  provider.

## Start the demo

### Option A — full stack (Docker, private demo profile)

```bash
cp .env.example .env
# For a local demo you can leave DOMAIN as-is and use http://localhost.
DOMAIN=localhost TLS_EMAIL= \
  docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.demo.yml \
  up -d --build
BASE_URL=http://localhost CURL_OPTS=-k ./scripts/smoke-test.sh   # optional
```

The demo profile sets `ENVIRONMENT=demo`, `DEMO_MODE=true`,
`AGENT_USE_REAL_LLM=false`. Keep it **private** (localhost, or Caddy basic auth).

### Option B — local dev (fastest for screen-recording)

```bash
# Backend with demo mode on
cd backend && pip install -r requirements.txt
DEMO_MODE=true ENVIRONMENT=demo uvicorn app.main:app --port 8000
# Frontend (separate shell)
cd frontend && npm ci && npm run dev        # http://localhost:5173
```

## Demo inputs and expected outcomes

The DemoEvaluator keys off the request text (case-insensitive). These are stable:

| # | Type          | Example input                                    | Expected outcome        | UI |
|---|---------------|--------------------------------------------------|-------------------------|----|
| 1 | Autonomous    | `What does the document say about pricing?`      | `completed`             | timeline populates; answer streams token-by-token |
| 2 | **Approval**  | `Delete all archived documents for finance`      | `waiting_for_approval`  | ApprovalPanel appears with a checkpoint; **Approve** → run resumes and completes |
| 3 | Clarification | `Summarize the report`                           | `waiting_for_user`      | ClarificationPanel; your answer resumes the run |

Approval keywords: `delete`, `deploy`, `purchase`, `send email`, `approve`.
Clarification keywords: `summarize the report`, `clarify`, `ambiguous`.
Anything else completes normally.

## Expected runtime events (scenario 1)

`runtime_started` → context/behavior events → capability retrieval → tool
execution (`ToolExecutionCard`) → `answer_started` → several `answer_chunk`
(streaming) → `runtime_completed` with outcome `completed`.

## Expected runtime events (scenario 2, HITL)

`runtime_started` → … → `runtime_completed` with outcome `waiting_for_approval`
and a **`checkpoint_id`**. The UI shows the ApprovalPanel. On **Approve**, the
client calls `POST /agent/resume` with `{kind: "approval"}`; the **same run**
continues and returns a final answer (JSON, not streamed).

## Threads & documents

Phase 43 scopes every request to one user's conversation and its documents. To
show it end-to-end:

1. **New conversation.** Click **New conversation** in the sidebar. It creates a
   persistent thread immediately (`POST /threads`), makes it active, and clears the
   messages, documents, run, checkpoint, HITL state, and any selected documents. A
   failure is surfaced inline, not swallowed.
2. **Upload into a thread.** With a thread open, upload a document. The selector
   keeps the filename visible, shows **Uploading…**, and the client **auto-polls**
   `GET /documents/{id}` until the status is completed/failed — so the document row
   updates on its own with no manual refresh (inline safe error on failure; the
   file input clears only on success). It is stored with that thread's `thread_id`;
   only this thread can retrieve over it.
3. **Thread-wide question.** Ask something like `What do these documents cover?`
   Retrieval filters Qdrant by `user_id` and the thread's full owned document
   set — no other thread's or user's documents are reachable.
4. **Single-document question.** Reference one file by name (e.g.
   `Summarize invoice_2024.pdf`). The resolver matches it to a stable
   `document_id` and retrieval is scoped to just that document.
5. **Document-ambiguity clarification (reliable pause).** Upload two files and ask
   a **vague** question like `summarize the report` (no filename). With multiple
   documents present the run **never silently guesses** — not even the
   most-recently-uploaded one — so it reliably pauses `waiting_for_user` with
   `pending_action="select_document"`, persists a checkpoint, and returns
   `document_candidates` (safe fields only: `document_id`, `filename`,
   `created_at`). The UI shows a **document picker**; picking a document resumes the
   **same run** — the backend revalidates the id against the owned set and continues
   retrieval over the chosen document.
6. **Comparison (source-separated answer).** Select two documents (or ask to
   compare them) and ask e.g. `Compare the technical skills in these two documents`.
   Retrieval is balanced **per document** so neither dominates, and the answer comes
   back **source-separated** — a `Document N — filename` section per document, then
   explicit **Similarities** and **Differences**, then **Sources**, with citations
   that name the file and page (`resume.pdf p.1`). Facts are never merged across the
   two files, and a selected document with no matching evidence is stated explicitly
   ("No relevant evidence was found in {filename}.") rather than dropped. This holds
   even in the default **demo** mode (`AGENT_USE_REAL_LLM=false`): the deterministic
   fallback provider synthesizes the same structure from the grouped evidence, so the
   comparison is never a single blended paragraph (Phase 44.1).

Filenames are only for matching/display; retrieval always uses the stable
`document_id`. Client-sent `selected_document_ids` are hints, revalidated
server-side (see [`SECURITY.md`](./SECURITY.md)). The runtime activity timeline is
**collapsed by default** (expand it to inspect stages — still safe metadata only).
Details: [`THREAD_DOCUMENT_MODEL.md`](./THREAD_DOCUMENT_MODEL.md).

## Show correlation (logs)

Every response carries an `X-Request-ID`. To follow one request across services:

```bash
docker compose logs backend | grep '"request_id":"<id>"'
```

Runtime events also carry the `run_id`; a HITL pause/resume shares the same
`run_id` across the two calls.

## Show health / metrics safely

```bash
curl -s http://localhost:8000/health/live      # {"status":"alive"}
curl -s http://localhost:8000/health/ready      # dependency map, 200/503
# Metrics are internal only and 404 at the public edge (Caddy). If you enabled
# METRICS_ENABLED, view them from inside the network, never publicly.
```

## Reset

```bash
# Docker: wipe state and restart clean
./scripts/stop.sh --with-volumes                # destroys demo data (guarded)
# then re-run the start command above
```

A new run always mints a fresh `run_id`; you don't need to reset between scenarios.

## Troubleshooting

- **HITL never pauses** → demo mode is off. Confirm `DEMO_MODE=true` and
  `ENVIRONMENT=demo` (not `production` — the guard refuses demo in production).
- **Answers look like `[stub-llm] …`** → expected in demo mode (deterministic
  provider). For real answers set an LLM key and `AGENT_USE_REAL_LLM=true` (not
  needed for the demo).
- **Backend refuses to start** → you set `ENVIRONMENT=production` with the dev
  auth stub. Use `ENVIRONMENT=demo` for the demo, or `ALLOW_DEV_AUTH=true`.
- **SSE not streaming through a proxy** → ensure Caddy/nginx buffering is off
  (the bundled configs do this) and heartbeats are enabled.

## Fallback recording plan

If a live provider or the network fails during a recording, **run the demo in
demo mode** (deterministic, offline) — it needs no external calls and produces
identical event flow. Keep a pre-recorded screen capture of scenarios 1–2 as a
backup to play if the environment is unavailable. Never fake outputs live; if
something breaks, switch to the recording.
