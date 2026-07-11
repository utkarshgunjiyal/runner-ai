"""Phase 46.2 — integration status API.

The route is mounted on a bare FastAPI app (not app.main, which needs config) and
the current-user dependency is overridden. Config-free; no DB, no MCP, no token.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent.github.status import derive_state
from app.routes.agent import get_current_user
from app.routes.integrations import configure_integrations, router


def _client(status_provider):
    configure_integrations(status_provider)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": "u"}
    return TestClient(app)


def test_not_configured_by_default():
    client = _client(lambda: derive_state(configured=False, connected=False))
    body = client.get("/integrations").json()
    assert body["github"]["status"] == "not_configured"
    assert body["github"]["label"] == "Not configured"
    assert body["github"]["read_only"] is True
    # Gmail stays truthful; MCP reflects availability.
    assert body["gmail"]["label"] == "Coming next"
    assert body["mcp_runtime"]["label"] == "Available"


def test_connected_shows_read_capabilities_no_token():
    caps = ["List / search GitHub repositories", "List GitHub issues"]
    client = _client(lambda: derive_state(configured=True, connected=True, capabilities=caps,
                                          allowed_tool_count=len(caps)))
    body = client.get("/integrations").json()
    assert body["github"]["status"] == "connected"
    assert body["github"]["capabilities"] == caps
    # Never a token/secret field anywhere in the response.
    raw = client.get("/integrations").text
    assert "token" not in raw.lower()
    assert "environment" not in raw and "GITHUB_PERSONAL_ACCESS_TOKEN" not in raw


def test_auth_failed_state():
    client = _client(lambda: derive_state(configured=True, connected=False,
                                          error_code="mcp_transport_auth_error"))
    body = client.get("/integrations").json()
    assert body["github"]["status"] == "auth_failed"
    assert body["github"]["label"] == "Authentication failed"


def test_refresh_reevaluates():
    calls = {"n": 0}

    def provider():
        calls["n"] += 1
        connected = calls["n"] >= 2  # first call not connected, then connected
        return derive_state(configured=True, connected=connected,
                            capabilities=["list_issues"] if connected else [],
                            allowed_tool_count=1 if connected else 0,
                            error_code=None if connected else "mcp_transport_unavailable")

    client = _client(provider)
    assert client.get("/integrations").json()["github"]["status"] == "unavailable"
    assert client.post("/integrations/refresh").json()["github"]["status"] == "connected"
