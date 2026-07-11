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
3b. **Document inventory (deterministic, no retrieval).** In a **brand-new, empty**
   thread ask `What documents are uploaded?` The agent answers **"There are
   currently no uploaded documents in this conversation. Upload a PDF to ask
   questions about it."** — with **no résumé content, no `E#` citations, and no
   retrieval activity** in the runtime inspector (Phase 46.1). This is a
   deterministic fast path: inventory questions list the thread's own document
   records (the same records the selector shows) and never run vector search.
   Upload a PDF and repeat → it lists `filename — Ready`; open another empty thread
   and repeat → empty again (per-thread isolation).
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
6. **Comparison (compact, source-separated answer).** Select two documents (or ask
   to compare them) and ask e.g. `Compare the technical skills in these two
   documents`. Retrieval is balanced **per document** so neither dominates, and the
   answer comes back **compressed and source-separated** — a `Document N — filename`
   section per document with skills grouped by category (Languages, Backend, AI /
   Machine Learning, Analytics / Automation, …), then **Similarities** and
   **Differences** stated as concepts (e.g. "Both include Python"; "one is
   analytics-focused, the other adds AI engineering and backend APIs"), then
   **Sources** citing file and page (`resume.pdf p.1`). Even in the default **demo**
   mode (`AGENT_USE_REAL_LLM=false`) the deterministic fallback compresses the
   retrieved chunks into concise skills — **no raw chunk dumps, no duplicated
   bullets, no contact/education/extracurricular noise, and no opaque `E#`
   citations** (Phase 44.2). Facts are never merged across the two files, and a
   selected document with no matching technical evidence is stated explicitly ("No
   relevant technical-skill evidence was found in {filename}.") rather than dropped.
   Enabling the real LLM (`AGENT_USE_REAL_LLM=true`) yields richer prose from the
   same comparison-marked prompt.

Filenames are only for matching/display; retrieval always uses the stable
`document_id`. Client-sent `selected_document_ids` are hints, revalidated
server-side (see [`SECURITY.md`](./SECURITY.md)). Details:
[`THREAD_DOCUMENT_MODEL.md`](./THREAD_DOCUMENT_MODEL.md).

## GitHub read-only connector (Phase 46.2)

Optional. Enable with `GITHUB_MCP_ENABLED=true` + a **low-privilege read-only**
token (`GITHUB_MCP_TOKEN` or `GITHUB_PERSONAL_ACCESS_TOKEN`) at the deployment
level (see [GITHUB_MCP.md](./GITHUB_MCP.md)). Everything goes through the real MCP
stack — no direct GitHub REST.

In **Docker Compose** the backend runs in a container, so use the default
`GITHUB_MCP_TRANSPORT=http` — it reaches the official remote endpoint
(`https://api.githubcopilot.com/mcp/`) over outbound HTTPS with **no Docker socket
mount**. (`GITHUB_MCP_TRANSPORT=stdio` is a local developer mode that needs Docker
on the host and does not work inside the Compose backend.)

- **Missing config** → Runner.ai starts normally; the Integrations panel shows
  GitHub **Not configured**; document/chat flows work; a GitHub request gets a safe
  unavailable result (GitHub tools are excluded before planning — no doc fallback).
- **Connected** → the Integrations panel shows **Connected** with the enabled
  read-only capabilities; **no token is ever shown**.
- Ask **"List my GitHub repositories."** → real repositories, safe links, no
  invented data; the runtime inspector shows the GitHub read tool.
- Ask **"Show details for utkarshgunjiyal/runner-ai."**, **"List open issues in
  …"**, **"List open pull requests in …"** → real, normalized results (or a clear
  "none" response).
- Ask **"Create a GitHub issue."** → no write tool exists; a truthful read-only
  limitation, no external change.
- **Stop the MCP server** and repeat a GitHub request → safe failure, no crash, no
  fallback to document retrieval.

> Deployment-scoped, **not** per-user OAuth — every user of the deployment shares
> the configured account, so restrict access. Live verification:
> `./scripts/verify-github-mcp.sh` (opt-in; never prints the token; no writes).

## Workspace UI walkthrough (Phase 45)

The frontend is a three-region **AI workspace**, worth showing on camera:

- **Left rail** — Runner.ai branding, **New conversation**, and the recent list
  (relative last-activity time + message count; a loading **skeleton**, an empty
  state, and an **error + Retry** when the backend is unreachable). At the bottom,
  a truthful **Integrations** section: **GitHub** and **Gmail** show *Not connected
  — coming next*, **MCP Runtime** shows *Available*. Nothing here implies a live
  OAuth session — there are no connect actions, and real per-user Gmail/GitHub is
  **not implemented** (honest limitation).
- **Center** — the active **thread title** in the header with a live status badge;
  the conversation; document **scope chips** above the composer showing whether the
  run searches *all* documents or a restricted selection (remove a chip to widen
  scope); a sticky composer (**Enter** sends, **Shift+Enter** newlines, **Stop**
  while streaming). Comparison answers render per-document sections with the
  **Sources** lifted into filename+page **chips** — no `E#` ids.
- **Right inspector** — open **Runtime details** from the ⚙ header button (a dot
  marks new activity). It is **collapsed by default** and holds the status summary,
  tool activity, and the safe runtime timeline (safe metadata only — no
  chain-of-thought, prompts, secrets, or payloads).

Resize to show responsiveness: on a tablet the inspector opens as a **sheet**; on a
phone the sidebar becomes a **drawer** (hamburger) and the composer stays sticky.

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
