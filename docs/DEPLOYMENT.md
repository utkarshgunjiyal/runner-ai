# Deployment

> Phase 42A provides **production-capable builds and composition**. It does **not**
> perform a deployment — no cloud target is configured. This guide covers local,
> Docker, and production-like composition so a target can be chosen later.

## Prerequisites

- Docker + Docker Compose v2 (`docker compose version`), or
- Python 3.11 + Node 22 for a host run.

## 1. Local (host) run

```bash
cp .env.example .env            # fill in values (LLM key optional; stub works offline)

# Backend
cd backend
pip install -r requirements.txt && pip install pytest
uvicorn app.main:app --reload --port 8000

# Frontend (separate shell)
cd frontend
npm ci
npm run dev                     # http://localhost:5173 (proxies /agent to :8000)
```

## 2. Docker (local/demo stack)

```bash
cp .env.example .env            # optional; env_file is optional
docker compose up --build
```

Services & ports:

| Service   | URL                       | Notes                                   |
| --------- | ------------------------- | --------------------------------------- |
| frontend  | http://localhost:3000     | nginx SPA; proxies `/agent` to backend  |
| backend   | http://localhost:8000     | FastAPI (`/health`, `/health/ready`, …) |
| mongodb   | localhost:27017           | durable checkpoints (when enabled)      |
| redis     | localhost:6379            | job queue + rate limiting               |
| qdrant    | localhost:6333            | vector store                            |
| minio     | localhost:9000 / :9001    | object storage + console                |

`minio-init` creates the uploads bucket and exits.

## 3. Production-like composition

```bash
export CORS_ORIGINS=https://app.example.com
export MINIO_ROOT_USER=... MINIO_ROOT_PASSWORD=...
export ANTHROPIC_API_KEY=...            # or OPENROUTER_API_KEY
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

The prod override:
- keeps infra ports off the host (internal network only),
- enables rate limiting (Redis), metrics, Mongo checkpoints, real LLM,
- `restart: always`,
- requires `CORS_ORIGINS` (fails fast if unset).

**Before public exposure** you must also (see [SECURITY.md](./SECURITY.md)):
replace the dev auth stub with real authentication, front the stack with TLS,
and rotate all default credentials.

## Images

- **backend** (`backend/Dockerfile`): `python:3.11-slim`, non-root (`uid 10001`),
  `tini` init for graceful shutdown, `HEALTHCHECK` on `/health/live`,
  `uvicorn --proxy-headers --timeout-graceful-shutdown 20`.
- **frontend** (`frontend/Dockerfile`): multi-stage `node:22` build → `nginx:1.27`
  static serve, SPA fallback, `/agent` reverse-proxy (buffering off for SSE),
  backend URL set at runtime via `BACKEND_URL`.

Build only:

```bash
docker compose build
```

## Health checks (for load balancers / orchestrators)

- **Liveness**: `GET /health/live` → `200 {"status":"alive"}` (no dependencies).
- **Readiness**: `GET /health/ready` → `200` when Mongo/Redis/Qdrant/MinIO are
  reachable, else `503`. Use readiness to gate traffic.

## Rollback

Images are tagged (`runner-ai-backend`, `runner-ai-frontend`). To roll back:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d \
  --no-build backend@sha256:<previous-digest>
```

or re-deploy the previous git tag and rebuild. State lives in the named volumes
(`runner_mongo_data`, …) and survives image rollbacks; a schema-incompatible
rollback should restore a Mongo snapshot first. Checkpoints are forward-only —
a rollback does not need to migrate them (waiting runs simply expire).
