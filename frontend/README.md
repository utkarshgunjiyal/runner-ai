# Runner.ai — Web UI (Phase 41B)

A React + TypeScript + Vite single-page app for Runner.ai V2: conversational
requests, **true token streaming**, a safe runtime activity timeline, and
**human-in-the-loop** (clarification, approval/rejection, deferred waits) with
checkpoint-based resume.

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
npm run lint         # eslint (0 warnings allowed)
npm test             # vitest (mocked fetch/streams — no live backend needed)
npm run build        # typecheck + production build to dist/
npm run preview      # preview the production build
```

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
  components/
    chat/     ChatShell · MessageList · Composer · StreamingMessage
    runtime/  RuntimeTimeline · RuntimeEventCard · RuntimeOutcomeBadge · ToolExecutionCard
    hitl/     ClarificationPanel · ApprovalPanel · WaitingContextPanel · FailedRunPanel
```

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

## Demo workflow

1. Start the backend (`cd ../backend && uvicorn app.main:app --reload`).
2. `npm run dev`, open http://localhost:5173.
3. Ask a question → watch tokens stream and the runtime timeline populate.
4. To see HITL, configure a run that pauses (e.g. an evaluator that asks for
   clarification) → answer the clarification/approval → the run continues in place.
