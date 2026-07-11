# Connectors

How Runner.ai V2 represents a user's authenticated relationship with an external
provider (GitHub, Gmail, Calendar), and how **capability eligibility** stops the
planner from ever seeing a tool the user can't actually use. This is the connector
half of Phase 43.

Companion: [`THREAD_DOCUMENT_MODEL.md`](./THREAD_DOCUMENT_MODEL.md) (the
thread/document half).

> **Read this first — honesty note.** There is **no real per-user OAuth today**,
> no token acquisition or refresh, and no secret storage. Phase 43 implements the
> **metadata / status / eligibility boundary** only. Credentials are referenced
> by an opaque `credential_reference` that points at *where a secret would live*,
> not at a secret. Real OAuth and secret storage are explicitly deferred — see
> [Deferred / not yet implemented](#deferred--not-yet-implemented).

---

## MCP server vs. connector (the critical distinction)

These are different concepts and must not be conflated:

| | **MCP server** | **Connector** |
|---|---|---|
| What it is | A source that *exposes/executes tool definitions* | *One user's* authenticated relationship with a provider |
| Credentials | **Static, server-wide** (`MCPServerConfig.environment` / `.headers`) | Per-user, referenced by an opaque `credential_reference` |
| Scope | Shared across all users of that server | Belongs to exactly one `user_id` |
| Lives in | `app/agent/mcp/` | `app/agent/connectors/` |
| Analogy | A shared machine with a fixed service account | *Your* login to GitHub |

An MCP server's credentials are a deployment-level, server-wide secret shared by
everyone who uses that server. A connector is personal: it records that *this
particular user* has authorized *this particular provider*, with which scopes,
and whether that relationship is currently healthy. Capability eligibility (below)
is about the **connector**, not the MCP server.

---

## The connector data model

A `ConnectorRecord` (`app/agent/connectors/models.py`) represents one user's
provider relationship:

| Field | Meaning |
|---|---|
| `provider` | `github` / `gmail` / `calendar` |
| `status` | `connected` (healthy, capabilities eligible) / `disconnected` / … |
| `scopes` | Granted scopes, e.g. `repo:read`, `gmail:read`, `gmail:send` |
| `credential_reference` | **Opaque pointer** to where a secret lives — never a raw token. Marked `repr=False`. |
| `account_display_name` | Human label for the connected account (safe to show) |
| `last_health_check` / health | When/whether the relationship was last verified |

Helper methods on the record drive eligibility: `is_healthy` (status is
`connected`) and `has_scopes(required)` (granted scopes ⊇ required scopes).

The shipped registry (`app/agent/connectors/registry.py`) is **in-memory**. It
holds records/status/scopes only; it stores no secrets.

---

## Capability eligibility

`app/agent/connectors/eligibility.py` filters the capability catalog so the
**planner never sees a tool it cannot actually run**. Each tool derives a
deterministic `CapabilityRequirement` from its tags:

- a `provider:<name>` tag → the connector it needs;
- `scope:<name>` tags → the required scopes (e.g. `scope:repo:write`);
- a write/external tag → the action requires approval downstream.

A capability is **eligible** only when the user's connector for that provider:

1. **exists**, and
2. is **healthy** (`status == connected`), and
3. **grants the required scopes**.

If any check fails, the capability is filtered out with a reason
(`connector_disconnected`, `insufficient_scope`, …) and never enters the
planner's view.

### Example — GitHub disconnected

The user has no GitHub connector, or its status is `disconnected`. All
`provider:github` capabilities are filtered out (`reason=connector_disconnected`).
The planner cannot plan a GitHub action because, as far as it can see, no GitHub
tool exists. The user sees no GitHub capability rather than a plan that fails at
execution time.

### Example — Gmail read but not send

The user's Gmail connector is `connected` with scope `gmail:read` but **not**
`gmail:send`. A read capability (`provider:gmail`, `scope:gmail:read`) is
**eligible** — the planner may use it. A send capability
(`provider:gmail`, `scope:gmail:send`) is **filtered out**
(`reason=insufficient_scope`) because the required scope was never granted. The
user can search/read mail but the send tool is simply absent from the catalog.

---

## Read vs. write policy and approval-before-execution

Eligibility filtering is a **visibility** control, not the safety control for
side effects. Write and external actions remain governed by the existing
policy/evaluator path:

- **Eligibility** decides whether a capability is even *offered* to the planner.
- **Policy / approval** decides whether an *offered* write/external action may
  actually *execute*. These actions are approval-gated: the run pauses
  `WAITING_FOR_APPROVAL`, persists a checkpoint, and only proceeds after an
  explicit approve on resume.

So a Gmail-send capability that *is* eligible (scope granted, connector healthy)
still cannot fire silently — it stops for approval before execution, exactly like
any other side-effecting action in the runtime. Eligibility and approval are
layered: eligibility removes tools the user *can't* use; approval guards tools the
user *can* use but whose effects need a human yes.

---

## Deferred / not yet implemented

Phase 43 ships the connector **boundary** only. The following are deliberately
**not** implemented and are deferred:

- **Real OAuth.** There is **no** per-user OAuth flow. Nothing acquires an
  authorization code, exchanges it for a token, or establishes a real provider
  session. Connector records are metadata describing a relationship, not proof of
  one.
- **Token acquisition & refresh.** No tokens are obtained, stored, refreshed, or
  rotated. `status`/health are set as metadata, not derived from a live token.
- **Secret storage.** No secret manager or vault integration. The
  `credential_reference` is an **opaque pointer** to *where a secret would live* —
  it is not a token and dereferences to nothing today.
- **Persistent connector registry.** The shipped registry is **in-memory**; it is
  not backed by a database and does not survive a restart.

What *is* real: the connector **data model**, the **eligibility** filter
(existence + health + scopes), the **MCP-server-vs-connector** separation, and the
guarantee that write/external actions stay **approval-gated**. When real OAuth and
secret storage are added later, they slot in behind the same
`credential_reference` boundary without changing the eligibility contract.

MCP itself also remains **disabled by default** with zero server configs (see
`ARCHITECTURE_WALKTHROUGH.md` §12).
