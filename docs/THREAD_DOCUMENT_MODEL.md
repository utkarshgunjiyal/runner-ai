# Thread & Document Model

How Runner.ai V2 scopes a request to **one user's** conversation and documents,
resolves which document(s) a request is about, filters retrieval accordingly, and
pauses safely when a document reference is ambiguous. This is the Phase 43
integration layer that sits between authentication and the existing
planner/executor runtime.

Companion: [`CONNECTORS.md`](./CONNECTORS.md) (the connector/eligibility half of
Phase 43) and [`ARCHITECTURE_WALKTHROUGH.md`](./ARCHITECTURE_WALKTHROUGH.md) (the
full request lifecycle).

---

## Identifiers

Each identifier means exactly one thing and is never conflated with another:

| Identifier | Source | Meaning |
|---|---|---|
| `user_id` | **auth only** (`get_current_user`), never client-asserted | The authenticated principal. All ownership derives from it. |
| `thread_id` | Mongo `threads._id` | One conversation. Owns its messages, summary, documents, runs, checkpoints, title. |
| `document_id` | Mongo `documents._id` | One uploaded document. Stable id used for retrieval; the filename is for matching/display only. |
| `run_id` | minted per run | One execution of the runtime (survives a pause/resume as the **same** id). |
| `checkpoint_id` | minted on pause | A persisted paused-run snapshot used to resume. |
| `connector_id` | connector registry | One user's provider relationship (see `CONNECTORS.md`). |

**`user_id` is authoritative.** It comes from the auth seam and is never taken
from the request body. Everything else is validated *against* it before use.

---

## Ownership boundaries

Ownership is a strict tree: **user → thread → run**. Data is scoped to whichever
level owns it.

- **User scope** — `preferences`, `knowledge` (long-term memory), `connectors`
  (records / status / scopes / `credential_reference`).
- **Thread scope** — `messages`, `summary`, `documents` (each document now
  carries a `thread_id`), `chunks` (scoped indirectly via the thread's owned
  document set), `runs`, `checkpoints`, `title`.
- **Run scope** — `run_id`, plan, tool executions, events, evaluation, repair,
  checkpoint.

A request may only ever touch data owned (transitively) by its authenticated
`user_id`. A document referenced in thread A cannot be read from thread B, and no
user can reach another user's documents — the backend re-derives the owned set
from Mongo on every request rather than trusting anything the client sends.

---

## Intent vs. scope (two separate axes)

The interpreter (`app/agent/interpret/`) is **deterministic** and classifies a
request along independent axes. Intent is *what the user wants*; scope is *what
data it touches*. They do not imply each other.

**Intents:** `conversation_followup`, `thread_memory_qa`, `document_qa`,
`document_summary`, `document_comparison`, `page_qa`, `external_lookup`,
`external_action`, `mixed_request`.

**Document scopes:** `none`, `all_thread_documents`, `single_document`,
`selected_documents`, `specific_page`, `unresolved_document`.

**Connector scopes:** `none`, `github`, `gmail`, `calendar`,
`multiple_connectors`, `unresolved_connector`.

The interpreter produces a `RequestInterpretation` (intent + document scope +
connector scope + action type) with `resolution_source="deterministic"`. Keeping
these axes separate means, e.g., a `document_summary` intent can have a
`single_document` scope in one request and an `unresolved_document` scope in
another — the scope, not the intent, decides whether a clarification is needed.

---

## Request lifecycle

