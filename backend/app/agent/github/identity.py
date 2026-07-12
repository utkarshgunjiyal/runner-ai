"""Trusted, deployment-scoped GitHub connector identity (Phase 46.2.6).

Account-scoped GitHub requests ("my repositories", "my runner-ai") need the
authenticated owner/login. This resolves it SAFELY from a trusted source and
never from arbitrary conversation text:

1. a best-effort ``get_me`` MCP response (the official server's authenticated-user
   tool), when a resolver is provided, else
2. an explicit deployment setting ``GITHUB_MCP_OWNER`` (validated).

This is **deployment-scoped**, not per-user OAuth: one configured identity is
shared by the whole deployment (see docs/SECURITY.md). The login is a public
handle — never a token, header, or private field. Nothing here logs or returns a
secret.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict

# GitHub login rules: 1–39 chars, alphanumeric or single hyphens, no leading/
# trailing hyphen. We validate defensively so a bad deployment value can never be
# projected into a search query or an ``owner`` argument.
_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")


def validate_owner(value: str | None) -> str | None:
    """Return a validated GitHub login/owner, or ``None`` if invalid/empty."""
    if not value:
        return None
    candidate = str(value).strip()
    return candidate if _LOGIN_RE.match(candidate) else None


class GithubIdentity(BaseModel):
    """Safe, secret-free authenticated-account identity for argument scoping."""

    model_config = ConfigDict(frozen=True)

    #: The authenticated GitHub login/owner (public handle), or None if unknown.
    owner: str | None = None
    #: Where ``owner`` came from: "get_me" | "deployment_setting" | "unknown".
    source: str = "unknown"

    @property
    def known(self) -> bool:
        return bool(self.owner)

    def public_view(self) -> dict:
        return {"owner": self.owner, "source": self.source, "known": self.known}


def _login_from_get_me(payload) -> str | None:
    """Extract a login from a get_me-style response, tolerating shapes.

    Accepts a dict with ``login`` (or ``user``/``owner`` sub-dicts), or an
    ``MCPToolCallResult``-like object exposing ``structured_content``. Only the
    public ``login`` handle is read; no other field is touched.
    """
    structured = getattr(payload, "structured_content", None)
    data = structured if isinstance(structured, dict) else payload
    if not isinstance(data, dict):
        return None
    for key in ("login", "username"):
        if isinstance(data.get(key), str):
            return validate_owner(data[key])
    for nested in ("user", "owner", "viewer"):
        sub = data.get(nested)
        if isinstance(sub, dict) and isinstance(sub.get("login"), str):
            return validate_owner(sub["login"])
    return None


async def resolve_github_identity(
    *, configured_owner: str | None = None, get_me_fn=None,
) -> GithubIdentity:
    """Resolve the trusted GitHub identity (best-effort, never raises).

    ``get_me_fn`` is an optional ``async () -> payload`` returning a get_me-style
    response (the composition root wires it to a real MCP call). A trusted remote
    identity wins; otherwise the validated deployment setting is used; otherwise
    the identity is unknown (account-scoped requests then clarify rather than
    guess). Failures degrade to the configured owner — a get_me error never blocks
    startup.
    """
    if get_me_fn is not None:
        try:
            login = _login_from_get_me(await get_me_fn())
            if login:
                return GithubIdentity(owner=login, source="get_me")
        except Exception:  # noqa: BLE001 - identity resolution must never raise
            pass
    validated = validate_owner(configured_owner)
    if validated:
        return GithubIdentity(owner=validated, source="deployment_setting")
    return GithubIdentity(owner=None, source="unknown")
