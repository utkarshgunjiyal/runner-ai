# Runner.ai

A context-aware, document-aware AI assistant. Runner.ai routes each user
request through a deterministic intent classifier, selects a retrieval policy
for that intent, assembles priority-ordered evidence (recent messages, thread
summaries, long-term memory, and document chunks/summaries), enforces a context
budget, and generates a grounded answer — synchronously or streamed token-by-token.

> **Status:** V1.5. Document ingestion, semantic retrieval, real LLM answers,
> long-term memory, and SSE streaming are implemented and runnable via Docker
> Compose (see [Deployment](#deployment-docker-compose)).

---

## Architecture

```
POST /chat/ask  |  POST /chat/stream
  → thread + summary bootstrap        (thread_service, thread_summary_service)
  → atomic seq allocation + persist   (thread_service, message_service)
  → intent classification             (behavior_router  → RequestPlan)
  → deterministic preference capture  (preference_service)      [on "remember that…"]
  → retrieval policy selection        (context_policy_service → ContextPolicy)
  → memory retrieval                  (memory_retrieval_service → MemoryContext)
      recent messages · thread summary · user preferences · knowledge
      · document summary · page · chunks (Qdrant semantic search)
  → priority-ordered assembly + budget (context_composer)
  → answer generation                 (llm_provider → llm_client)  [sync or streamed]
  → persist answer + maybe summarize  (thread_summary_service)
```

**Document ingestion** runs asynchronously off the request path:

```
POST /documents/upload → validate → MinIO → Mongo document + job → Redis
  → worker: extract (pypdf) → chunk → embed → Qdrant → summary → status
```

The design is **schema-driven**: `RequestPlan`, `ContextPolicy`,
`ContextEvidence`, and `MemoryContext` (`backend/app/schemas/`) form the stable
contract every service reads and writes, so new evidence sources plug in
without reshaping the pipeline.

### Tech stack

| Concern          | Technology                          |
| ---------------- | ----------------------------------- |
| API              | FastAPI (async) + Uvicorn           |
| Data store       | MongoDB (Motor)                     |
| Job queue        | Redis                               |
| Object storage   | MinIO                               |
| Vector store     | Qdrant                              |
| LLM              | Anthropic / OpenRouter (httpx)      |
| Config           | Pydantic Settings                   |

---

## Deployment (Docker Compose)

Brings up the full stack: `mongodb`, `redis`, `qdrant`, `minio`, `backend`
(API), and `worker` (ingestion). The backend and worker share one image.

```bash
# 1. (optional) provide an LLM key — without one the app runs with a stub provider
cp .env.example .env          # then set ANTHROPIC_API_KEY=... (or OPENROUTER_API_KEY)

# 2. build + start everything
docker compose up --build     # add -d to run detached

# 3. verify
curl http://localhost:8000/health          # {"status":"healthy", ...}
docker compose ps                           # mongodb/redis healthy; backend healthy
```

Services & ports: API `:8000` (docs at `/docs`), Mongo `:27017`, Redis `:6379`,
Qdrant `:6333`, MinIO API `:9000` / console `:9001` (`minioadmin`/`minioadmin`).

**Health checks:** `mongodb` (mongosh ping), `redis` (redis-cli ping), and
`backend` (HTTP `/health`) report health to Compose; `backend` waits for Mongo
to be healthy and the `worker` waits on Mongo/Redis before starting. Qdrant and
MinIO use start-ordering plus a worker boot-retry (`_init_infra`) so a
slightly-late dependency self-heals rather than crash-loops. Everything is
`restart: unless-stopped`.

```bash
docker compose logs -f worker     # watch ingestion jobs
docker compose down               # stop (add -v to wipe volumes)
```

---

## Local development (without Docker for the app)

```bash
cp .env.example .env                     # localhost URLs are correct for host runs
docker compose up -d mongodb redis qdrant minio   # infra only

cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

uvicorn app.main:app --reload            # terminal 1 — API
python -m app.worker                     # terminal 2 — worker
```

---

## API reference

| Method | Path                    | Purpose                                        |
| ------ | ----------------------- | ---------------------------------------------- |
| GET    | `/health`               | Liveness + Mongo connectivity                  |
| POST   | `/chat/ask`             | Ask a question → `{thread_id, answer}`         |
| POST   | `/chat/stream`          | Same, streamed as Server-Sent Events           |
| POST   | `/documents/upload`     | Upload a PDF (multipart) → `{document_id, job_id}` |
| GET    | `/documents/{id}`       | Document status + summary                       |
| GET    | `/jobs/{id}`            | Ingestion job status                            |
| GET    | `/memory/preferences`   | List captured user preferences                  |
| GET    | `/memory/knowledge`     | List knowledge entries                          |
| POST   | `/memory/knowledge`     | Add a knowledge fact                            |

`/chat/*` accept `{"question": str, "thread_id"?: str, "document_id"?: str}`.

---

## End-to-end test

```bash
# 1. upload a PDF and wait for it to finish ingesting
curl -s -X POST localhost:8000/documents/upload -F 'file=@handbook.pdf'   # -> {document_id, job_id}
curl -s localhost:8000/documents/<document_id>                            # status: completed

# 2. ask about it (grounded in retrieved chunks)
curl -s -X POST localhost:8000/chat/ask -H 'Content-Type: application/json' \
  -d '{"question":"What does the document say about X?","document_id":"<document_id>"}'

# 3. stream an answer (SSE: status events, then tokens, then final)
curl -N -X POST localhost:8000/chat/stream -H 'Content-Type: application/json' \
  -d '{"question":"summarize page 2","document_id":"<document_id>"}'

# 4. long-term memory
curl -s -X POST localhost:8000/chat/ask -H 'Content-Type: application/json' \
  -d '{"question":"Remember that I prefer concise answers"}'
curl -s localhost:8000/memory/preferences
```

> With no LLM key set, answers come from a deterministic **stub** provider so the
> pipeline is fully exercisable offline; set `ANTHROPIC_API_KEY` (or
> `OPENROUTER_API_KEY`) for real generation.

---

## Configuration

All settings load from environment variables / `.env` via Pydantic Settings
(`backend/app/config.py`). See [`.env.example`](.env.example). Required:
`MONGO_URL`. Key LLM knobs: `LLM_PROVIDER` (`auto`/`anthropic`/`openrouter`/`stub`),
`LLM_MODEL`, `ANTHROPIC_API_KEY` / `OPENROUTER_API_KEY`.

## Observability

Logs are structured JSON on stdout. Every request gets a correlation
`request_id` (returned as the `X-Request-ID` response header and attached to
every log line for that request); clients may supply their own via the
`X-Request-ID` request header.

---

## Project structure

```
backend/
  Dockerfile             # shared image for API + worker
  app/
    main.py              # bootstrap: lifespan, CORS, request-id middleware
    config.py            # Pydantic Settings
    database.py          # Mongo client, collections, index setup
    logging_config.py    # structured JSON logging + request-id context
    worker.py            # Redis-driven ingestion worker
    routes/              # health, chat, documents, jobs, memory
    schemas/             # RequestPlan, ContextPolicy, ContextEvidence, MemoryContext, …
    services/            # routing, policy, retrieval, composition, LLM, ingestion,
                         #   storage, vectors, embeddings, preferences, knowledge, …
docker-compose.yml       # full stack: infra + backend + worker
```

---

## Roadmap

- **Phase 0 — Production cleanup** ✅ config, logging, request IDs, CORS, indexes
- **Phase 1 — Document pipeline** ✅ upload → MinIO → Redis → worker → extract → chunk → embed → Qdrant → summary
- **Phase 2 — Retrieval** ✅ Qdrant semantic search, document/page/chunk retrieval wired into the memory pipeline
- **Phase 3 — Real LLM** ✅ Anthropic/OpenRouter client with timeouts + retries; LLM thread summaries
- **Phase 4 — Memory** ✅ user preferences, knowledge memory, context-budget enforcement
- **Phase 5 — Streaming** ✅ Server-Sent Events (status events + token streaming)
- **Phase 6 — Deployment** ✅ full Docker Compose (Mongo, Redis, Qdrant, MinIO, backend, worker) + health checks

**Deferred (post-V1.5):** hybrid/BM25 + re-ranking, per-section summaries,
automatic knowledge extraction, HITL preference confirmation, auth &
multi-tenancy (replacing the `dev_user` placeholder), and a frontend.