```
   POST /agent/run | /agent/run/stream            [user_id from auth ONLY]
        │
        ▼
  ┌────────────────────────────────────────────┐
  │ 1. Auth + thread ownership                   │  validate thread_id ⊆ user's
  │    persist user message (RunRecorder)        │  threads; store the message
  └───────────────┬────────────────────────────┘
        ▼
  ┌────────────────────────────────────────────┐
  │ 2. Interpret (interpret/)                    │  intent + document/connector
  │    deterministic — no LLM ownership calls    │  scope + action type
  └───────────────┬────────────────────────────┘
        ▼
  ┌────────────────────────────────────────────┐
  │ 3. Resolve documents (documents/resolver)    │  ownership-validated: hints
  │                                              │  revalidated vs owned set
  └───────────────┬────────────────────────────┘
        ▼
  ┌────────────────────────────────────────────┐
  │ 4. Scope Gate (runtime/scope_gate.py)        │
  │    ambiguous / unauthorized doc ref?         │
  │      → WAITING_FOR_USER + checkpoint +       │  SAFE candidate list
  │        document_candidates                   │  (document_id/filename/created_at)
  │    resolved? → attach labelled document       │
  │      chunk evidence                           │
  └───────────────┬────────────────────────────┘
        ▼
  ┌────────────────────────────────────────────┐
  │ 5. Behavior gate → capability retrieval       │  connector-eligibility
  │    (connector-eligibility filtered)          │  filtered (see CONNECTORS.md)
  └───────────────┬────────────────────────────┘
        ▼
  ┌────────────────────────────────────────────┐
  │ 6. Planner / policy / approval → executor    │  write/external actions stay
  │    → evaluate / repair → stream answer       │  approval-gated
  └───────────────┬────────────────────────────┘
        ▼
  ┌────────────────────────────────────────────┐
  │ 7. Persist assistant message + run metadata  │  RunRecorder.after_run
  │    schedule summary                           │
  └────────────────────────────────────────────┘
```

Steps 1–4 are the Phase 43 additions in front of the existing runtime (steps
5–7). The Scope Gate runs **early** — before any planning or tool retrieval — so
an ambiguous or unauthorized document reference never reaches the planner.

---

## Document resolution algorithm

`app/agent/documents/resolver.py` resolves a request's document reference against
the thread's **owned** document set. It is **deterministic**: the LLM never
decides ownership. Priority order (first match wins):

1. **UI-selected ids** — validated as a subset (`⊆`) of the owned set. Any id not
   owned by the thread invalidates the selection.
2. **Exact filename** match (unique).
3. **Unique normalized-filename** match.
4. **Unique partial / title** match.
5. **Single owned document**, or the recently-referenced document.
6. **Last-uploaded document** (only for a bare "all/none"-style reference).
7. **Clarification** — no confident match → pause for the user (see below).

A resolution carries the matched `document_ids` plus a `resolution_source`
(`exact_filename`, `normalized_filename`, `partial_filename`, `recent_document`,
…) for auditability. If a named reference matches multiple documents (e.g. two
files named `Report.pdf`), the resolver returns an **ambiguous** result with the
candidate list instead of guessing.

### Client hints are hints only

`selected_document_ids` supplied by the client are **hints, never
authorization**. The backend revalidates every id against the thread's Mongo
document set; ids that are not owned are dropped/rejected. **Filenames** are used
only for matching and display — retrieval always uses stable `document_id`s.

### Vague-reference policy (Phase 44 hardening)

A **vague** document reference — "this document", "the report", "the PDF", "that
file" — carries no filename to match. When multiple documents exist in the thread,
guessing is unsafe, so Phase 44 tightens exactly when such a phrase may
auto-resolve. A vague reference resolves automatically **only** in these three
cases:

1. **Exactly one document** is in the thread (nothing to be ambiguous about).
2. **The UI explicitly selected documents** (`selected_document_ids`, still
   revalidated against the owned set).
3. **The immediate prior turn genuinely referenced exactly one document** — read
   from the last assistant message's persisted `resolved_document_ids` (the "this"
   in "and what about this document's dates?" points at what the previous answer
   was already about).

**Forbidden weak signals.** "Last uploaded", "last indexed", and "newest / last in
the list" **never** silently resolve a vague phrase when multiple documents exist.
Recency is not intent. When none of the three cases hold, the run does **not**
guess — it pauses `WAITING_FOR_USER` with `pending_action="select_document"` and a
safe candidate list (the unchanged Phase 43 contract; see below). This closes the
gap where a stale "most recent" document could be silently answered against the
wrong file.

