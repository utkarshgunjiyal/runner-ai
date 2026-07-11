# Runner.ai — Web UI (Phase 45)

A React + TypeScript + Vite single-page app for Runner.ai V2: a polished,
three-region **AI workspace** (conversations rail · chat · runtime inspector)
with conversational requests, **true token streaming**, a safe runtime activity
inspector, and **human-in-the-loop** (clarification, approval/rejection, deferred
waits) with checkpoint-based resume.

No UI framework, router, or CSS library is used — just React 18 + a single
design-token stylesheet (`src/styles.css`). The app is dark-first, responsive
(desktop columns → tablet inspector sheet → mobile sidebar drawer), and
accessible (semantic buttons, visible focus, `aria-*`, reduced-motion).

The UI is a thin transport + presentation layer over the existing backend API.
It contains **no business logic** — the runtime, planner, retrieval, evaluation,
and repair all live in the backend; the UI only renders `RuntimeEvent`s and drives
`/agent/resume`.

## Requirements

- Node 18+ (developed on Node 22)
- The Runner.ai backend running (see `../backend`)

## Install

```bash
cd frontend
npm install
```

## Environment variables

| Variable           | Default                 | Purpose                                                             |
| ------------------ | ----------------------- | ------------------------------------------------------------------ |
| `VITE_BACKEND_URL` | `""` (same-origin)      | Backend base URL. Empty = same-origin (dev server proxies `/agent`). Set for a cross-origin backend. |

In dev, leave it empty: the Vite dev server proxies `/agent/*` to the backend
(default `http://localhost:8000`, override with `VITE_BACKEND_URL`), so auth
cookies and SSE work same-origin without CORS friction.

## Development

```bash
npm run dev          # start the dev server on http://localhost:5173
```

The backend must be reachable at `VITE_BACKEND_URL` (default `http://localhost:8000`).
Auth uses **HTTP-only cookies** — the client always sends `credentials: "include"`
and never stores tokens in `localStorage`.

## Scripts

```bash
npm run typecheck    # tsc --noEmit
npm run lint         # eslint 9 flat config (0 warnings allowed)
npm test             # vitest (mocked fetch/streams — no live backend needed)
npm run build        # typecheck + production build to dist/
npm run preview      # preview the production build
```

## Production (Docker)

The production image (`frontend/Dockerfile`) is a multi-stage build: it compiles
the Vite assets and serves them with **nginx** (SPA fallback + `/agent` reverse
proxy with buffering off for SSE). The Vite dev server is **not** used in
production. The backend URL is set at container start via `BACKEND_URL` (default
`http://backend:8000`) — no rebuild needed to repoint it. Keep `VITE_BACKEND_URL`
empty so the SPA calls `/agent` same-origin and nginx proxies it (cookies stay
same-origin). Copy `.env.example` → `.env` for local overrides.

Brought up with the rest of the stack via `docker compose` (see
[../docs/DEPLOYMENT.md](../docs/DEPLOYMENT.md)); the UI is served on `:3000`.

## Architecture

```
src/
  api/
    types.ts        # typed backend contracts (RuntimeEvent, outcomes, resume)
    sseClient.ts    # POST-SSE via fetch → ReadableStream (EventSource can't POST)
    agentClient.ts  # POST /agent/resume (JSON)
  state/
    runTypes.ts     # explicit run state machine (idle → streaming → waiting/completed/failed)
    runReducer.ts   # pure event → state transitions; safe-only timeline mapping
  hooks/
    useRuntimeStream.ts  # one abortable stream (aborts prior on new request / unmount)
    useAgentRun.ts       # orchestrates submit → stream, resolve → resume
  lib/
    format.ts       # pure display helpers: relative time + safe answer/source parsing
  components/
    chat/         ChatShell (3-region layout) · MessageList · Composer · StreamingMessage
    threads/      ThreadSidebar (branding, recent, skeleton/empty/error, integrations)
    documents/    DocumentSelector (upload · multi-select · scope hints)
    integrations/ IntegrationsPanel (truthful Gmail/GitHub/MCP status)
    runtime/      RuntimeInspector · RuntimeTimeline · RuntimeEventCard · RuntimeOutcomeBadge · ToolExecutionCard
    hitl/         ClarificationPanel · ApprovalPanel · DocumentPickerPanel · WaitingContextPanel · FailedRunPanel
  styles.css      # design tokens (spacing/radii/surfaces/accent/status/shadows) + responsive layout
```

### Workspace layout (Phase 45)
Three regions inside `app-layout`:
- **Left rail (`ThreadSidebar`)** — Runner.ai branding, **New conversation**, the
  recent-conversation list (relative last-activity time + message count, loading
  **skeleton**, empty state, and an **error + Retry**), and a truthful
  **Integrations** section. Raw thread ids are never shown; an untitled thread
  falls back to a friendly label (backend title is preferred when present — no
  extra LLM call is made just for titles).
