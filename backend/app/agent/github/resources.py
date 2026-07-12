"""Deterministic GitHub resource resolution (Phase 46.2.6).

Turns a natural-language GitHub reference into a structured resource — owner,
repository, issue number, pull-request number, whether the request is scoped to
the authenticated account ("my") — using only deterministic parsing plus a
trusted connector identity. No LLM. It never guesses an owner or repository and
never invents an issue/PR number.

Resolution precedence:
- an explicit ``owner/repo`` in the text always wins;
- a bare repository name resolves its owner from the trusted connector identity
  (or from unambiguous prior thread context), never from arbitrary text;
- "my" maps to the authenticated identity;
- ambiguity (a bare repo name matching several known owners) is reported, never
  silently resolved.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict

from app.agent.github.identity import GithubIdentity, validate_owner

# owner/repo slug: owner is a GitHub login; repo allows ".", "_", "-".
_OWNER_REPO_RE = re.compile(
    r"\b([A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38})/([A-Za-z0-9._-]{1,100})\b"
)
# "issue 12", "issue #12", "#12"
_ISSUE_RE = re.compile(r"\bissue\s+#?(\d+)\b", re.IGNORECASE)
# "pull request 5", "pull 5", "PR 5", "PR #5"
_PR_RE = re.compile(r"\b(?:pull\s+request|pull|pr)\s+#?(\d+)\b", re.IGNORECASE)
_HASH_NUM_RE = re.compile(r"(?<![\w/])#(\d+)\b")

# Words that are never a repository name when parsed positionally.
_STOPWORDS = {
    "my", "the", "a", "an", "all", "open", "closed", "issue", "issues", "pull",
    "requests", "request", "pr", "prs", "repository", "repositories", "repo",
    "repos", "github", "in", "for", "of", "on", "show", "list", "find", "get",
    "details", "detail", "about", "search", "and", "or", "me",
}
# Positional cues that a following/preceding token is a repository name.
_REPO_AFTER = ("in", "repository", "repo", "named", "called")


class GithubResources(BaseModel):
    """Structured resources parsed from a GitHub request (all optional)."""

    model_config = ConfigDict(frozen=True)

    owner: str | None = None
    repo: str | None = None
    issue_number: int | None = None
    pull_number: int | None = None
    #: True when the request is scoped to the authenticated account ("my", "@me").
    account_scoped: bool = False
    #: Where the owner came from: "explicit" | "connector_identity" |
    #: "prior_context" | None.
    owner_source: str | None = None
    #: >1 when a bare repo name matched several known owners (ambiguous).
    owner_candidates: int = 0

    def resolved_types(self) -> list[str]:
        out = []
        if self.owner:
            out.append("owner")
        if self.repo:
            out.append("repo")
        if self.issue_number is not None:
            out.append("issue_number")
        if self.pull_number is not None:
            out.append("pull_number")
        if self.account_scoped:
            out.append("account_scope")
        return out


def _first_positive_int(match) -> int | None:
    if not match:
        return None
    try:
        value = int(match.group(1))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _clean_token(tok: str) -> str:
    """Strip trailing/leading sentence punctuation from a token (keeps internal
    dots so ``repo.js``-style names survive)."""
    return tok.strip(".,!?;:'\"()")


def _extract_repo_name(text: str, explicit_repo: str | None) -> str | None:
    """Best-effort bare repository name (no owner) from positional cues."""
    if explicit_repo:
        return None
    tokens = [_clean_token(t) for t in re.findall(r"[A-Za-z0-9._/-]+", text)]
    lowered = [t.lower() for t in tokens]

    def _valid(tok: str) -> bool:
        return (
            bool(tok)
            and "/" not in tok
            and tok.lower() not in _STOPWORDS
            and not tok.isdigit()
            and bool(re.match(r"^[A-Za-z0-9._-]{1,100}$", tok))
        )

    # After an explicit cue word ("in runner-ai", "repository runner-ai").
    for i, low in enumerate(lowered):
        if low in _REPO_AFTER and i + 1 < len(tokens) and _valid(tokens[i + 1]):
            return tokens[i + 1]
    # "my <name> repository/repo" → the token before the cue.
    for i, low in enumerate(lowered):
        if low in ("repository", "repo") and i - 1 >= 0 and _valid(tokens[i - 1]):
            return tokens[i - 1]
    return None


def resolve_resources(
    text: str,
    *,
    identity: GithubIdentity | None = None,
    known_repositories: list[dict] | None = None,
) -> GithubResources:
    """Deterministically resolve GitHub resources from ``text``.

    ``known_repositories`` (optional, thread-scoped safe context) is a list of
    ``{"owner": ..., "repo": ...}`` used only to (a) resolve a bare repo name's
    owner when unambiguous, or (b) report ambiguity when several owners share the
    name. It is never fetched here and never drawn from arbitrary chat text.
    """
    raw = text or ""
    account_scoped = bool(re.search(r"\bmy\b", raw, re.IGNORECASE)) or "@me" in raw.lower()

    owner = repo = None
    owner_source = None
    m = _OWNER_REPO_RE.search(raw)
    if m:
        owner = validate_owner(m.group(1))
        repo = m.group(2)
        if owner:
            owner_source = "explicit"

    issue_number = _first_positive_int(_ISSUE_RE.search(raw))
    pull_number = _first_positive_int(_PR_RE.search(raw))
    if issue_number is None and pull_number is None:
        # A bare "#12" is treated as an issue reference by default.
        issue_number = _first_positive_int(_HASH_NUM_RE.search(raw))

    owner_candidates = 0
    if repo is None:
        repo = _extract_repo_name(raw, explicit_repo=repo)

    # Resolve a bare repo name's owner from trusted sources only.
    if repo is not None and owner is None:
        matches = [
            r for r in (known_repositories or [])
            if isinstance(r, dict) and str(r.get("repo", "")).lower() == repo.lower()
        ]
        distinct_owners = {str(r.get("owner")) for r in matches if r.get("owner")}
        if len(distinct_owners) > 1:
            owner_candidates = len(distinct_owners)  # ambiguous — do not guess
        elif len(distinct_owners) == 1:
            owner = validate_owner(next(iter(distinct_owners)))
            owner_source = "prior_context" if owner else None
        elif identity is not None and identity.known:
            owner = identity.owner
            owner_source = "connector_identity"

    # "my" with no repo → account-scoped owner is the identity (for listings).
    if owner is None and account_scoped and identity is not None and identity.known and repo is None:
        owner = identity.owner
        owner_source = "connector_identity"

    return GithubResources(
        owner=owner, repo=repo, issue_number=issue_number, pull_number=pull_number,
        account_scoped=account_scoped, owner_source=owner_source,
        owner_candidates=owner_candidates,
    )
