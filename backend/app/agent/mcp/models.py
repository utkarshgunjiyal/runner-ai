"""MCP domain models (Phase 39).

Provider-agnostic models that describe an MCP server, a discovered tool, and a
tool-call result — independent of any SDK. SDK-native objects never cross the
client boundary; the client adapter translates them into these types.

Security. ``MCPServerConfig.environment`` and ``.headers`` may hold secrets
(API keys, auth tokens). They are:
- excluded from ``repr``/``str`` (``repr=False``), so logs never print them;
- excluded from ``public_metadata()`` (the only dump used for API/observability);
- never copied into a ``ToolSpec`` or a ``RuntimeEvent``.

Server configuration comes from trusted composition/admin logic only — never from
untrusted user request input (see the registry manager).
"""

import re
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# server_id participates in the capability id ``mcp.<server_id>.<tool>`` and in
# handler routing, so it must be a stable, dot-free, whitespace-free token to
# keep server namespaces isolated and ids unambiguous.
_SERVER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class MCPTransport(str, Enum):
    """Supported MCP transports.

    Both are modelled for validation, but only the transport(s) an installed
    dependency supports are actually wired to a real client. The default test
    suite uses a fake client and needs neither.
    """

    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable_http"


class MCPRetryConfig(BaseModel):
    """Per-server transport retry policy (Phase 41A). Deterministic, bounded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_attempts: int = Field(default=2, ge=1)
    base_delay_seconds: float = Field(default=0.1, ge=0)
    max_delay_seconds: float = Field(default=2.0, ge=0)
    backoff: float = Field(default=2.0, ge=1)


class MCPServerConfig(BaseModel):
    """Trusted configuration for one MCP server.

    ``stdio`` requires ``command``; ``streamable_http`` requires ``url``.
    Secrets (``environment``, ``headers``) are redacted from repr and excluded
    from public metadata.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    server_id: str
    name: str
    transport: MCPTransport
    command: list[str] | None = None
    url: str | None = None
    working_directory: str | None = None
    environment: dict[str, str] | None = Field(default=None, repr=False)
    headers: dict[str, str] | None = Field(default=None, repr=False)
    enabled: bool = True
    timeout_seconds: float = Field(default=30.0, gt=0)
    retry: MCPRetryConfig = Field(default_factory=MCPRetryConfig)
    metadata: dict = Field(default_factory=dict)
    # Optional read-only allowlist (Phase 46.2): when set, ONLY these discovered
    # tool names are registered as capabilities; every other advertised tool
    # (including any write tool) is excluded before it can ever become eligible.
    # ``None`` = register all discovered tools (unchanged behavior). Trusted config
    # only — a server can never widen its own allowlist.
    tool_allowlist: list[str] | None = None

    @field_validator("server_id")
    @classmethod
    def _valid_server_id(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("server_id must be a non-empty string")
        if not _SERVER_ID_RE.match(value):
            raise ValueError(
                "server_id must match [A-Za-z0-9][A-Za-z0-9_-]* (no dots or spaces)"
            )
        return value

    @field_validator("name")
    @classmethod
    def _non_empty_name(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("name must be a non-empty string")
        return value

    @model_validator(mode="after")
    def _transport_requirements(self):
        if self.transport == MCPTransport.STDIO:
            if not self.command:
                raise ValueError("stdio transport requires a non-empty command")
        elif self.transport == MCPTransport.STREAMABLE_HTTP:
            if not self.url or not self.url.strip():
                raise ValueError("streamable_http transport requires a url")
        return self

    def public_metadata(self) -> dict:
        """Secret-free view safe for logs, API responses, and observability.

        Deliberately omits ``environment``, ``headers``, and ``url`` (a URL may
        embed credentials). Only non-sensitive routing/identity fields.
        """
        return {
            "server_id": self.server_id,
            "name": self.name,
            "transport": self.transport.value,
            "enabled": self.enabled,
            "timeout_seconds": self.timeout_seconds,
        }


class MCPToolDefinition(BaseModel):
    """A tool as advertised by an MCP server (provider-neutral, post-translation).

    ``input_schema`` is an untrusted JSON-Schema-shaped dict from the server; it
    is validated and size-capped before becoming a ``ToolSpec`` (see the registry
    manager). ``annotations`` carries any server-provided hints.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str = ""
    input_schema: dict = Field(default_factory=dict)
    annotations: dict = Field(default_factory=dict)
    metadata: dict = Field(default_factory=dict)


class MCPToolCallResult(BaseModel):
    """Provider-neutral result of an MCP ``call_tool``.

    ``content`` is the textual/blocks payload; ``structured_content`` is any
    structured JSON result. ``is_error`` marks a remote tool error (the call
    completed but the tool reported failure). SDK-native result objects are never
    stored here — the client adapter normalizes into these fields.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    success: bool = True
    content: list[dict] = Field(default_factory=list)
    structured_content: dict | None = None
    is_error: bool = False
    metadata: dict = Field(default_factory=dict)
