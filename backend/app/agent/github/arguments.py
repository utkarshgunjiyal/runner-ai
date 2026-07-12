"""GitHub tool-argument construction (Phase 46.2.6).

Translates a natural-language GitHub request + resolved resources + the trusted
connector identity into schema-valid arguments for the SELECTED GitHub MCP tool.
Deterministic (no LLM): it maps the operation off the discovered tool name, fills
only fields the discovered ``input_schema`` declares (tolerating snake/camel
aliases), scopes account requests with GitHub search syntax (``user:@me`` /
``user:<login>``), validates required resources, and — crucially — returns a
structured *missing* / *ambiguous* result instead of ever emitting a global,
unscoped, or guessed query.

Internal orchestration fields (user_id/thread_id/run_id/request_id) are never
produced here; only declared tool arguments are. This composes with the Phase
46.2.4 adapter projection as defense in depth.

The builder is a callable seam: ``build(tool, run_context, default_args)`` returns
an ``ArgumentBuildResult``. For a non-GitHub tool it returns the caller's default
args unchanged, so DirectRuntime stays source-agnostic and every other capability
is byte-identical.
"""

from __future__ import annotations

import re

from app.agent.github.identity import GithubIdentity
from app.agent.github.resources import GithubResources, resolve_resources
from app.agent.models.tool_spec import ToolKind, ToolSpec
from app.agent.runtime.arguments import ArgumentBuildResult

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


def _is_github(tool: ToolSpec) -> bool:
    return tool.kind == ToolKind.MCP and "github" in (getattr(tool, "tags", []) or [])


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


def _as_positive_int(value) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _search_terms(text: str) -> str:
    tokens = [t.strip(".,!?;:'\"()") for t in re.findall(r"[A-Za-z0-9._-]+", text or "")]
    kept = [t for t in tokens if t and t.lower() not in _SEARCH_FILLER and not t.isdigit()]
    return " ".join(kept).strip()


class GithubArgumentBuilder:
    """Deterministic GitHub argument builder (an argument-builder seam)."""

    def __init__(self, *, identity: GithubIdentity | None = None) -> None:
        self._identity = identity or GithubIdentity()

    # The DirectRuntime argument-builder contract.
    def build(self, tool: ToolSpec, run_context, default_args: dict) -> ArgumentBuildResult:
        if not _is_github(tool):
            # Not our provider — leave the caller's args untouched.
            return ArgumentBuildResult.build_ok(default_args)

        name = _tool_name(tool)
        text = getattr(run_context, "user_request", "") or ""
        meta = getattr(run_context, "metadata", {}) or {}
        known = meta.get("github_active_repositories")
        known = known if isinstance(known, list) else None
        res = resolve_resources(text, identity=self._identity, known_repositories=known)

        if res.owner_candidates > 1:
            return ArgumentBuildResult.build_ambiguous(
                "owner", res.owner_candidates,
                resource_summary={"operation": name, "reason": "multiple_owner_matches"},
            )

        props = _schema_props(tool)
        req = _schema_required(tool)
        planner_args = meta.get("capability_args") if isinstance(meta.get("capability_args"), dict) else {}

        if name == "search_repositories":
            return self._search_repositories(tool, props, req, res, text, planner_args)
        if name == "search_issues":
            return self._search_issues(tool, props, req, res, text, planner_args)
        if name in ("list_issues", "list_pull_requests"):
            return self._list_repo_scoped(tool, name, props, req, res, text, planner_args)
        if name == "issue_read":
            return self._read_numbered(tool, props, req, res, "issue", _ISSUE_NUM, res.issue_number, planner_args)
        if name == "pull_request_read":
            return self._read_numbered(tool, props, req, res, "pull", _PULL_NUM, res.pull_number, planner_args)

        # Unknown GitHub tool: fall back to the caller's default args (projected).
        return ArgumentBuildResult.build_ok(default_args)

    # -- Per-operation builders ---------------------------------------------

    def _finalize(self, props, schema_required, extra_required, args, summary) -> ArgumentBuildResult:
        # Keep only declared keys (defense in depth alongside adapter projection).
        projected = {k: v for k, v in args.items() if (not props) or k in props}
        required = list(dict.fromkeys([*extra_required, *schema_required]))
        missing = [r for r in required if r not in projected]
        if missing:
            return ArgumentBuildResult.build_missing(missing, resource_summary=summary)
        return ArgumentBuildResult.build_ok(projected, resource_summary=summary)

    def _account_scope_qualifier(self, res: GithubResources) -> str:
        if res.owner:
            return f"user:{res.owner}"
        if self._identity.known:
            return f"user:{self._identity.owner}"
        return "user:@me"  # server resolves @me to the authenticated account

    def _search_repositories(self, tool, props, req, res, text, planner_args) -> ArgumentBuildResult:
        qkey = _pick(props, _QUERY) or "query"
        summary = {"operation": "list_repositories", "account_scoped": res.account_scoped,
                   "owner_source": res.owner_source}
        if planner_args.get("query"):
            query = str(planner_args["query"])
        else:
            terms = []
            if res.repo:
                terms.append(res.repo)
            if res.account_scoped or res.owner:
                terms.append(self._account_scope_qualifier(res))
                summary["scope_qualifier"] = True
            query = " ".join(terms).strip() or _search_terms(text)
        if not query:
            return ArgumentBuildResult.build_missing(["query"], resource_summary=summary)
        return self._finalize(props, req, [qkey], {qkey: query}, summary)

    def _search_issues(self, tool, props, req, res, text, planner_args) -> ArgumentBuildResult:
        qkey = _pick(props, _QUERY) or "query"
        summary = {"operation": "search_issues", "account_scoped": res.account_scoped}
        terms = _search_terms(text)
        if res.account_scoped:
            if res.owner:
                scope = f"author:{res.owner}"
            elif self._identity.known:
                scope = f"author:{self._identity.owner}"
            else:
                scope = "author:@me"
            terms = f"{terms} {scope}".strip()
            summary["scope_qualifier"] = True
        query = str(planner_args.get("query") or terms).strip()
        if not query:
            return ArgumentBuildResult.build_missing(["query"], resource_summary=summary)
        return self._finalize(props, req, [qkey], {qkey: query}, summary)

    def _list_repo_scoped(self, tool, name, props, req, res, text, planner_args) -> ArgumentBuildResult:
        summary = {"operation": name, "owner_source": res.owner_source}
        args, missing = self._owner_repo(props, res, planner_args)
        if missing:
            return ArgumentBuildResult.build_missing(missing, resource_summary=summary)
        skey = _pick(props, _STATE)
        state = planner_args.get("state") or self._parse_state(text)
        if skey and state:
            args[skey] = state
        okey = _pick(props, _OWNER) or "owner"
        rkey = _pick(props, _REPO) or "repo"
        return self._finalize(props, req, [okey, rkey], args, summary)

    def _read_numbered(self, tool, props, req, res, kind, num_aliases, number, planner_args) -> ArgumentBuildResult:
        summary = {"operation": f"{kind}_read", "owner_source": res.owner_source}
        args, missing = self._owner_repo(props, res, planner_args)
        nkey = _pick(props, num_aliases)
        planner_num = planner_args.get(num_aliases[0]) or planner_args.get(num_aliases[-1])
        value = number if number is not None else _as_positive_int(planner_num)
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

    # -- Shared resolution --------------------------------------------------

    def _owner_repo(self, props, res, planner_args):
        okey = _pick(props, _OWNER)
        rkey = _pick(props, _REPO)
        owner = planner_args.get("owner") or res.owner
        repo = planner_args.get("repo") or planner_args.get("repository") or res.repo
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
