"""Real GitHub read-only MCP connector (Phase 46.2).

Connects Runner.ai to a real GitHub account through the EXISTING MCP architecture
(server registry → connection → transport → discovery → adapter → unified
registry). This package adds only GitHub-specific, config-free pieces:

- ``server`` — the trusted, pinned GitHub MCP server config + the read-only tool
  allowlist and the explicitly blocked write tools.
- ``enrich`` — a ToolSpec enricher that turns a discovered read tool into a rich,
  retrieval-friendly capability (provider tag, keywords, typical questions).
- ``normalize`` — pure normalizers (Repository / Issue / PullRequest) with bounded
  excerpts and grounded, secret-free formatting.
- ``status`` — deployment-scoped connector status derivation + the safe
  integration-status view for the API/frontend.

Boundaries: no direct GitHub REST calls (MCP only), no write tools, no per-user
OAuth, and no credential ever crosses into a ToolSpec, evidence, log, error, or API
response.
"""

from app.agent.github.arguments import GithubArgumentBuilder
from app.agent.github.enrich import github_spec_transform
from app.agent.github.identity import (
    GithubIdentity,
    resolve_github_identity,
    validate_owner,
)
from app.agent.github.normalize import github_result_normalizer
from app.agent.github.resources import GithubResources, resolve_resources
from app.agent.github.server import (
    GITHUB_BLOCKED_WRITE_TOOLS,
    GITHUB_MCP_SERVER_ID,
    GITHUB_READ_ONLY_TOOLS,
    build_github_mcp_server_config,
)
from app.agent.github.status import (
    GithubConnectorState,
    build_github_connector_record,
    integration_status_view,
)

__all__ = [
    "GITHUB_MCP_SERVER_ID",
    "GITHUB_READ_ONLY_TOOLS",
    "GITHUB_BLOCKED_WRITE_TOOLS",
    "build_github_mcp_server_config",
    "github_spec_transform",
    "github_result_normalizer",
    "GithubConnectorState",
    "build_github_connector_record",
    "integration_status_view",
    "GithubArgumentBuilder",
    "GithubIdentity",
    "resolve_github_identity",
    "validate_owner",
    "GithubResources",
    "resolve_resources",
]
