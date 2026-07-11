"""Integration status API (Phase 46.2).

A safe, read-only status surface the frontend Integrations panel uses to show
REALITY: the live GitHub connector status (Not configured / Connecting / Connected
/ Degraded / Authentication failed / Unavailable) with its enabled read-only
capabilities, plus Gmail (truthfully "coming next") and the MCP runtime.

The status is derived by a provider installed by the composition root; it NEVER
contains a token, header, URL, or raw payload. There is no token input here — this
phase configures GitHub at the deployment/server level only.
"""

from fastapi import APIRouter, Depends

from app.agent.github.status import GithubConnectorState, derive_state, integration_status_view
from app.routes.agent import get_current_user

router = APIRouter(prefix="/integrations", tags=["integrations"])

# Installed by the composition root (main.py). Returns a fresh GithubConnectorState
# each call so a Refresh re-reads the current MCP health. Defaults to "not
# configured" so the route is safe before wiring / when GitHub is disabled.
_status_provider = lambda: derive_state(configured=False, connected=False)  # noqa: E731


def configure_integrations(status_provider) -> None:
    """Composition-root hook: install the GitHub status provider."""
    global _status_provider
    _status_provider = status_provider


def current_github_state() -> GithubConnectorState:
    try:
        state = _status_provider()
    except Exception:  # noqa: BLE001 - status must never break; degrade safely
        return derive_state(configured=True, connected=False, error_code="status_error")
    return state if isinstance(state, GithubConnectorState) else derive_state(
        configured=False, connected=False
    )


@router.get("")
async def get_integrations(user=Depends(get_current_user)) -> dict:
    """Live, safe integration statuses (auth-scoped; no secrets)."""
    return integration_status_view(current_github_state())


@router.post("/refresh")
async def refresh_integrations(user=Depends(get_current_user)) -> dict:
    """Re-evaluate and return the current statuses (safe Refresh/Retry action)."""
    return integration_status_view(current_github_state())
