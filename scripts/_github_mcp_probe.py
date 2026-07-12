"""Opt-in live GitHub MCP probe (Phase 46.2). NOT run in CI.

Discovers the allowlisted read-only GitHub tools through the real MCP stack and
performs ONE read (repository listing) plus optional issue/PR reads for a test
repo. Prints ONLY safe, normalized output — never the token, headers, URL, or a
raw payload. Performs NO writes.

Run via scripts/verify-github-mcp.sh (which validates the environment first).
"""

from __future__ import annotations

import asyncio
import os
import sys

# Ensure the backend package is importable when run from the repo root.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


async def main() -> int:
    token = os.environ.get("GITHUB_MCP_TOKEN") or os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN")
    if not token:
        print("FAIL: no GitHub token in environment (GITHUB_MCP_TOKEN / GITHUB_PERSONAL_ACCESS_TOKEN)")
        return 2

    from app.agent.github import (
        GITHUB_MCP_SERVER_ID,
        build_github_mcp_server_config,
        github_result_normalizer,
        github_spec_transform,
    )
    from app.agent.github.normalize import format_output
    from app.agent.mcp.composition import build_mcp_registry_manager
    from app.agent.tools.mcp_adapter import MCPAdapter

    transport = os.environ.get("GITHUB_MCP_TRANSPORT", "http").strip().lower()
    url = os.environ.get("GITHUB_MCP_URL", "https://api.githubcopilot.com/mcp/")
    image = os.environ.get("GITHUB_MCP_IMAGE", "ghcr.io/github/github-mcp-server:v0.6.0")
    config = build_github_mcp_server_config(token=token, transport=transport, url=url, image=image)
    where = url if transport == "http" else image  # neither contains the token
    print(f"GitHub MCP server: {config.name}  transport={transport}  target={where}  read_only=True")
    print(f"Allowlisted read tools: {', '.join(config.tool_allowlist)}")

    manager, conn = await build_mcp_registry_manager(
        [config], spec_transform=github_spec_transform, discover=False
    )
    try:
        specs = await manager.discover_server_tools(GITHUB_MCP_SERVER_ID)
        stats = manager.discovery_stats(GITHUB_MCP_SERVER_ID)
        print(f"Discovered={stats.get('discovered_tool_count')} "
              f"allowed={stats.get('allowed_tool_count')} "
              f"excluded={stats.get('excluded_tool_count')}")
        print("Enabled capabilities: " + ", ".join(s.name for s in specs))

        adapter = MCPAdapter(manager, result_normalizers={GITHUB_MCP_SERVER_ID: github_result_normalizer})

        # Argument parity (Phase 46.2.6): show the RUNTIME-built arguments for the
        # account-scoped request next to the verifier's, and confirm they resolve to
        # the same account scoping. Never prints the token or any header.
        from app.agent.github import (
            GithubArgumentBuilder,
            GithubResourceResolver,
            resolve_github_identity,
        )
        from app.agent.resources import (
            ArgumentBuilderRegistry,
            ResourceAwareArgumentBuilder,
            ResourceResolverRegistry,
        )
        from app.agent.runtime.context import RunContext

        async def _get_me():
            return await manager.client.call_tool(config, "get_me", {})

        identity = await resolve_github_identity(
            configured_owner=os.environ.get("GITHUB_MCP_OWNER"), get_me_fn=_get_me
        )
        resolvers = ResourceResolverRegistry()
        resolvers.register(GithubResourceResolver(identity=identity))
        builders = ArgumentBuilderRegistry()
        builders.register(GithubArgumentBuilder())
        pipeline = ResourceAwareArgumentBuilder(resolvers, builders)
        repo_spec = manager.tool_registry.get(f"mcp.{GITHUB_MCP_SERVER_ID}.search_repositories")
        built = pipeline.build(
            repo_spec, RunContext.create("List all my GitHub repositories.", user_id="probe"),
            {"query": "List all my GitHub repositories."},
        )
        print(f"\nIdentity source: {identity.source}  known={identity.known}")
        print(f"Runtime-built args : {built.arguments}")
        print("Verifier args      : {'query': 'user:@me'}")

        # READ ONLY: list repositories for the authenticated account, using the
        # runtime-built (account-scoped) arguments.
        result = await adapter.execute(repo_spec, built.arguments if built.ok else {"query": "user:@me"})
        print("\n--- Repositories (read-only) ---")
        print(format_output(result.output) if result.success else "  (repository read failed safely)")

        # Optional: inspect one configured test repo's open issues (still read-only).
        repo = os.environ.get("GITHUB_TEST_REPO", "")
        if "/" in repo:
            owner, name = repo.split("/", 1)
            issue_spec = manager.tool_registry.get(f"mcp.{GITHUB_MCP_SERVER_ID}.list_issues")
            issues = await adapter.execute(issue_spec, {"owner": owner, "repo": name, "state": "open"})
            print(f"\n--- Open issues in {repo} (read-only) ---")
            print(format_output(issues.output) if issues.success else "  (issue read failed safely)")
    except Exception as exc:  # noqa: BLE001 - print a SAFE message only
        print(f"FAIL: discovery/read failed safely: {type(exc).__name__}")
        return 1
    finally:
        await manager.close()
        if conn is not None:
            await conn.shutdown()
    print("\nOK: live GitHub read-only MCP verification succeeded (no writes performed).")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
