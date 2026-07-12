# Security

Security posture and the checklist that must be completed before public exposure.

## Must-do before production

1. **Authentication.** The API ships a **development auth stub**
   (`get_current_user` → `dev_user` in `app/routes/agent.py`). It must be replaced
   or wired to real authentication (the dependency is overridable) **before public
   deployment**. Until then, treat every endpoint as unauthenticated.

   **Phase 42B startup guard.** With `ENVIRONMENT=production`, the backend now
   **refuses to boot** while the dev stub is active unless `ALLOW_DEV_AUTH=true`
   is explicitly set — so a public deployment cannot *silently* authenticate
   everyone as `dev_user`. Options:
   - **Public multi-user:** wire real auth (override `get_current_user`) and leave
     `ALLOW_DEV_AUTH=false`.
   - **Private demo:** run `ENVIRONMENT=demo` (or set `ALLOW_DEV_AUTH=true`) **and**
     lock the edge with Caddy basic auth (`deploy/auth.conf`). The app auth stays
     explicitly development-only.

   Local development is unaffected (the guard only applies to
   `ENVIRONMENT=production`).
2. **CORS.** Set `CORS_ORIGINS` to explicit origins (never `*`). Credentialed
   requests are automatically disabled when `CORS_ORIGINS="*"`, so `*` cannot be
   combined with cookies.
3. **Cookies.** Auth uses HTTP-only cookies. In production set `Secure`,
   `HttpOnly`, and `SameSite=Lax` (or `None` + `Secure` for cross-origin). The
   frontend never stores tokens in `localStorage`.
4. **TLS.** Terminate TLS at your load balancer / ingress. The backend trusts
   `X-Forwarded-*` (`--proxy-headers`) only behind a trusted proxy.
5. **Credentials.** Rotate all defaults (`MINIO_ROOT_USER/PASSWORD`), set real LLM
   keys via environment/secret manager. No secrets are committed; `.env` is
   git-ignored and never baked into images.

## Built-in protections (Phase 42A)

