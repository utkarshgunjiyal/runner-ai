"""Phase 46.2 — GitHub read-only MCP connector unit tests (config-free).

Covers the server config + allowlist, ToolSpec enrichment, result normalization
(bounded excerpts + secret-free), issue/PR number validation, and connector
status mapping. No network, no SDK, no live GitHub.
"""

import pytest

from app.agent.github import (
    GITHUB_BLOCKED_WRITE_TOOLS,
    GITHUB_READ_ONLY_TOOLS,
    build_github_mcp_server_config,
    github_result_normalizer,
    github_spec_transform,
)
from app.agent.github.normalize import (
    excerpt,
    normalize_issue,
    normalize_pull_request,
    normalize_repository,
    normalize_tool_result,
    validate_issue_number,
)
from app.agent.github.server import DEFAULT_GITHUB_MCP_IMAGE
from app.agent.github.status import (
    STATUS_AUTH_FAILED,
    STATUS_CONNECTED,
    STATUS_NOT_CONFIGURED,
    STATUS_UNAVAILABLE,
    build_github_connector_record,
    derive_state,
    integration_status_view,
)
from app.agent.connectors.models import ConnectorStatus
from app.agent.mcp.models import MCPToolCallResult, MCPToolDefinition
from app.agent.mcp.registry import convert_tool_definition


# --------------------------------------------------------------------------- #
# Server config + allowlist
# --------------------------------------------------------------------------- #

def test_config_requires_token_and_keeps_it_secret():
    with pytest.raises(ValueError):
        build_github_mcp_server_config(token="")

    # Default = HTTP remote transport (Phase 46.2.1): token only in the header.
    config = build_github_mcp_server_config(token="ghp_SECRET123")
    assert config.transport.value == "streamable_http"
    assert config.url == "https://api.githubcopilot.com/mcp/"
    assert config.headers["Authorization"] == "Bearer ghp_SECRET123"
    assert "ghp_SECRET123" not in (config.url or "")  # never in the URL
    # Secret-free repr + public metadata (omits headers AND url per policy).
    assert "ghp_SECRET123" not in repr(config)
    assert "headers" not in config.public_metadata()
    assert "url" not in config.public_metadata()
    assert "ghp_SECRET123" not in str(config.public_metadata())


def test_stdio_mode_keeps_token_in_env_only():
    config = build_github_mcp_server_config(token="ghp_SECRET123", transport="stdio")
    assert config.transport.value == "stdio"
    assert "ghp_SECRET123" not in " ".join(config.command)  # never on the command line
    assert config.environment["GITHUB_PERSONAL_ACCESS_TOKEN"] == "ghp_SECRET123"
    assert "--read-only" in config.command
    assert config.metadata["image"] == DEFAULT_GITHUB_MCP_IMAGE
    assert ":latest" not in DEFAULT_GITHUB_MCP_IMAGE
    assert "ghp_SECRET123" not in repr(config)


def test_invalid_transport_and_missing_url_fail_safe():
    with pytest.raises(ValueError):
        build_github_mcp_server_config(token="t", transport="carrier-pigeon")
    with pytest.raises(ValueError):
        build_github_mcp_server_config(token="t", transport="http", url="")


def test_allowlist_is_read_only_and_excludes_all_write_tools():
    config = build_github_mcp_server_config(token="t")
    allow = set(config.tool_allowlist)
    assert allow == set(GITHUB_READ_ONLY_TOOLS)
    # No write/admin tool is in the allowlist.
    for write_tool in GITHUB_BLOCKED_WRITE_TOOLS:
        assert write_tool not in allow, write_tool
    # The blocked set and the read set are disjoint.
    assert set(GITHUB_BLOCKED_WRITE_TOOLS).isdisjoint(set(GITHUB_READ_ONLY_TOOLS))


# --------------------------------------------------------------------------- #
# Enrichment
# --------------------------------------------------------------------------- #

def test_enrichment_preserves_id_and_marks_read_only():
    config = build_github_mcp_server_config(token="t")
    definition = MCPToolDefinition(name="search_repositories", input_schema={"type": "object"})
    spec = convert_tool_definition(config, definition)
    enriched = github_spec_transform(config, "search_repositories", spec)

    assert enriched.id == "mcp.github.search_repositories"  # id unchanged
    assert "github" in enriched.tags
    assert "read_only" in enriched.tags
    assert enriched.requires_approval is False
    assert enriched.typical_user_questions  # rich retrieval metadata added
    assert any("repositor" in k for k in enriched.keywords)


def test_enrichment_passthrough_for_unknown_tool():
    config = build_github_mcp_server_config(token="t")
    definition = MCPToolDefinition(name="some_other_tool", input_schema={"type": "object"})
    spec = convert_tool_definition(config, definition)
    assert github_spec_transform(config, "some_other_tool", spec) is spec


# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #

