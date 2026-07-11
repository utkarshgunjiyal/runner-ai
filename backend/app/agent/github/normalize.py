"""GitHub MCP result normalization (Phase 46.2).

Turns a raw GitHub MCP tool result into stable, bounded, secret-free internal
structures (Repository / Issue / PullRequest) plus grounded, human-readable
evidence. Pure and config-free. Only whitelisted fields are copied, bodies are
excerpted, and lists are capped — no tokens, headers, hidden server metadata, or
oversized payloads ever pass through.

The final answer is grounded ONLY in these normalized structures — never a raw MCP
payload.
"""

from __future__ import annotations

import json

from app.agent.mcp.models import MCPToolCallResult
from app.agent.runtime.context import EvidenceItem

_MAX_ITEMS = 30
_BODY_EXCERPT_CHARS = 280
_MAX_TEXT = 200


def _clip(value, limit: int = _MAX_TEXT) -> str:
    text = "" if value is None else str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def excerpt(value, limit: int = _BODY_EXCERPT_CHARS) -> str:
    """A bounded, single-normalized-whitespace excerpt of an untrusted body."""
    return _clip(value, limit)


def _as_int(value) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def validate_issue_number(value) -> int:
    """Validate an issue/PR number is a positive integer (raises ValueError)."""
    n = _as_int(value)
    if n is None:
        raise ValueError("issue/PR number must be a positive integer")
    return n


def _labels(raw) -> list[str]:
    out: list[str] = []
    for label in raw or []:
        if isinstance(label, dict):
            name = label.get("name")
        else:
            name = label
        if name:
            out.append(_clip(name, 60))
    return out[:10]


def _login(raw) -> str:
    if isinstance(raw, dict):
        return _clip(raw.get("login") or raw.get("name") or "", 80)
    return _clip(raw, 80)


def normalize_repository(raw: dict) -> dict:
    raw = raw or {}
    owner = _login(raw.get("owner"))
    name = _clip(raw.get("name"), 140)
    full = _clip(raw.get("full_name") or (f"{owner}/{name}" if owner and name else name), 200)
    visibility = raw.get("visibility")
    if not visibility:
        visibility = "private" if raw.get("private") else "public"
    return {
        "owner": owner,
        "name": name,
        "full_name": full,
        "description": _clip(raw.get("description"), 200),
        "visibility": _clip(visibility, 20),
        "default_branch": _clip(raw.get("default_branch"), 100),
        "updated_at": _clip(raw.get("updated_at") or raw.get("pushed_at"), 40),
        "url": _clip(raw.get("html_url") or raw.get("url"), 300),
    }


def normalize_issue(raw: dict) -> dict:
    raw = raw or {}
    return {
        "number": _as_int(raw.get("number")),
        "title": _clip(raw.get("title"), 200),
        "state": _clip(raw.get("state"), 20),
        "author": _login(raw.get("user") or raw.get("author")),
        "labels": _labels(raw.get("labels")),
        "created_at": _clip(raw.get("created_at"), 40),
        "updated_at": _clip(raw.get("updated_at"), 40),
        "url": _clip(raw.get("html_url") or raw.get("url"), 300),
        "body_excerpt": excerpt(raw.get("body")),
    }


def normalize_pull_request(raw: dict) -> dict:
    raw = raw or {}
    base = raw.get("base") or {}
    head = raw.get("head") or {}
    return {
        "number": _as_int(raw.get("number")),
        "title": _clip(raw.get("title"), 200),
        "state": _clip(raw.get("state"), 20),
        "author": _login(raw.get("user") or raw.get("author")),
        "base": _clip(base.get("ref") if isinstance(base, dict) else base, 100),
        "head": _clip(head.get("ref") if isinstance(head, dict) else head, 100),
        "draft": bool(raw.get("draft")),
        "created_at": _clip(raw.get("created_at"), 40),
        "updated_at": _clip(raw.get("updated_at"), 40),
        "url": _clip(raw.get("html_url") or raw.get("url"), 300),
        "body_excerpt": excerpt(raw.get("body")),
    }


# --------------------------------------------------------------------------- #
# Raw payload extraction
# --------------------------------------------------------------------------- #

def _payload(result: MCPToolCallResult):
    """Extract the JSON payload from an MCP result: prefer structured_content,
    else parse the first JSON text block. Returns a dict/list or None."""
    if result.structured_content:
        return result.structured_content
    for block in result.content or []:
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
            try:
                return json.loads(block["text"])
            except (ValueError, TypeError):
                continue
    return None


def _as_list(payload, *keys) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
        # A single object → one-element list.
        return [payload]
    return []


def _first(payload):
    if isinstance(payload, list):
        return payload[0] if payload else {}
    if isinstance(payload, dict):
        for key in ("issue", "pull_request", "repository", "data"):
            if isinstance(payload.get(key), dict):
                return payload[key]
        return payload
    return {}


# --------------------------------------------------------------------------- #
# Formatting (grounded evidence text)
# --------------------------------------------------------------------------- #

def _line(text: str, url: str) -> str:
    return f"{text} — {url}" if url else text


