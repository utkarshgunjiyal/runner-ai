# Runbook

First-response guide for common operational situations. All commands assume the
Docker composition; adapt paths for a host run.

## Quick triage

```bash
docker compose ps                                  # what's up / restarting
curl -fsS localhost:8000/health/ready | jq         # which dependency is down
docker compose logs --tail=100 backend | jq -R 'fromjson? // .'   # structured logs
```

Filter one request across services by its correlation id:

```bash
docker compose logs backend | grep '"request_id":"<id>"'
```

## Symptom → action

### Backend returns 503 on `/health/ready`
A dependency is unreachable. The body names it (`{"dependencies":{"redis":"unavailable"}}`).
- Check that service: `docker compose ps <svc>`, `docker compose logs <svc>`.
- Restart it: `docker compose restart <svc>`.
- Liveness (`/health/live`) staying 200 means the process itself is fine.

### Clients get `429 Too Many Requests`
Rate limiting is working. Check `Retry-After`. If limits are too tight, raise
`RATE_LIMIT_{RUN,STREAM,RESUME}_PER_MINUTE` and restart the backend. Confirm
`RATE_LIMIT_BACKEND=redis` in multi-replica deployments (memory is per-process).

### SSE stream stalls / disconnects through a proxy
Ensure proxy buffering is **off** for `/agent/run/stream` (the bundled nginx
config does this). Heartbeats every `SSE_HEARTBEAT_SECONDS` keep idle streams
open; lower it if an aggressive proxy still closes them.

### A run "hangs" / high CPU after clients leave
Disconnects cancel background work automatically (Phase 42A). If you still see
orphaned load, confirm you are on this build and that the proxy actually closes
the upstream connection on client disconnect.

### LLM answers look like `[stub-llm] …`
No LLM key configured → the stub provider is active. Set `ANTHROPIC_API_KEY`
(or `OPENROUTER_API_KEY`) and `AGENT_USE_REAL_LLM=true`, then restart. **Never**
add LLM calls to health checks.

### Resume returns 404 / 409
- `404`: checkpoint unknown/expired → the run can no longer be resumed (start a
  new run). Ensure `AGENT_CHECKPOINT_BACKEND=mongo` for durability across restarts.
- `409`: the checkpoint was already resumed/cancelled (or a concurrent resume).
  The UI clears the checkpoint on 409 — expected.

### Mongo checkpoint growth
Waiting checkpoints accumulate in the `agent_checkpoints` collection. Add a TTL
index / periodic cleanup for old `resumed`/`cancelled` records if needed.

## Deploy / rollback

- Deploy: `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build`.
- Rollback: redeploy the previous image tag / git tag and rebuild. Named volumes
  persist state across image changes (see [DEPLOYMENT.md](./DEPLOYMENT.md)).

## Escalation

Capture before escalating: `docker compose ps`, the failing request's
`request_id` and the surrounding structured logs, `/health/ready` output, and
(if metrics enabled) a `/metrics` snapshot. These contain no secrets.
