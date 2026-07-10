"""Shared JSON-RPC 2.0 MCP transport base (Phase 41A).

Implements the MCP request sequence — ``initialize`` → ``notifications/initialized``
→ ``tools/list`` → ``tools/call`` — plus health bookkeeping, on top of two
per-transport primitives (``_open_channel`` / ``_rpc`` / ``_notify`` /
``_close_channel``). Concrete transports (stdio, streamable_http) only implement
the byte/HTTP channel; the protocol logic and error/health handling live here.

No vendor MCP SDK: this is a minimal, correct JSON-RPC client over an injectable
channel, so the real protocol path is exercised deterministically in tests.
"""

import time

from app.agent.mcp.errors import (
    MCPError,
    TransportConnectionLost,
    TransportProtocolError,
    TransportUnavailable,
)
from app.agent.mcp.models import MCPToolCallResult, MCPToolDefinition
from app.agent.mcp.transport import MCPTransport, ServerHealth

_PROTOCOL_VERSION = "2025-06-18"
_CLIENT_INFO = {"name": "runner-ai", "version": "2"}


def parse_jsonrpc_result(message: dict, request_id) -> dict:
    """Validate a JSON-RPC response envelope and return its ``result`` dict.

    Raises ``TransportProtocolError`` on malformed frames or a JSON-RPC error
    object (which may carry server detail — only a safe message escapes).
    """
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        raise TransportProtocolError("malformed JSON-RPC frame")
    if "error" in message:
        # Do not surface raw server error text upward.
        raise TransportProtocolError("MCP server returned a JSON-RPC error")
    if message.get("id") != request_id:
        raise TransportProtocolError("JSON-RPC response id mismatch")
    result = message.get("result")
    if not isinstance(result, dict):
        raise TransportProtocolError("JSON-RPC result missing")
    return result


class BaseJsonRpcTransport(MCPTransport):
    """MCP protocol logic shared by concrete transports."""

    def __init__(self, config, *, clock=None) -> None:
        super().__init__(config)
        self._clock = clock or time.time
        self._connected = False
        self._health = ServerHealth()
        self._next_id = 0

    @property
    def is_connected(self) -> bool:
        return self._connected

    def health(self) -> ServerHealth:
        return self._health

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def connect(self) -> None:
        try:
            await self._open_channel()
            request_id = self._new_id()
            raw = await self._rpc(request_id, "initialize", {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": _CLIENT_INFO,
            })
            parse_jsonrpc_result(raw, request_id)
            await self._notify("notifications/initialized", {})
        except MCPError:
            self._connected = False
            self._health.record_failure(self._clock())
            raise
        except Exception as exc:  # noqa: BLE001 - wrap raw channel errors
            self._connected = False
            self._health.record_failure(self._clock())
            raise TransportUnavailable(f"connect failed: {exc}") from exc
        self._connected = True
        self._health.record_success(self._clock())

    async def list_tools(self) -> list[MCPToolDefinition]:
        result = await self._call("tools/list", {})
        tools = result.get("tools", [])
        if not isinstance(tools, list):
            raise TransportProtocolError("tools/list returned a non-list")
        out: list[MCPToolDefinition] = []
        for entry in tools:
            if not isinstance(entry, dict) or not entry.get("name"):
                raise TransportProtocolError("invalid tool definition")
            out.append(MCPToolDefinition(
                name=entry["name"],
                description=entry.get("description") or "",
                input_schema=entry.get("inputSchema") or {},
                annotations=entry.get("annotations") or {},
            ))
        return out

    async def call_tool(self, tool_name: str, arguments: dict) -> MCPToolCallResult:
        result = await self._call("tools/call", {"name": tool_name, "arguments": dict(arguments or {})})
        is_error = bool(result.get("isError", False))
        return MCPToolCallResult(
            success=not is_error,
            content=result.get("content") or [],
            structured_content=result.get("structuredContent"),
            is_error=is_error,
        )

    async def _call(self, method: str, params: dict) -> dict:
        if not self._connected:
            raise TransportConnectionLost("transport is not connected")
        request_id = self._new_id()
        try:
            raw = await self._rpc(request_id, method, params)
            result = parse_jsonrpc_result(raw, request_id)
        except MCPError:
            self._health.record_failure(self._clock())
            raise
        except Exception as exc:  # noqa: BLE001
            self._connected = False
            self._health.record_failure(self._clock())
            raise TransportConnectionLost(f"{method} failed: {exc}") from exc
        self._health.record_success(self._clock())
        return result

    async def disconnect(self) -> None:
        self._connected = False
        try:
            await self._close_channel()
        except Exception:  # noqa: BLE001 - disconnect is best-effort
            pass

    async def close(self) -> None:
        await self.disconnect()

    # -- Per-transport channel primitives ------------------------------------

    async def _open_channel(self) -> None:
        raise NotImplementedError

    async def _rpc(self, request_id: int, method: str, params: dict) -> dict:
        """Send a JSON-RPC request and return the raw response envelope."""
        raise NotImplementedError

    async def _notify(self, method: str, params: dict) -> None:
        raise NotImplementedError

    async def _close_channel(self) -> None:
        raise NotImplementedError
