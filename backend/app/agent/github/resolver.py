"""GitHub resource resolver (Phase 46.3.1).

The GitHub implementation of the provider-agnostic ``ResourceResolver``. It reuses
the existing deterministic parsing (``github/resources.py``) and trusted identity
(``github/identity.py``) — no duplicated parsing — and emits provider-neutral
``Resource`` objects tagged with their deterministic source. The argument builder
then consumes these; it never re-parses owners/repos/ids.

Sources this phase: the current request and the trusted connector identity, plus
prior repository context surfaced via ``execution_state`` (formalized into an
execution-state store in Phase 46.3.2). No LLM.
"""

from __future__ import annotations

from app.agent.github.identity import GithubIdentity
from app.agent.github.resources import resolve_resources
from app.agent.resources.models import (
    ResolutionContext,
    Resource,
    ResolvedResources,
    ResourceSource,
)

GITHUB_PROVIDER = "github"

# GithubResources.owner_source → deterministic ResourceSource.
_OWNER_SOURCE = {
    "explicit": ResourceSource.REQUEST,
    "connector_identity": ResourceSource.CONNECTOR_IDENTITY,
    "prior_context": ResourceSource.THREAD_STATE,
}


class GithubResourceResolver:
    """Resolves GitHub resources (owner/repo/issue_number/pull_number + scope)."""

    provider = GITHUB_PROVIDER

    def __init__(self, *, identity: GithubIdentity | None = None) -> None:
        self._identity = identity or GithubIdentity()

    def resolve(self, ctx: ResolutionContext) -> ResolvedResources:
        # Prior repository context (thread state) — the 46.3.2 store will populate
        # this; today it is surfaced under a provider-namespaced key.
        known = ctx.execution_state.get("github_active_repositories")
        known = known if isinstance(known, list) else None

        res = resolve_resources(
            ctx.user_request, identity=self._identity, known_repositories=known
        )

        resources: dict[str, Resource] = {}
        if res.owner:
            resources["owner"] = Resource(
                type="owner", value=res.owner, provider=self.provider,
                source=_OWNER_SOURCE.get(res.owner_source, ResourceSource.REQUEST),
            )
        if res.repo:
            resources["repo"] = Resource(
                type="repo", value=res.repo, provider=self.provider,
                source=ResourceSource.REQUEST,
            )
        if res.issue_number is not None:
            resources["issue_number"] = Resource(
                type="issue_number", value=res.issue_number, provider=self.provider,
                source=ResourceSource.REQUEST,
            )
        if res.pull_number is not None:
            resources["pull_number"] = Resource(
                type="pull_number", value=res.pull_number, provider=self.provider,
                source=ResourceSource.REQUEST,
            )

        ambiguous = {"owner": res.owner_candidates} if res.owner_candidates > 1 else {}
        flags = {"account_scoped": bool(res.account_scoped)}
        return ResolvedResources(
            provider=self.provider, resources=resources, ambiguous=ambiguous, flags=flags,
        )