def test_normalize_repository_whitelists_safe_fields_only():
    raw = {
        "owner": {"login": "utkarshgunjiyal"}, "name": "runner-ai",
        "full_name": "utkarshgunjiyal/runner-ai", "description": "Autonomous agent platform",
        "private": True, "default_branch": "main", "updated_at": "2026-07-01T00:00:00Z",
        "html_url": "https://github.com/utkarshgunjiyal/runner-ai",
        "token": "ghp_LEAK", "node_id": "secret", "owner_secret": "x",
    }
    repo = normalize_repository(raw)
    assert repo == {
        "owner": "utkarshgunjiyal", "name": "runner-ai",
        "full_name": "utkarshgunjiyal/runner-ai", "description": "Autonomous agent platform",
        "visibility": "private", "default_branch": "main",
        "updated_at": "2026-07-01T00:00:00Z",
        "url": "https://github.com/utkarshgunjiyal/runner-ai",
    }
    assert "ghp_LEAK" not in str(repo)  # no stray fields leaked


def test_normalize_issue_and_pr_bound_bodies():
    long_body = "x" * 5000
    issue = normalize_issue({"number": 23, "title": "Fix scope", "state": "open",
                             "user": {"login": "dev"}, "labels": [{"name": "bug"}],
                             "body": long_body, "html_url": "https://gh/i/23"})
    assert issue["number"] == 23 and issue["author"] == "dev" and issue["labels"] == ["bug"]
    assert len(issue["body_excerpt"]) <= 281  # bounded excerpt

    pr = normalize_pull_request({"number": 15, "title": "Add MCP", "state": "open",
                                 "user": {"login": "dev"}, "base": {"ref": "main"},
                                 "head": {"ref": "feature"}, "draft": True, "body": long_body})
    assert pr["number"] == 15 and pr["base"] == "main" and pr["head"] == "feature" and pr["draft"] is True
    assert len(pr["body_excerpt"]) <= 281


def test_excerpt_normalizes_whitespace_and_bounds():
    assert excerpt("  a\n\n  b  ", limit=50) == "a b"
    assert len(excerpt("y" * 1000, limit=100)) <= 100


def test_issue_number_validation():
    assert validate_issue_number("23") == 23
    for bad in ("0", "-1", "abc", None, 0):
        with pytest.raises(ValueError):
            validate_issue_number(bad)


def test_normalizer_returns_structured_output_and_grounded_evidence_no_raw():
    result = MCPToolCallResult(
        success=True,
        structured_content={"items": [
            {"name": "runner-ai", "full_name": "u/runner-ai", "description": "Agent platform",
             "html_url": "https://github.com/u/runner-ai", "private": False},
        ]},
        content=[{"type": "text", "text": '{"token":"ghp_LEAK"}'}],  # raw block ignored
    )
    output, evidence = github_result_normalizer("search_repositories", result)
    assert output["kind"] == "repositories"
    assert output["repositories"][0]["full_name"] == "u/runner-ai"
    # Grounded evidence text; no raw payload / secret.
    text = evidence[0].content
    assert "runner-ai" in text
    assert "ghp_LEAK" not in text and "ghp_LEAK" not in str(output)
    assert "Repositories" in text


def test_normalize_tool_result_dispatch_and_empty():
    empty = normalize_tool_result("list_issues", MCPToolCallResult(success=True, structured_content={"items": []}))
    assert empty == {"provider": "github", "kind": "issues", "issues": []}


# --------------------------------------------------------------------------- #
# Status mapping
# --------------------------------------------------------------------------- #

def test_status_mapping():
    assert derive_state(configured=False, connected=False).status == STATUS_NOT_CONFIGURED
    assert derive_state(configured=True, connected=True, capabilities=["x"]).status == STATUS_CONNECTED
    assert derive_state(configured=True, connected=False,
                        error_code="mcp_transport_auth_error").status == STATUS_AUTH_FAILED
    assert derive_state(configured=True, connected=False,
                        error_code="mcp_transport_timeout").status == STATUS_UNAVAILABLE
    # Connected but ZERO allowlisted read tools → degraded (never guess a tool).
    from app.agent.github.status import STATUS_DEGRADED
    assert derive_state(configured=True, connected=True, capabilities=[],
                        allowed_tool_count=0).status == STATUS_DEGRADED
    assert not derive_state(configured=True, connected=True, capabilities=[],
                            allowed_tool_count=0).is_connected


def test_connector_record_gates_eligibility():
    # Not configured → no record → github ineligible.
    assert build_github_connector_record("u", derive_state(configured=False, connected=False)) is None
    # Connected (with read tools) → healthy record → eligible.
    rec = build_github_connector_record(
        "u", derive_state(configured=True, connected=True, capabilities=["list_issues"], allowed_tool_count=1))
    assert rec.status == ConnectorStatus.CONNECTED and rec.is_healthy
    # Configured but failed → non-healthy record (reportable, not eligible).
    rec2 = build_github_connector_record("u", derive_state(configured=True, connected=False,
                                                           error_code="mcp_transport_auth_error"))
    assert rec2.status == ConnectorStatus.ERROR and not rec2.is_healthy


def test_integration_status_view_is_truthful_and_secret_free():
    view = integration_status_view(derive_state(configured=True, connected=True, capabilities=["list_issues"]))
    assert view["github"]["status"] == STATUS_CONNECTED
    assert view["github"]["read_only"] is True
    assert view["gmail"]["label"] == "Coming next"  # Gmail stays truthful
    assert "token" not in str(view) and "environment" not in str(view)
