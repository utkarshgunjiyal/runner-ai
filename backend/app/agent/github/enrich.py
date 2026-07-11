"""GitHub read-tool ToolSpec enrichment (Phase 46.2).

A discovered MCP tool becomes a generic ``ToolSpec`` (medium risk, external side
effect, ``mcp``/``github`` tags). This enricher turns each ALLOWLISTED GitHub read
tool into a complete, retrieval-friendly capability: a clear display name and
description, keywords, typical user questions and examples, a bounded timeout and
retry policy, evidence priority, and an explicit read-only classification.

Pure and config-free. It preserves the capability id and NEVER injects a secret.
The ``github`` provider tag (from the server id) is preserved, so the eligibility
layer keeps these tools connector-gated. Unknown/unlisted tool names pass through
unchanged (the allowlist already limits what is discovered).
"""

from __future__ import annotations

from app.agent.models.tool_spec import LatencyClass, RiskLevel, SideEffectType, ToolSpec

# Per read-tool retrieval + governance metadata. Keys are the official server's
# real tool names. Read-only: EXTERNAL side effect (private data egress) at MEDIUM
# risk — never "risk-free" — and never requires_approval (reads don't mutate).
_CATALOG: dict[str, dict] = {
    "search_repositories": {
        "display": "List / search GitHub repositories",
        "description": "List or search the authenticated account's GitHub repositories (read-only).",
        "keywords": ["github", "repository", "repositories", "repos", "list", "search", "my repos"],
        "questions": [
            "List my GitHub repositories.",
            "Which repository was updated most recently?",
            "Search my repositories for runner-ai.",
        ],
        "examples": ["List my GitHub repositories.", "Show details for utkarshgunjiyal/runner-ai."],
        "evidence_priority": 5,
    },
    "list_issues": {
        "display": "List GitHub issues",
        "description": "List issues in a GitHub repository, filterable by state (read-only).",
        "keywords": ["github", "issue", "issues", "list", "open", "closed", "bug"],
        "questions": ["What open issues are in runner-ai?", "List issues in utkarshgunjiyal/runner-ai."],
        "examples": ["List open issues in utkarshgunjiyal/runner-ai."],
        "evidence_priority": 5,
    },
    "issue_read": {
        "display": "Get a GitHub issue",
        "description": "Get one GitHub issue by number in a repository (read-only).",
        "keywords": ["github", "issue", "get", "show", "detail", "number"],
        "questions": ["Show issue 23 in runner-ai.", "What is issue 18 about?"],
        "examples": ["Show issue 23 in utkarshgunjiyal/runner-ai."],
        "evidence_priority": 5,
    },
    "list_pull_requests": {
        "display": "List GitHub pull requests",
        "description": "List pull requests in a GitHub repository, filterable by state (read-only).",
        "keywords": ["github", "pull", "request", "pr", "prs", "list", "open", "merge"],
        "questions": ["List open pull requests in runner-ai.", "What PRs are open in the repo?"],
        "examples": ["List open pull requests in utkarshgunjiyal/runner-ai."],
        "evidence_priority": 5,
    },
    "pull_request_read": {
        "display": "Get a GitHub pull request",
        "description": "Get one GitHub pull request by number in a repository (read-only).",
        "keywords": ["github", "pull", "request", "pr", "get", "show", "summarize", "number"],
        "questions": ["Summarize pull request 15.", "Show PR 7 in runner-ai."],
        "examples": ["Summarize pull request 15 in utkarshgunjiyal/runner-ai."],
        "evidence_priority": 5,
    },
    "search_issues": {
        "display": "Search GitHub issues",
        "description": "Search issues across GitHub repositories (read-only).",
        "keywords": ["github", "issue", "issues", "search", "find"],
        "questions": ["Search my issues for MCP timeout."],
        "examples": ["Search issues for document scope."],
        "evidence_priority": 4,
    },
}


def github_spec_transform(config, tool_name: str, spec: ToolSpec) -> ToolSpec:
    """Enrich a discovered GitHub read tool's ToolSpec. Preserves the id/tags and
    never injects secrets; unknown tools pass through unchanged."""
    meta = _CATALOG.get(tool_name)
    if meta is None:
        return spec

    # Keep the provider ("github") + "mcp" tags; add stable capability tags.
    tags = list(dict.fromkeys([*spec.tags, "github", "read_only"]))
    capability_tags = list(dict.fromkeys([*spec.capability_tags, "github", "vcs", "read_only"]))
    keywords = list(dict.fromkeys([*meta["keywords"], *spec.keywords]))

    return spec.model_copy(
        update={
            "name": meta["display"],
            "description": meta["description"],
            "tags": tags,
            "capability_tags": capability_tags,
            "keywords": keywords,
            "typical_user_questions": list(meta["questions"]),
            "examples": list(meta["examples"]),
            # Read-only external data egress: medium risk, no approval, idempotent.
            "risk_level": RiskLevel.MEDIUM,
            "side_effects": SideEffectType.EXTERNAL,
            "requires_approval": False,
            "idempotent": True,
            "data_egress": True,
            "cacheable": False,
            "latency_class": LatencyClass.HIGH,
            "timeout_seconds": int(getattr(config, "timeout_seconds", 45)) or 45,
            "max_retries": 1,
            "evidence_priority": meta["evidence_priority"],
            "enabled": True,
        }
    )
