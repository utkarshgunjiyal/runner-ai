#!/usr/bin/env bash
# Opt-in LIVE verification of the GitHub read-only MCP connector (Phase 46.2).
#
# This performs REAL GitHub reads through the official GitHub MCP server. It is
# NOT part of CI and never runs automatically. It requires explicit environment
# configuration and fails safely when anything is missing. It never prints the
# token and never performs a write.
#
# Usage (HTTP — recommended, works from a container over outbound HTTPS):
#   GITHUB_MCP_ENABLED=true \
#   GITHUB_MCP_TRANSPORT=http \       # default
#   GITHUB_MCP_TOKEN=ghp_xxx \        # or GITHUB_PERSONAL_ACCESS_TOKEN
#   [GITHUB_MCP_URL=https://api.githubcopilot.com/mcp/] \
#   [GITHUB_TEST_REPO=owner/name] \   # optional read of one repo's open issues
#   ./scripts/verify-github-mcp.sh
#
# Usage (stdio — optional local developer mode; the HOST must have Docker):
#   GITHUB_MCP_ENABLED=true GITHUB_MCP_TRANSPORT=stdio GITHUB_MCP_TOKEN=ghp_xxx \
#   [GITHUB_MCP_IMAGE=ghcr.io/github/github-mcp-server:vX.Y.Z] ./scripts/verify-github-mcp.sh
set -euo pipefail

fail() { echo "verify-github-mcp: $1" >&2; exit "${2:-1}"; }

# --- Fail safe on missing configuration (never proceed without a token). ------
if [ "${GITHUB_MCP_ENABLED:-false}" != "true" ]; then
  fail "GITHUB_MCP_ENABLED is not 'true' — set it to opt in." 2
fi
TOKEN="${GITHUB_MCP_TOKEN:-${GITHUB_PERSONAL_ACCESS_TOKEN:-}}"
if [ -z "${TOKEN}" ]; then
  fail "no token: set GITHUB_MCP_TOKEN or GITHUB_PERSONAL_ACCESS_TOKEN." 2
fi
if [ "${CI:-}" = "true" ]; then
  fail "refusing to run in CI (CI=true) — this makes live GitHub calls." 3
fi

command -v python3 >/dev/null 2>&1 || fail "python3 is required."
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TRANSPORT="$(printf '%s' "${GITHUB_MCP_TRANSPORT:-http}" | tr '[:upper:]' '[:lower:]')"

case "${TRANSPORT}" in
  http)
    URL="${GITHUB_MCP_URL:-https://api.githubcopilot.com/mcp/}"
    echo "verify-github-mcp: transport=http  endpoint=${URL}  (no Docker required)"
    export GITHUB_MCP_URL="${URL}"
    ;;
  stdio)
    command -v docker >/dev/null 2>&1 || fail "stdio mode needs Docker on the host running this script."
    IMAGE="${GITHUB_MCP_IMAGE:-ghcr.io/github/github-mcp-server:v0.6.0}"
    echo "verify-github-mcp: transport=stdio  pinned image=${IMAGE}"
    docker pull "${IMAGE}" >/dev/null 2>&1 || fail "could not pull ${IMAGE} — confirm the pinned tag exists."
    export GITHUB_MCP_IMAGE="${IMAGE}"
    ;;
  *)
    fail "unsupported GITHUB_MCP_TRANSPORT='${TRANSPORT}' (use 'http' or 'stdio')." 2
    ;;
esac

# Export for the probe (token via env only; never echoed).
export GITHUB_MCP_TOKEN="${TOKEN}"
export GITHUB_MCP_TRANSPORT="${TRANSPORT}"

echo "verify-github-mcp: discovering allowlisted read tools and listing repositories (read-only, no writes)…"
python3 "${ROOT}/scripts/_github_mcp_probe.py"
