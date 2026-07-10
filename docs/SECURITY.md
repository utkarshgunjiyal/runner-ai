# Security

Security posture and the checklist that must be completed before public exposure.

## Must-do before production

1. **Authentication.** The API ships a **development auth stub**
   (`get_current_user` → `dev_user` in `app/routes/agent.py`). It must be replaced
   or wired to real authentication (the dependency is overridable) **before public
   deployment**. Until then, treat every endpoint as unauthenticated.
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

## Reporting

Treat this as a demo/interview project; there is no formal disclosure process.
For a real deployment, add a `SECURITY.md` disclosure policy and a security
contact.
