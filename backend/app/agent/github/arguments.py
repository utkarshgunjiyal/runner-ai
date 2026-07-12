"""GitHub tool-argument construction (Phase 46.2.6; layered in Phase 46.3.1).

Shapes ALREADY-RESOLVED GitHub resources (from ``GithubResourceResolver``) onto the
selected GitHub MCP tool's discovered ``input_schema``. It consumes
``ResolvedResources`` and never parses owners, repository names, or issue/PR numbers
itself — resolution is the resolver's job. Deterministic (no LLM): it maps the
operation off the discovered tool name, fills only fields the schema declares
(snake/camel alias-aware), scopes account requests with GitHub search syntax
(``user:@me`` / ``user:<login>``), validates required resources, and returns a
structured *missing* / *ambiguous* result instead of ever emitting a global,
unscoped, or guessed query.

Internal orchestration fields (user_id/thread_id/run_id/request_id) are never
produced here; only declared tool arguments are (composes with the Phase 46.2.4
adapter projection).
"""

from __future__ import annotations

import re

from app.agent.models.tool_spec import ToolSpec
from app.agent.resources.models import ResolvedResources
from app.agent.runtime.arguments import ArgumentBuildResult

GITHUB_PROVIDER = "github"

# Field-name aliases: the official server has used both snake_case and camelCase
# across versions. We set the value under whichever the discovered schema declares.
_OWNER = ("owner",)
_REPO = ("repo", "repository")
_ISSUE_NUM = ("issue_number", "issueNumber")
_PULL_NUM = ("pull_number", "pullNumber")
_STATE = ("state",)
_QUERY = ("query", "q")
_METHOD = ("method",)

_STATE_RE = re.compile(r"\b(open|closed|all|merged)\b", re.IGNORECASE)
# Filler stripped from free-text search terms so a search isn't polluted.
_SEARCH_FILLER = {
    "search", "find", "my", "the", "a", "an", "for", "in", "github", "issue",
    "issues", "repository", "repositories", "repo", "repos", "all", "list",
    "show", "me", "of",
}


def _tool_name(tool: ToolSpec) -> str | None:
    ref = getattr(tool, "handler_ref", None)
    if isinstance(ref, str) and ref.startswith("mcp:"):
        parts = ref.split(":", 2)
        if len(parts) == 3:
            return parts[2]
    return None


def _schema_props(tool: ToolSpec) -> dict:
    schema = getattr(tool, "input_schema", None)
    props = schema.get("properties") if isinstance(schema, dict) else None
    return props if isinstance(props, dict) else {}


def _schema_required(tool: ToolSpec) -> list[str]:
    schema = getattr(tool, "input_schema", None)
    req = schema.get("required") if isinstance(schema, dict) else None
    return [str(r) for r in req] if isinstance(req, list) else []


def _pick(props: dict, aliases: tuple[str, ...]) -> str | None:
    """First alias the schema declares; or the first alias if the schema is open."""
    for alias in aliases:
        if alias in props:
            return alias
    return None if props else aliases[0]


def _search_terms(text: str) -> str:
    tokens = [t.strip(".,!?;:'\"()") for t in re.findall(r"[A-Za-z0-9._-]+", text or "")]
    kept = [t for t in tokens if t and t.lower() not in _SEARCH_FILLER and not t.isdigit()]
    return " ".join(kept).strip()


