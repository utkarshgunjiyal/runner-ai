"""MCP domain error taxonomy (Phase 39).

Every MCP failure surfaces as one of these domain errors — never a raw SDK or
transport exception. The MCP adapter maps them onto the existing
``AdapterResult`` / recovery taxonomy (``tools/result.py``) so deterministic
recovery keys off ``error_code`` + ``retryable`` with no LLM involved.

``safe_message`` is a generic, vendor-free string; the raw cause (which may hold
server/transport detail) is never exposed beyond the client/adapter boundary.
"""


class MCPError(Exception):
    """Base for all MCP failures. Carries an API-safe classification."""

    error_code = "mcp_error"
    retryable = False
    safe_message = "The MCP request could not be completed."


class MCPServerNotFoundError(MCPError):
    """The referenced server_id is not registered (config-level)."""

    error_code = "mcp_server_not_found"
    retryable = False
    safe_message = "The requested MCP server is not registered."


class MCPConnectionError(MCPError):
    """The MCP server could not be reached / the session could not be opened."""

    error_code = "mcp_connection_error"
    retryable = True
    safe_message = "The MCP server is temporarily unavailable."


class MCPDiscoveryError(MCPError):
    """Tool discovery (list_tools) failed for a server."""

    error_code = "mcp_discovery_error"
    retryable = True
    safe_message = "MCP tool discovery failed."


class MCPToolNotFoundError(MCPError):
    """The requested capability/tool is not registered for its server.

    Non-retryable until an explicit registry refresh re-discovers tools.
    """

    error_code = "mcp_tool_not_found"
    retryable = False
    safe_message = "The requested MCP tool is not available."


class MCPToolInvocationError(MCPError):
    """The remote MCP tool returned an error, or the call otherwise failed.

    Only a safe message is preserved; raw remote/server detail is not surfaced.
    """

    error_code = "mcp_tool_invocation_error"
    retryable = False
    safe_message = "The MCP tool reported an error."


class MCPTimeoutError(MCPError):
    """The MCP call exceeded its timeout. Retryable."""

    error_code = "mcp_timeout"
    retryable = True
    safe_message = "The MCP request timed out."


class MCPProtocolError(MCPError):
    """Malformed / unexpected MCP payload (bad schema, invalid tool definition)."""

    error_code = "mcp_protocol_error"
    retryable = False
    safe_message = "The MCP server returned an unexpected response."


# --------------------------------------------------------------------------- #
# Transport-layer errors (Phase 41A)
#
# Raised by concrete transports (stdio / streamable_http) and the connection
# manager. They subclass ``MCPError`` so the existing ``MCPAdapter`` maps them to
# an ``AdapterResult`` with no change — raw transport/SDK exceptions are wrapped
# here and never leak upward.
# --------------------------------------------------------------------------- #

class TransportError(MCPError):
    """Base for transport-layer failures."""

    error_code = "mcp_transport_error"
    retryable = False
    safe_message = "The MCP transport failed."


class TransportUnavailable(TransportError):
    """The transport could not be established (spawn/connect failed)."""

    error_code = "mcp_transport_unavailable"
    retryable = True
    safe_message = "The MCP server is temporarily unavailable."


class TransportTimeout(TransportError):
    """A transport operation exceeded its deadline."""

    error_code = "mcp_transport_timeout"
    retryable = True
    safe_message = "The MCP request timed out."


class TransportProtocolError(TransportError):
    """Malformed transport framing / invalid JSON-RPC message."""

    error_code = "mcp_transport_protocol_error"
    retryable = False
    safe_message = "The MCP server returned an unexpected response."


class TransportAuthenticationError(TransportError):
    """The transport was rejected for auth reasons (won't fix on retry)."""

    error_code = "mcp_transport_auth_error"
    retryable = False
    safe_message = "The MCP server rejected the connection."


class TransportConnectionLost(TransportError):
    """An established session dropped mid-operation."""

    error_code = "mcp_transport_connection_lost"
    retryable = True
    safe_message = "The MCP connection was lost."


class TransportBusy(TransportError):
    """The transport is at capacity / a concurrent op is in flight."""

    error_code = "mcp_transport_busy"
    retryable = True
    safe_message = "The MCP server is busy; please retry."