- **Center (`ChatShell` main)** — a header with the active **thread title**, a
  status indicator, and toggles; the message list; document **scope chips** above
  a sticky **Composer** (Enter sends · Shift+Enter newlines · Stop while
  streaming); HITL panels; and upload state.
- **Right (`RuntimeInspector`)** — **collapsed by default**, opened from the
  header. Shows the status summary, tool activity, and the safe runtime timeline —
  **safe metadata only** (never chain-of-thought, prompts, secrets, or payloads).

On tablets the inspector becomes an overlay **sheet**; on phones the sidebar
becomes a **drawer** (hamburger toggle) and the composer stays sticky. A scrim
closes either overlay.

### Assistant answers & sources
Comparison/QA answers preserve their paragraph and bullet structure (`white-space:
pre-wrap`). When an answer carries a trailing **Sources** list (Phase 44.x), those
are lifted into compact **source chips** (filename + page); bare evidence ids
(`E1`, `E7`, …) are never rendered as sources. Answer text is safe structured text
— no untrusted/dynamic HTML is injected.

### Integrations (live GitHub status — Phase 46.2)
`IntegrationsPanel` now fetches **live** status from `GET /integrations`
(`api/integrationsClient.ts`) and renders reality: **GitHub** shows its real
deployment connector state (*Not configured / Connecting / Connected / Degraded /
Authentication failed / Unavailable*) with its enabled **read-only** capabilities
and a **Refresh** action; **Gmail** stays truthfully *Coming next*; **MCP Runtime**
reflects runtime availability. There is **no token input** in the browser (GitHub is
configured at the deployment level), the panel never claims per-user OAuth, and any
fetch failure degrades to a safe fallback that never shows a false "Connected".

### Streaming
`POST /agent/run/stream` returns `text/event-stream`. `sseClient` reads the
`ReadableStream` and parses frames manually: it handles partial frames across
network chunks, multiple frames per chunk, malformed JSON (skipped safely), and
event ordering. Answer tokens (`answer_chunk`) render **immediately** — never
buffered. A bounded repair produces a second `answer_started` round, shown as a
fresh draft with the prior one superseded.

### Human-in-the-loop
When a streamed run ends `WAITING_*`, the terminal event carries a
`checkpoint_id`. The UI shows the matching panel and, on the user's response,
`POST /agent/resume` continues the **same** run:

| Outcome                | UI                          | Resolution sent                    |
| ---------------------- | --------------------------- | ---------------------------------- |
| `waiting_for_user`     | clarification input         | `{ kind: "clarification", value }` |
| `waiting_for_approval` | Approve / Reject (+ reason) | `{ kind: "approval" \| "rejection" }` |
| `waiting_for_context`  | safe deferred state         | — (deferred; no fake continuation) |
| `waiting_for_replan`   | safe deferred state         | — (deferred; no fake continuation) |
| `failed`               | safe message + Retry        | — (retry re-submits the request)   |

Resume is JSON (not streamed) — the UI shows a loading state and never pretends
it token-streams. Duplicate resume is prevented; the checkpoint id is replaced if
the run waits again and cleared on completion.

### Safety
Only **safe metadata** is rendered — capability ids, statuses, durations, safe
error codes/messages. Raw prompts, secrets, headers, environment, full internal
state, and stack traces are never displayed. A top-level `ErrorBoundary` keeps a
render error from leaking internals.

### Threads, documents & reliability (Phase 44)
- **New conversation.** The sidebar **New conversation** action creates a
  persistent thread immediately (`POST /threads`), makes it active, and clears the
  messages, documents, run, checkpoint, HITL state, and any selected documents in
  one step. A create failure is **surfaced inline**, never silently dropped, so the
  app never ends up in a half-cleared state pointing at no thread.
- **Upload polling & error UX.** The document selector keeps the filename visible
  during upload, shows an **Uploading…** state, and renders inline **safe** errors;
  the file input is cleared **only on success**. After an upload the client
  **auto-polls** `GET /documents/{id}` until the document is `completed`/`failed`
  (bounded poll interval + max duration), refreshing the thread's document list as
  the status changes — so a document row updates on its own with no manual refresh.
  Polling is **cancelled on thread switch / unmount** so a stale poll can't write
  into the wrong thread.
- **Collapsed runtime activity.** The runtime activity timeline is **collapsed by
  default** to keep the chat focused; it stays fully functional when expanded and
  still shows only safe metadata (no chain-of-thought, no raw prompts, no secrets).

## Demo workflow

1. Start the backend (`cd ../backend && uvicorn app.main:app --reload`).
2. `npm run dev`, open http://localhost:5173.
3. Ask a question → watch tokens stream and the runtime timeline populate.
4. To see HITL, configure a run that pauses (e.g. an evaluator that asks for
   clarification) → answer the clarification/approval → the run continues in place.