class GithubArgumentBuilder:
    """Deterministic GitHub argument builder — consumes resolved resources."""

    provider = GITHUB_PROVIDER

    def build(
        self, tool: ToolSpec, resolved: ResolvedResources, *,
        planner_args: dict | None = None, request_text: str = "",
    ) -> ArgumentBuildResult:
        planner_args = planner_args or {}
        name = _tool_name(tool)

        if resolved.ambiguous.get("owner", 0) > 1:
            return ArgumentBuildResult.build_ambiguous(
                "owner", resolved.ambiguous["owner"],
                resource_summary={"operation": name, "reason": "multiple_owner_matches"},
            )

        props = _schema_props(tool)
        req = _schema_required(tool)
        owner = planner_args.get("owner") or resolved.get("owner")
        repo = planner_args.get("repo") or planner_args.get("repository") or resolved.get("repo")
        account_scoped = resolved.flag("account_scoped")
        owner_source = resolved.source_of("owner")
        owner_source = owner_source.value if owner_source is not None else None

        if name == "search_repositories":
            return self._search_repositories(props, req, owner, repo, account_scoped, request_text, planner_args, owner_source)
        if name == "search_issues":
            return self._search_issues(props, req, owner, account_scoped, request_text, planner_args)
        if name in ("list_issues", "list_pull_requests"):
            return self._list_repo_scoped(name, props, req, owner, repo, request_text, planner_args, owner_source)
        if name == "issue_read":
            number = planner_args.get("issue_number") or resolved.get("issue_number")
            return self._read_numbered(props, req, owner, repo, "issue", _ISSUE_NUM, number, planner_args, owner_source)
        if name == "pull_request_read":
            number = planner_args.get("pull_number") or resolved.get("pull_number")
            return self._read_numbered(props, req, owner, repo, "pull", _PULL_NUM, number, planner_args, owner_source)

        # Unknown GitHub tool: nothing to shape → empty (caller/validation decides).
        return ArgumentBuildResult.build_ok({})

    # -- Per-operation builders ---------------------------------------------

    def _finalize(self, props, schema_required, extra_required, args, summary) -> ArgumentBuildResult:
        projected = {k: v for k, v in args.items() if (not props) or k in props}
        required = list(dict.fromkeys([*extra_required, *schema_required]))
        missing = [r for r in required if r not in projected]
        if missing:
            return ArgumentBuildResult.build_missing(missing, resource_summary=summary)
        return ArgumentBuildResult.build_ok(projected, resource_summary=summary)

    def _account_qualifier(self, owner) -> str:
        return f"user:{owner}" if owner else "user:@me"

    def _search_repositories(self, props, req, owner, repo, account_scoped, text, planner_args, owner_source) -> ArgumentBuildResult:
        qkey = _pick(props, _QUERY) or "query"
        summary = {"operation": "list_repositories", "account_scoped": account_scoped,
                   "owner_source": owner_source}
        if planner_args.get("query"):
            query = str(planner_args["query"])
        else:
            terms = []
            if repo:
                terms.append(str(repo))
            if account_scoped or owner:
                terms.append(self._account_qualifier(owner))
                summary["scope_qualifier"] = True
            query = " ".join(terms).strip() or _search_terms(text)
        if not query:
            return ArgumentBuildResult.build_missing(["query"], resource_summary=summary)
        return self._finalize(props, req, [qkey], {qkey: query}, summary)

    def _search_issues(self, props, req, owner, account_scoped, text, planner_args) -> ArgumentBuildResult:
        qkey = _pick(props, _QUERY) or "query"
        summary = {"operation": "search_issues", "account_scoped": account_scoped}
        terms = _search_terms(text)
        if account_scoped:
            scope = f"author:{owner}" if owner else "author:@me"
            terms = f"{terms} {scope}".strip()
            summary["scope_qualifier"] = True
        query = str(planner_args.get("query") or terms).strip()
        if not query:
            return ArgumentBuildResult.build_missing(["query"], resource_summary=summary)
        return self._finalize(props, req, [qkey], {qkey: query}, summary)

    def _list_repo_scoped(self, name, props, req, owner, repo, text, planner_args, owner_source) -> ArgumentBuildResult:
        summary = {"operation": name, "owner_source": owner_source}
        args, missing = self._owner_repo(props, owner, repo)
        if missing:
            return ArgumentBuildResult.build_missing(missing, resource_summary=summary)
        skey = _pick(props, _STATE)
        state = planner_args.get("state") or self._parse_state(text)
        if skey and state:
            args[skey] = state
        okey = _pick(props, _OWNER) or "owner"
        rkey = _pick(props, _REPO) or "repo"
        return self._finalize(props, req, [okey, rkey], args, summary)

    def _read_numbered(self, props, req, owner, repo, kind, num_aliases, number, planner_args, owner_source) -> ArgumentBuildResult:
        summary = {"operation": f"{kind}_read", "owner_source": owner_source}
        args, missing = self._owner_repo(props, owner, repo)
        nkey = _pick(props, num_aliases)
        value = _as_positive_int(number)
        if value is None:
            missing = missing + [num_aliases[0]]
        elif nkey:
            args[nkey] = value
        mkey = _pick(props, _METHOD)
        if mkey and "method" in props:
            args[mkey] = "get"  # read = get, per the consolidated read tools
        if missing:
            return ArgumentBuildResult.build_missing(missing, resource_summary=summary)
        okey = _pick(props, _OWNER) or "owner"
        rkey = _pick(props, _REPO) or "repo"
        extra = [okey, rkey] + ([nkey] if nkey else [])
        return self._finalize(props, req, extra, args, summary)

    # -- Shared shaping ------------------------------------------------------

    def _owner_repo(self, props, owner, repo):
        okey = _pick(props, _OWNER)
        rkey = _pick(props, _REPO)
        args, missing = {}, []
        if owner and okey:
            args[okey] = owner
        else:
            missing.append("owner")
        if repo and rkey:
            args[rkey] = repo
        else:
            missing.append("repo")
        return args, missing

    @staticmethod
    def _parse_state(text: str) -> str | None:
        m = _STATE_RE.search(text or "")
        return m.group(1).lower() if m else None


def _as_positive_int(value) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None