- **Security headers** (`SECURITY_HEADERS_ENABLED=true`): `X-Content-Type-Options:
  nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, and a
  `Content-Security-Policy` (API default `default-src 'none'; frame-ancestors 'none'`;
  the SPA sets a stricter self-only CSP in nginx).
- **Request body size limit** (`MAX_REQUEST_BODY_BYTES`, 413 on exceed).
- **Rate limiting** (per-route, 429 + `Retry-After`).
- **Correlation ids** validated before use (no injection via `X-Request-ID`).
- **Safe errors**: health/readiness and server errors never leak stack traces,
  connection strings, credentials, or raw provider/transport exception text.
- **No secret exposure**: MCP `headers`/`environment`/`working_directory` never
  enter a `ToolSpec`, `RuntimeEvent`, metric label, or log line.
- **Metrics label guard**: high-cardinality/sensitive keys are dropped.

## Demo mode boundaries (Phase 42B)

`DEMO_MODE` wires a `DemoEvaluator` onto the **existing** answer-evaluator seam so
marked prompts reach a genuine HITL pause. It is safe by construction:

- **Off by default** and **refused in production** (the startup guard blocks
  `DEMO_MODE=true` when `ENVIRONMENT=production`).
- It never bypasses the runtime state machine, uses the existing planner/provider/
  tool interfaces, emits genuine `RuntimeEvent`s, and exercises the real
  checkpoint/resume path.
- The UI fabricates no events. Demo answers use the deterministic provider (marked
  as such); no fake business logic lives in React.
- Tests (`tests/agent/test_demo_evaluator.py`, `tests/deploy/test_startup_guard.py`)
  prove it is inert unless explicitly enabled and cannot activate in production.

## Thread/document scoping & connectors (Phase 43)

- **`user_id` from auth only.** Ownership always derives from the authenticated
  principal; it is never read from the request body.
- **`selected_document_ids` are hints, not authorization.** The backend
  revalidates every id against the thread's Mongo document set on both the initial
  request and on resume. An id the thread does not own is dropped/rejected — a
  client cannot widen its access by asserting ids.
- **No cross-thread / cross-user document access.** Documents and chunks are
  scoped by `thread_id` and `user_id`; retrieval filters Qdrant by `user_id` plus
  the validated document-id set, so one thread (or user) can never read another's
  documents.
- **Document candidates are safe metadata only.** The ambiguity picker exposes
  just `document_id`, `filename`, and `created_at` — no chunk text, no internals,
  no other users' data.
- **`credential_reference` is opaque.** A connector's `credential_reference` is a
  pointer, never a raw token; it is marked `repr=False` and is never logged,
  serialized into a `ToolSpec`/`RuntimeEvent`/metric label, or returned by the
  API.
- **Eligibility ≠ approval.** Connector eligibility only controls whether a
  capability is *visible* to the planner (existence + health + scopes). Write /
  external actions still stop for approval before execution via the existing
  policy/evaluator path.
- **Real OAuth / secret storage deferred.** There is **no** per-user OAuth, token
  acquisition/refresh, or secret storage today — only the metadata/status/
  eligibility boundary. See [`CONNECTORS.md`](./CONNECTORS.md).

## Stabilization hardening (Phase 44)

- **No accidental preference writes.** `save_user_preference` is a **write**, and
  it is gated behind an intent capability gate that requires **explicit**
  preference-save language ("remember that…", "from now on…", "save this
  preference"). Casual chat and persistence-test messages never reach the write —
  the tool is kept out of the planner's candidate set unless the intent qualifies,
  so a user cannot have a preference persisted without asking for it.
- **Safe storage error.** A MinIO / object-storage failure during upload returns a
  coded `document_storage_unavailable` (HTTP **503**) — never a raw stack trace,
  bucket name, or connection string. It joins the existing safe-error posture.
- **MinIO credentials from env, not hardcoded.** The docker-compose backend reads
  MinIO settings from the environment (`MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` /
  `MINIO_BUCKET` / `MINIO_SECURE`, defaulting to the local `minioadmin` dev creds)
  rather than baking credentials into the compose file — so a real deployment sets
  them via env/secret manager and rotates them (see "Credentials" above).
- **Ambiguity candidates stay safe metadata only.** The hardened Phase 44
  vague-reference policy still pauses through the unchanged Phase 43 contract: the
  document picker exposes only `document_id`, `filename`, and `created_at` — no
  chunk text, no internals, no other users' data.

## Data handling

- Prompts, document contents, and provider payloads are **not logged** by default
  (`LOG_SENSITIVE=false`). Keep it off in production.
- Runtime internals (`RunContext`, `FinalPrompt`, plan/evaluation internals) are
  never exposed via the API or the SSE stream — only safe, curated fields.

## Dependency risk (accepted, documented)

`npm audit` reports 5 advisories (esbuild / vite / vitest) that are **dev-only,
transitive** dependencies of the Vite dev server and Vitest UI. They are **not
present in the production build** — the production image serves pre-built static
assets via nginx and never runs the Vite dev server. Fixing them requires a major
`vite` 6 / `vitest` 3 upgrade (breaking), which is out of scope for an operational
hardening pass. **Accepted risk**: a developer must not run `npm run dev` on an
untrusted network. Re-evaluate on the next planned frontend tooling upgrade.

`datetime.utcnow()` (deprecated on Python 3.12+) remains in the **locked V1.5
services** (`app/services/*`); the V2 agent code (`app/agent/execution/executor.py`)
was made timezone-aware. On the current Python 3.11 runtime these do not warn;
migrate the V1.5 services when that layer is next revised.

## GitHub MCP connector (Phase 46.2)

The GitHub read-only MCP connector is a **deployment-scoped** integration, not
per-user OAuth. Security properties and boundaries:

- **Deployment-scoped identity.** The configured GitHub token belongs to the
  *deployment*, not to individual users. Every user of a deployment with GitHub
  enabled effectively shares that one account. **Multi-user production exposure is
  therefore unsafe** — access to a deployment with GitHub enabled must be
  restricted (e.g. localhost, single-tenant, or authenticated behind Caddy basic
  auth). Do not enable it on a shared, public, multi-user deployment.
- **Private repositories.** If the token can read private repos, their metadata is
  visible to anyone who can use the deployment. Prefer a **low-privilege,
  read-only** token — `public_repo`, a test account, or public-repo-only access.
  **Never** grant write/admin scopes.
- **Connector identity metadata (Phase 46.2.6).** To scope account requests ("my
  repositories") the connector resolves the authenticated **login/owner** — a
  **public GitHub handle**, not a secret — from a trusted source only: a
  best-effort `get_me` MCP response, else the validated `GITHUB_MCP_OWNER`
  deployment setting. It is never inferred from arbitrary conversation text, and
  no token or private authentication field is read or exposed. Like the rest of
  the connector this identity is **deployment-scoped, not per-user OAuth** — one
  shared identity for the whole deployment.
- **No host Docker socket (Phase 46.2.1).** The recommended transport is the
  official **remote Streamable HTTP** endpoint (`GITHUB_MCP_TRANSPORT=http`,
  default), reached over outbound HTTPS. Runner.ai therefore **never mounts
  `/var/run/docker.sock`, never installs a Docker CLI in the backend, never runs a
  privileged container, and never uses Docker-in-Docker** — mounting the host
  socket would grant effective host root. The optional `stdio` mode runs a local
  Docker process and is a developer-only mode for a host that already has Docker.
- **Secret handling.** The token is read from the environment only. In http mode it
  is sent only in the `Authorization: Bearer` header (never in the URL); in stdio
  mode only in the process environment. It is excluded from `repr`/`str`, from
  `MCPServerConfig.public_metadata()` (which omits `url` and `headers`), from every
  `ToolSpec`/`RuntimeEvent`, from adapter results and errors (safe, vendor-free
  messages only), and from the `/integrations` API. It is never committed
  (`.env.example` has placeholders; `.gitignore` excludes `.env`), printed, or
  logged. The optional live-verification script never prints it.
- **Read-only enforcement.** Two independent layers: the server is launched
  `--read-only`, and discovery registers only an explicit read-only **allowlist**
  (`MCPServerConfig.tool_allowlist`). Write/admin tools can never be registered,
  become eligible, reach the planner, or be offered to the LLM.
- **Fail-safe.** With GitHub disabled/misconfigured, the connector is "Not
  configured" and the rest of Runner.ai is unaffected; a connection failure never
  crashes startup and never falls back to document retrieval for GitHub questions.

See [GITHUB_MCP.md](./GITHUB_MCP.md) for setup, tools, and limitations.

## Reporting

Treat this as a demo/interview project; there is no formal disclosure process.
For a real deployment, add a `SECURITY.md` disclosure policy and a security
contact.
