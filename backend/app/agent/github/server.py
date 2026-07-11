"""GitHub MCP server configuration + read-only allowlist (Phase 46.2).

Selected server: the **official GitHub MCP server**, `github/github-mcp-server`
(https://github.com/github/github-mcp-server), run over **stdio** via its published
container image. The image tag is **pinned** (never a floating ``latest``) and is
overridable per deployment.

Read-only by construction:
- the server is launched with ``--read-only`` and a repos/issues/pull_requests
  toolset, and
- discovery is constrained by ``GITHUB_READ_ONLY_TOOLS`` (the registry registers
  only these tool names), so even if the server advertised a write tool it could
  never become an eligible capability.

Tool names below are the official server's real names (as exposed by the current
GitHub MCP server): repository/issue/pull-request reads plus optional search.
"""

from __future__ import annotations

from app.agent.mcp.models import MCPRetryConfig, MCPServerConfig, MCPTransport

# Stable server id. Using "github" is deliberate: the MCP registry tags every tool
# with the server id, and the connector-eligibility layer reads a "github" tag as
# ``provider=github`` — so these tools are connector-gated for free.
GITHUB_MCP_SERVER_ID = "github"

# Official remote Streamable HTTP MCP endpoint (Phase 46.2.1). Works from a
# containerized backend over outbound HTTPS — no Docker socket / CLI / DinD.
DEFAULT_GITHUB_MCP_URL = "https://api.githubcopilot.com/mcp/"

# Pinned image reference (stdio mode only; override with GITHUB_MCP_IMAGE for a
# confirmed release). NOTE: pin to a stable release tag you have verified.
DEFAULT_GITHUB_MCP_IMAGE = "ghcr.io/github/github-mcp-server:v0.6.0"

# The read-only toolset the server is asked to expose (repos, issues, PRs).
DEFAULT_GITHUB_TOOLSETS = "repos,issues,pull_requests"

# Explicit read-only allowlist — ONLY these discovered tools ever register.
# (Real official-server tool names.)
GITHUB_READ_ONLY_TOOLS: tuple[str, ...] = (
    "search_repositories",     # list / search repositories
    "list_issues",             # list issues in a repository
    "issue_read",              # get one issue (method=get)
    "list_pull_requests",      # list pull requests in a repository
    "pull_request_read",       # get one pull request (method=get)
    "search_issues",           # optional: search issues
)

# Write/admin tools that MUST NEVER become eligible (documented for the allowlist
# guard + tests). The allowlist above already excludes everything not listed; this
# set is the explicit block-list the tests assert against.
GITHUB_BLOCKED_WRITE_TOOLS: tuple[str, ...] = (
    "issue_write",                 # create/update issue
    "add_issue_comment",
    "sub_issue_write",
    "create_pull_request",
    "update_pull_request",
    "merge_pull_request",
    "pull_request_review_write",
    "add_comment_to_pending_review",
    "enable_pr_auto_merge",
    "disable_pr_auto_merge",
    "request_copilot_review",
    "push_files",
    "create_or_update_file",
    "delete_file",
    "create_branch",
    "create_repository",
    "fork_repository",
    "run_secret_scanning",
    "actions_run_trigger",
    "resolve_review_thread",
    "unresolve_review_thread",
)


def build_github_mcp_server_config(
    *,
    token: str,
    transport: str = "http",
    url: str = DEFAULT_GITHUB_MCP_URL,
    image: str = DEFAULT_GITHUB_MCP_IMAGE,
    command: list[str] | None = None,
    toolsets: str = DEFAULT_GITHUB_TOOLSETS,
    timeout_seconds: float = 45.0,
    allowlist: tuple[str, ...] | list[str] = GITHUB_READ_ONLY_TOOLS,
) -> MCPServerConfig:
    """Build the trusted GitHub MCP ``MCPServerConfig``.

    ``transport`` selects the deployment mode (Phase 46.2.1):

    - ``"http"`` (default): the official remote Streamable HTTP endpoint. The token
      is sent ONLY in the ``Authorization: Bearer`` header (never in the URL, command
      line, ToolSpec, metadata, or log). Works from a containerized backend over
      outbound HTTPS — no Docker socket / CLI / Docker-in-Docker.
    - ``"stdio"``: an optional developer mode that launches the official server as a
      local Docker process (the host must have Docker). The token is placed only in
      the process ENVIRONMENT.

    Requires a non-empty token; callers gate on configuration first. Fails safe on an
    unsupported transport or an http mode with no URL."""
    if not token or not str(token).strip():
        raise ValueError("GitHub MCP requires a non-empty token")

    mode = (transport or "http").strip().lower()
    common = dict(
        server_id=GITHUB_MCP_SERVER_ID,
        name="GitHub (read-only)",
        enabled=True,
        timeout_seconds=timeout_seconds,
        retry=MCPRetryConfig(max_attempts=2, base_delay_seconds=0.2, max_delay_seconds=2.0),
        tool_allowlist=list(allowlist),
    )

    if mode == "http":
        if not url or not str(url).strip():
            raise ValueError("GitHub MCP http transport requires a URL")
        return MCPServerConfig(
            transport=MCPTransport.STREAMABLE_HTTP,
            url=str(url).strip(),
            # Token lives ONLY in the Authorization header (redacted from repr and
            # excluded from public_metadata, which also omits url + headers).
            headers={"Authorization": f"Bearer {token}"},
            metadata={"provider": "github", "read_only": True, "transport": "http"},
            **common,
        )

    if mode == "stdio":
        default_command = [
            "docker", "run", "-i", "--rm",
            "-e", "GITHUB_PERSONAL_ACCESS_TOKEN",
            "-e", "GITHUB_TOOLSETS",
            image,
            "stdio", "--read-only",
        ]
        return MCPServerConfig(
            transport=MCPTransport.STDIO,
            command=command or default_command,
            environment={
                "GITHUB_PERSONAL_ACCESS_TOKEN": str(token),
                "GITHUB_TOOLSETS": toolsets,
            },
            metadata={"provider": "github", "read_only": True, "transport": "stdio", "image": image},
            **common,
        )

    raise ValueError(f"unsupported GitHub MCP transport: {transport!r} (use 'http' or 'stdio')")