def format_repositories(repos: list[dict]) -> str:
    if not repos:
        return "No repositories were found for the authenticated GitHub account."
    lines = [f"Repositories ({len(repos)})"]
    for r in repos:
        label = r["full_name"] or r["name"]
        desc = f" — {r['description']}" if r["description"] else ""
        lines.append(_line(f"- {label}{desc}", r["url"]))
    return "\n".join(lines)


def format_issues(issues: list[dict], *, kind: str = "issues") -> str:
    if not issues:
        return f"No {kind} were found."
    lines = [f"Open/searched {kind} ({len(issues)})"]
    for i in issues:
        num = f"#{i['number']} " if i["number"] else ""
        state = f" [{i['state']}]" if i["state"] else ""
        lines.append(_line(f"- {num}{i['title']}{state}", i["url"]))
    return "\n".join(lines)


def format_issue(issue: dict) -> str:
    num = f"#{issue['number']} " if issue["number"] else ""
    lines = [f"Issue {num}{issue['title']}".rstrip()]
    if issue["state"]:
        lines.append(f"State: {issue['state']}")
    if issue["author"]:
        lines.append(f"Author: {issue['author']}")
    if issue["labels"]:
        lines.append("Labels: " + ", ".join(issue["labels"]))
    if issue["body_excerpt"]:
        lines.append(issue["body_excerpt"])
    if issue["url"]:
        lines.append(issue["url"])
    return "\n".join(lines)


def format_pull_requests(pulls: list[dict]) -> str:
    if not pulls:
        return "No pull requests were found."
    lines = [f"Pull requests ({len(pulls)})"]
    for p in pulls:
        num = f"#{p['number']} " if p["number"] else ""
        state = f" [{p['state']}]" if p["state"] else ""
        draft = " (draft)" if p["draft"] else ""
        lines.append(_line(f"- {num}{p['title']}{state}{draft}", p["url"]))
    return "\n".join(lines)


def format_pull_request(pr: dict) -> str:
    num = f"#{pr['number']} " if pr["number"] else ""
    lines = [f"Pull request {num}{pr['title']}".rstrip()]
    if pr["state"]:
        lines.append(f"State: {pr['state']}" + (" (draft)" if pr["draft"] else ""))
    if pr["author"]:
        lines.append(f"Author: {pr['author']}")
    if pr["base"] or pr["head"]:
        lines.append(f"Branch: {pr['head']} → {pr['base']}")
    if pr["body_excerpt"]:
        lines.append(pr["body_excerpt"])
    if pr["url"]:
        lines.append(pr["url"])
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Normalizer dispatch (used by the MCP adapter)
# --------------------------------------------------------------------------- #

def normalize_tool_result(tool_name: str, result: MCPToolCallResult) -> dict:
    """Normalize a GitHub MCP result into a stable, safe ``output`` dict."""
    payload = _payload(result)

    if tool_name in ("search_repositories",):
        repos = [normalize_repository(r) for r in _as_list(payload, "items", "repositories")[:_MAX_ITEMS]]
        return {"provider": "github", "kind": "repositories", "repositories": repos}

    if tool_name in ("list_issues", "search_issues"):
        issues = [normalize_issue(i) for i in _as_list(payload, "items", "issues")[:_MAX_ITEMS]]
        return {"provider": "github", "kind": "issues", "issues": issues}

    if tool_name == "issue_read":
        return {"provider": "github", "kind": "issue", "issue": normalize_issue(_first(payload))}

    if tool_name == "list_pull_requests":
        pulls = [normalize_pull_request(p) for p in _as_list(payload, "items", "pull_requests")[:_MAX_ITEMS]]
        return {"provider": "github", "kind": "pull_requests", "pull_requests": pulls}

    if tool_name == "pull_request_read":
        return {"provider": "github", "kind": "pull_request", "pull_request": normalize_pull_request(_first(payload))}

    # Unknown allowlisted tool: return a minimal safe wrapper (no raw payload).
    return {"provider": "github", "kind": "unknown", "tool": _clip(tool_name, 80)}


def format_output(output: dict) -> str:
    kind = output.get("kind")
    if kind == "repositories":
        return format_repositories(output.get("repositories", []))
    if kind == "issues":
        return format_issues(output.get("issues", []))
    if kind == "issue":
        return format_issue(output.get("issue", {}))
    if kind == "pull_requests":
        return format_pull_requests(output.get("pull_requests", []))
    if kind == "pull_request":
        return format_pull_request(output.get("pull_request", {}))
    return "No GitHub data was returned."


def github_result_normalizer(tool_name: str, result: MCPToolCallResult):
    """MCP-adapter hook: ``(tool_name, result) -> (output_dict, [EvidenceItem])``.

    Produces the normalized structure AND a grounded, formatted evidence block so
    the final answer is built only from real GitHub data. No secret can appear —
    only whitelisted, excerpted fields are included."""
    output = normalize_tool_result(tool_name, result)
    evidence = [
        EvidenceItem(
            source=f"github:{tool_name}",
            content=format_output(output),
            score=1.0,
            metadata={"provider": "github", "kind": output.get("kind")},
        )
    ]
    return output, evidence
