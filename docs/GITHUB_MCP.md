# GitHub Read-Only MCP Connector (Phase 46.2)

Runner.ai connects to a **real GitHub account** through the existing MCP
architecture and proves **read-only** GitHub tool execution end-to-end. No direct
GitHub REST calls are made — everything goes through the MCP server, adapter, and
unified capability registry.

```
request → interpretation → connector eligibility → MCP discovery →
capability retrieval → direct/planner runtime → MCP tool adapter →
GitHub MCP server → GitHub API → normalized result → grounded answer
```

## Selected server (pinned)

- **Official GitHub MCP server** — `github/github-mcp-server`
  (<https://github.com/github/github-mcp-server>).
- Transport: **stdio**, run via the published container image.
- Pinned image (override with `GITHUB_MCP_IMAGE`): **`ghcr.io/github/github-mcp-server:v0.6.0`**
  — never a floating `:latest`. **Confirm the tag against the release page for your
  deployment** and run the verification script below.
- Launched read-only: `stdio --read-only` with
  `GITHUB_TOOLSETS=repos,issues,pull_requests`.

## Authentication (development / deployment-scoped)

Configured at the **deployment/server level only** via environment variables:

| Variable | Purpose |
| --- | --- |
| `GITHUB_MCP_ENABLED` | `true` to enable (default `false`). |
| `GITHUB_MCP_TOKEN` **or** `GITHUB_PERSONAL_ACCESS_TOKEN` | The GitHub token (a **secret**). |
| `GITHUB_MCP_IMAGE` | Pinned server image tag. |
| `GITHUB_MCP_TOOLSETS` | `repos,issues,pull_requests`. |
| `GITHUB_MCP_TIMEOUT_SECONDS` | Per-call timeout (default 45). |

The token is placed **only** in the server process environment — never on the
command line, in a `ToolSpec`, in tool metadata, in an API response, or in a log/
trace/error. Configuration **fails safe**: with `GITHUB_MCP_ENABLED=false` or no
token, GitHub is simply "Not configured" and the rest of Runner.ai is unaffected.

**Minimum permissions.** Use a **low-privilege, read-only** token
(`public_repo`, or `repo` scoped to read for private repos). Never request write/
admin scopes. Prefer a test account or public-repo-only access.

> **This is NOT per-user OAuth.** The configured identity is shared by the whole
> deployment. See [SECURITY.md](./SECURITY.md#github-mcp-connector-phase-462).

## Enabled read-only tools (allowlist)

Discovery registers **only** these tool names (the real official-server names);
every other advertised tool — including all write tools — is excluded *before* it
can become an eligible capability:

| Capability | Tool | Purpose |
| --- | --- | --- |
| List / search repositories | `search_repositories` | list the account's repositories |
| List issues | `list_issues` | issues in a repository |
| Get issue | `issue_read` | one issue by number |
| List pull requests | `list_pull_requests` | PRs in a repository |
| Get pull request | `pull_request_read` | one PR by number |
| Search issues (optional) | `search_issues` | search issues |

### Blocked write/admin tools (never eligible)

`issue_write`, `add_issue_comment`, `sub_issue_write`, `create_pull_request`,
`update_pull_request`, `merge_pull_request`, `pull_request_review_write`,
`add_comment_to_pending_review`, `enable_pr_auto_merge`, `disable_pr_auto_merge`,
`request_copilot_review`, `push_files`, `create_or_update_file`, `delete_file`,
`create_branch`, `create_repository`, `fork_repository`, `run_secret_scanning`,
`actions_run_trigger`, `resolve_review_thread`, `unresolve_review_thread`.

The allowlist is enforced at discovery in `MCPRegistryManager` (via
`MCPServerConfig.tool_allowlist`), so a write tool can never be registered, never
become eligible, and never reach the planner or the LLM — even if the server
advertises it.

## Runtime lifecycle

**Startup** (`app/main.py`): build the pinned GitHub `MCPServerConfig` → register
it with the MCP registry manager → discover tools (best-effort; a failure is
caught and reported as a status, never crashes startup) → the read-only allowlist
filters registration → discovered read tools are **enriched** into rich
`ToolSpec`s → the deployment GitHub **connector state** is derived from the real
health. **Shutdown**: the connection manager closes the MCP session/process.

A GitHub failure never blocks startup; document and chat flows keep working.

## Eligibility, execution, and grounding

- **Eligibility.** A `github` server id yields a `github` provider tag, so the
  connector-eligibility layer gates these tools: they are eligible **only** when
  the GitHub connector is CONNECTED. When GitHub is unavailable, the tools are
  filtered out **before planning** (on both the planner and direct paths) — the
  planner never sees them and the LLM is never offered them.
- **Execution.** `MCPAdapter` runs the tool through the injected `MCPClient` with a
  bounded timeout + retry; failures map onto the existing recovery taxonomy with a
  safe, vendor-free message (no token, no raw exception text).
- **Normalization.** A per-server normalizer turns the raw MCP payload into stable
  **Repository / Issue / PullRequest** structures with **bounded body excerpts** and
  whitelisted fields only — then a grounded, human-readable evidence block. The
  final answer is built **only** from this normalized data (never a raw payload).

## Status API + frontend

`GET /integrations` (and `POST /integrations/refresh`) return safe statuses:
GitHub (`not_configured` / `connecting` / `connected` / `degraded` / `auth_failed`
/ `unavailable`) with its enabled read capabilities, Gmail (`Coming next`), and the
MCP runtime. **No token or secret** ever appears. The frontend Integrations panel
renders this live status with a Refresh action and **no token input**.

## Limitations

- **No per-user OAuth** — deployment-scoped shared identity (restrict access).
- **Read-only** — no writes this phase (Gmail/calendar out of scope).
- Tool names/allowlist target the pinned server version; **confirm** them for your
  release via the verification script.
- Live GitHub verification requires Docker + a token (opt-in script below); it is
  **not** part of the automated test suite.

## Verification

Automated (fake-MCP, no network):

```bash
cd backend && python -m pytest tests/agent/test_github_connector.py \
  tests/agent/test_github_mcp_integration.py tests/agent/test_github_runtime.py \
  tests/api/test_integrations.py
```

Opt-in **live** (real GitHub reads; never in CI; never prints the token; no writes):

```bash
GITHUB_MCP_ENABLED=true GITHUB_MCP_TOKEN=ghp_xxx \
  [GITHUB_MCP_IMAGE=ghcr.io/github/github-mcp-server:vX.Y.Z] \
  [GITHUB_TEST_REPO=owner/name] \
  ./scripts/verify-github-mcp.sh
```

## Troubleshooting

| Symptom | Likely cause / action |
| --- | --- |
| Status `Not configured` | `GITHUB_MCP_ENABLED` not `true`, or no token. |
| Status `Authentication failed` | Invalid/expired token, or missing read scope. |
| Status `Unavailable` | Docker not running, image tag wrong, or network blocked. |
| `could not pull <image>` | Confirm the pinned tag exists; set `GITHUB_MCP_IMAGE`. |
| GitHub request answered from documents | Should not happen — GitHub tools are excluded when unavailable; file a bug. |
