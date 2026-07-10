"""Phase 39 tests — MCP domain models + validation.

Config-free: pure pydantic models, no client, no network. Verifies transport
validation, id/namespace rules, secret redaction (repr + public metadata), and
the tool/result shapes.
"""

import pytest

from app.agent.mcp.models import (
    MCPServerConfig,
    MCPToolCallResult,
    MCPToolDefinition,
    MCPTransport,
)


def stdio(**kw):
    base = dict(server_id="fs", name="Filesystem", transport=MCPTransport.STDIO,
                command=["mcp-fs"])
    base.update(kw)
    return MCPServerConfig(**base)


def http(**kw):
    base = dict(server_id="gh", name="GitHub", transport=MCPTransport.STREAMABLE_HTTP,
                url="https://mcp.example/github")
    base.update(kw)
    return MCPServerConfig(**base)


# --------------------------------------------------------------------------- #
# Transport requirements
# --------------------------------------------------------------------------- #

def test_stdio_requires_command():
    with pytest.raises(ValueError):
        MCPServerConfig(server_id="fs", name="FS", transport=MCPTransport.STDIO)


def test_stdio_rejects_empty_command():
    with pytest.raises(ValueError):
        MCPServerConfig(server_id="fs", name="FS", transport=MCPTransport.STDIO, command=[])


def test_http_requires_url():
    with pytest.raises(ValueError):
        MCPServerConfig(server_id="gh", name="GH", transport=MCPTransport.STREAMABLE_HTTP)


def test_valid_configs_build():
    assert stdio().command == ["mcp-fs"]
    assert http().url.endswith("/github")


# --------------------------------------------------------------------------- #
# server_id rules (stable, unique-friendly, namespace-isolating)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bad", ["", "  ", "has space", "has.dot", "has/slash"])
def test_invalid_server_ids_rejected(bad):
    with pytest.raises(ValueError):
        stdio(server_id=bad)


@pytest.mark.parametrize("good", ["github", "gh-1", "file_system", "S3"])
def test_valid_server_ids_accepted(good):
    assert stdio(server_id=good).server_id == good


def test_unknown_fields_forbidden():
    with pytest.raises(ValueError):
        MCPServerConfig(server_id="fs", name="FS", transport=MCPTransport.STDIO,
                        command=["x"], secret_token="oops")  # extra field


# --------------------------------------------------------------------------- #
# Secret handling
# --------------------------------------------------------------------------- #

def test_secrets_absent_from_repr():
    cfg = http(headers={"Authorization": "Bearer SECRET-TOKEN"},
               environment={"API_KEY": "sk-SECRET"})
    text = repr(cfg)
    assert "SECRET-TOKEN" not in text
    assert "sk-SECRET" not in text


def test_secrets_absent_from_public_metadata():
    cfg = http(headers={"Authorization": "Bearer SECRET-TOKEN"},
               environment={"API_KEY": "sk-SECRET"})
    meta = cfg.public_metadata()
    blob = str(meta)
    assert "SECRET-TOKEN" not in blob
    assert "sk-SECRET" not in blob
    # url may embed credentials → excluded from public metadata too
    assert "url" not in meta
    assert set(meta) == {"server_id", "name", "transport", "enabled", "timeout_seconds"}


def test_timeout_must_be_positive():
    with pytest.raises(ValueError):
        stdio(timeout_seconds=0)


# --------------------------------------------------------------------------- #
# Tool definition + result shapes
# --------------------------------------------------------------------------- #

def test_tool_definition_defaults():
    d = MCPToolDefinition(name="read_file")
    assert d.description == "" and d.input_schema == {} and d.annotations == {}


def test_tool_call_result_shape():
    r = MCPToolCallResult(
        success=True,
        content=[{"type": "text", "text": "hello"}],
        structured_content={"ok": True},
    )
    assert r.success and not r.is_error
    assert r.content[0]["text"] == "hello"
    assert r.structured_content == {"ok": True}
