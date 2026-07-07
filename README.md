# Runner.ai

A context-aware, document-aware AI assistant. Runner.ai routes each user
request through an intent classifier, selects a retrieval policy for that
intent, assembles priority-ordered evidence (recent messages, thread
summaries, and — as later phases land — document chunks, summaries, and
long-term memory), and generates a grounded answer.

> **Status:** V1.5 in progress. The request → routing → policy → context →
> persistence pipeline is implemented and running. The document ingestion,
> retrieval, real LLM, memory, and streaming layers are being built out in
> phases (see [Roadmap](#roadmap)).

---

## Architecture

```
POST /chat/ask
  → thread + summary bootstrap        (thread_service, thread_summary_service)
  → atomic seq allocation + persist   (thread_service, message_service)
  → intent classification             (behavior_router  → RequestPlan)
  → retrieval policy selection        (context_policy_service → ContextPolicy)
  → memory retrieval                  (memory_retrieval_service → MemoryContext)
  → priority-ordered context assembly (context_composer)
  → answer generation                 (llm_provider)
  → persist answer + maybe summarize  (summary_queue_service)
```

The design is **schema-driven**: `RequestPlan`, `ContextPolicy`,
`ContextEvidence`, and `MemoryContext` (in `backend/app/schemas/`) form the
stable contract that every service reads and writes, so new evidence sources
plug in without reshaping the pipeline.

### Tech stack

| Concern            | Technology                        |
| ------------------ | --------------------------------- |
| API                | FastAPI (async) + Uvicorn         |
| Data store         | MongoDB (Motor async driver)      |
| Config             | Pydantic Settings                 |
| Object storage     | MinIO *(Phase 1)*                 |
| Job queue          | Redis *(Phase 1)*                 |
| Vector store       | Qdrant *(Phase 1/2)*              |
| LLM / embeddings   | Claude / OpenRouter *(Phase 2/3)* |

---

## Getting started

### Prerequisites
- Python 3.11+
- Docker (for MongoDB via `docker-compose`)

### 1. Configure environment
```bash
cp .env.example .env
# edit .env — at minimum set MONGO_URL
```

### 2. Start MongoDB
```bash
docker compose up -d mongodb
```

### 3. Install and run the backend
```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

The API is now at `http://localhost:8000` (interactive docs at `/docs`).

### Quick check
```bash
curl http://localhost:8000/health
curl -X POST http://localhost:8000/chat/ask \
  -H 'Content-Type: application/json' \
  -d '{"question": "Hello, what can you do?"}'
```

---

## Configuration

All settings are read from environment variables / `.env` via Pydantic
Settings (`backend/app/config.py`). See [`.env.example`](.env.example) for the
full list. Required: `MONGO_URL`.

## Observability

Logs are emitted as structured JSON on stdout. Every request is assigned a
correlation `request_id` (returned in the `X-Request-ID` response header and
attached to every log line for that request); clients may supply their own via
the `X-Request-ID` request header.

---

## Project structure

```
backend/app/
  main.py              # app bootstrap: lifespan, CORS, request-id middleware
  config.py            # Pydantic Settings
  database.py          # Mongo client, collections, index setup
  logging_config.py    # structured JSON logging + request-id context
  routes/              # health, chat
  schemas/             # RequestPlan, ContextPolicy, ContextEvidence, MemoryContext, Chat
  services/            # routing, policy, retrieval, composition, persistence, summaries
```

---

## Roadmap

- **Phase 0 — Production cleanup** ✅ config, logging, request IDs, CORS, indexes
- **Phase 1 — Document pipeline** — upload → MinIO → Redis job → worker → extract → chunk → embed → Qdrant → summary
- **Phase 2 — Retrieval** — Qdrant + hybrid retrieval, re-ranking, document/page/section retrieval
- **Phase 3 — Real LLM** — Claude/OpenRouter client with timeouts, retries, streaming-ready
- **Phase 4 — Memory** — user preferences, knowledge memory, thread summaries, context-budget enforcement
- **Phase 5 — Streaming** — Server-Sent Events (status events + token streaming)
- **Phase 6 — Deployment** — full Docker Compose (Mongo, Redis, Qdrant, MinIO, backend, worker), health checks
