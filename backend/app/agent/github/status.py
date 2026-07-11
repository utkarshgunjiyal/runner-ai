"""GitHub connector status (Phase 46.2). Pure, config-free, secret-free.

Derives the deployment-scoped GitHub connector status from the MCP server's real
state and maps it to the safe labels the frontend Integrations panel shows. It
also builds the connector record the eligibility layer reads (GitHub capabilities
are eligible only when the status is CONNECTED), and the full integration-status
view for the status API.

Nothing here ever contains a token, header, URL, or raw payload.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.agent.connectors.models import ConnectorProvider, ConnectorRecord, ConnectorStatus

# Frontend-facing status labels (stable strings the UI maps to copy/colour).
STATUS_NOT_CONFIGURED = "not_configured"
STATUS_CONNECTING = "connecting"
STATUS_CONNECTED = "connected"
STATUS_DEGRADED = "degraded"
STATUS_AUTH_FAILED = "auth_failed"
STATUS_UNAVAILABLE = "unavailable"

_LABELS = {
    STATUS_NOT_CONFIGURED: "Not configured",
    STATUS_CONNECTING: "Connecting",
    STATUS_CONNECTED: "Connected",
    STATUS_DEGRADED: "Degraded",
    STATUS_AUTH_FAILED: "Authentication failed",
    STATUS_UNAVAILABLE: "Unavailable",
}


class GithubConnectorState(BaseModel):
    """Safe, secret-free snapshot of the GitHub connector for API/eligibility."""

    model_config = ConfigDict(frozen=True)

    configured: bool = False
    status: str = STATUS_NOT_CONFIGURED
    capabilities: list[str] = Field(default_factory=list)  # enabled read-tool names
    discovered_tool_count: int = 0
    allowed_tool_count: int = 0
    error_code: str | None = None

    @property
    def is_connected(self) -> bool:
        return self.status == STATUS_CONNECTED

    def public_view(self) -> dict:
        return {
            "provider": "github",
            "configured": self.configured,
            "status": self.status,
            "label": _LABELS.get(self.status, "Unknown"),
            "read_only": True,
            "capabilities": list(self.capabilities),
            "allowed_tool_count": self.allowed_tool_count,
            "error_code": self.error_code,
        }


def derive_state(
    *,
    configured: bool,
    connected: bool,
    capabilities: list[str] | None = None,
    discovered_tool_count: int = 0,
    allowed_tool_count: int = 0,
    error_code: str | None = None,
) -> GithubConnectorState:
    """Map raw MCP facts → a GitHub connector state. ``error_code`` is a safe,
    vendor-free code only (e.g. ``mcp_transport_auth_error``)."""
    if not configured:
        return GithubConnectorState(configured=False, status=STATUS_NOT_CONFIGURED)
    if connected:
        # Connected but zero allowlisted read tools registered → DEGRADED (e.g. the
        # pinned tool names are absent on this server version). Never guess a
        # replacement tool; report degraded so the operator confirms the release.
        allowed = allowed_tool_count if allowed_tool_count else len(capabilities or [])
        status = STATUS_CONNECTED if allowed > 0 else STATUS_DEGRADED
        return GithubConnectorState(
            configured=True,
            status=status,
            capabilities=list(capabilities or []),
            discovered_tool_count=discovered_tool_count,
            allowed_tool_count=allowed_tool_count,
            error_code=None if status == STATUS_CONNECTED else "no_allowlisted_tools",
        )
    # Configured but not connected → classify by the (safe) error code.
    code = (error_code or "").lower()
    if "auth" in code:
        status = STATUS_AUTH_FAILED
    elif "timeout" in code or "connect" in code or "unavailable" in code or "connection" in code:
        status = STATUS_UNAVAILABLE
    else:
        status = STATUS_DEGRADED
    return GithubConnectorState(configured=True, status=status, error_code=error_code or None)


def build_github_connector_record(user_id: str, state: GithubConnectorState) -> ConnectorRecord | None:
    """The connector record the eligibility layer reads. Returns None when GitHub
    is not configured (→ GitHub tools are ineligible and never reach the planner).
    A non-connected state yields a non-healthy record (also ineligible) so the
    status is still reportable."""
    if not state.configured:
        return None
    status = ConnectorStatus.CONNECTED if state.is_connected else ConnectorStatus.ERROR
    return ConnectorRecord(
        connector_id="github:deployment",
        user_id=user_id,
        provider=ConnectorProvider.GITHUB,
        status=status,
        scopes=[],
        account_display_name="GitHub (deployment-scoped)",
        error_code=state.error_code,
    )


def integration_status_view(github: GithubConnectorState) -> dict:
    """The full, safe integrations payload for the status API / frontend.

    GitHub reflects live state; Gmail stays truthful (not implemented); the MCP
    runtime reflects whether GitHub (the only wired server) is available."""
    return {
        "github": github.public_view(),
        "gmail": {
            "provider": "gmail",
            "configured": False,
            "status": STATUS_NOT_CONFIGURED,
            "label": "Coming next",
            "read_only": True,
            "capabilities": [],
        },
        "mcp_runtime": {
            "provider": "mcp",
            "status": STATUS_CONNECTED if github.is_connected else "available",
            "label": "Connected" if github.is_connected else "Available",
        },
    }