> The broader `resolver.py` priority list above still applies to *named*
> references; the vague-reference rules govern only phrases that name no document.

---

## Qdrant filter behavior

Retrieval is scoped in the vector store, not just in application logic:

- **Always** filters by `user_id`.
- **Document scope** is enforced by filtering to the thread's *validated*
  `document_id` set via a `MatchAny` filter, plus optional `pages`.
- New chunks also carry `thread_id`, `filename`, and `source_type` in the
  payload. This is **backward compatible**: older user-global chunks that predate
  Phase 43 have no `thread_id` but remain retrievable within a thread because
  they are reached through that thread's validated document-id set.

The entry point is:

```python
vector_store_service.search_scoped(
    query_vector, user_id, top_k,
    document_ids=..., pages=..., thread_id=...,
)
```

Called by the runtime **after** Mongo ownership validation, so the document-id
set passed in is already known to belong to the user's thread.

---

## Comparison-aware retrieval (Phase 44)

When a request resolves to **multiple documents** — a `document_comparison` intent
or a `selected_documents` scope — flat top-k retrieval tends to let whichever
document has the strongest chunks dominate the evidence, so the answer silently
skews toward one file. Phase 44 balances retrieval **per document** instead:

- Each resolved document gets its own quota of candidate chunks
  (`PER_DOCUMENT_CHUNK_QUOTA`, default **5**, configurable).
- The per-document candidate lists are **round-robin merged with
  de-duplication**, under a final `FINAL_CHUNK_BUDGET` (default **16**), so no
  single document can crowd the others out.
- Each chunk keeps its metadata (`document_id`, `filename`, `page`, `chunk_id`,
  `source_type`, `score`) intact through the merge — the labels the final context
  relies on are never lost.

Single-document requests are unaffected (one document, one quota).

---

## Source-aware final context (Phase 44)

Once chunks are retrieved, the evidence handed to the answer provider is
**labelled by source** so the model can attribute and separate facts:

- Every document chunk is rendered with `[DOCUMENT: filename] [PAGE: n]` labels.
- For multi-document / comparison requests, the answer prompt requires a
  **separate labelled section per document**, plus explicit **Similarities** and
  **Differences**.
- Citations are **source-aware** — filename + page, e.g. `resume.pdf p.1`.
- The prompt **forbids merging facts or identities across documents** (e.g. it must
  not attribute one résumé's skills to another person), which keeps comparisons
  honest rather than blending two files into one imagined profile.

---

## Thread switching semantics

A thread is a hard scope boundary. Switching threads changes `thread_id`, and
with it:

- the messages, summary, and title in context;
- the **owned document set** used for resolution and Qdrant filtering;
- the runs and checkpoints that can be resumed.

Documents, chunks, runs, and checkpoints do not leak across threads. Uploading a
document while in a thread associates it with that `thread_id`; asking a
thread-wide question retrieves only over that thread's documents. There is no
cross-thread document access, by construction.

---

## Ambiguity checkpoint / resume flow

When document resolution is ambiguous or the reference is unauthorized, the run
does not guess — it pauses.

1. **Pause.** The run enters `WAITING_FOR_USER` with
   `pending_action="select_document"` and persists a **checkpoint**.
2. **Safe candidates.** The response metadata carries `document_candidates`, a
   list of **safe** fields only: `document_id`, `filename`, `created_at`. No
   chunk text, no other users' data, no internals.
3. **UI picker.** The frontend renders a document picker from those candidates.
4. **Resume.** The client resumes with:

   ```json
   {
     "checkpoint_id": "<id>",
     "resolution": { "kind": "clarification", "value": ["<document_id>", ...] }
   }
   ```

5. **Revalidate + continue.** The backend revalidates the chosen ids against the
   owned set and the **same** `run_id` resumes, now running retrieval over the
   resolved document scope. No new run is started and no context is rebuilt from
   scratch.

The picked ids are treated as hints just like the initial selection — they are
re-checked against ownership, so a resume payload cannot be used to reach a
document the thread does not own.
