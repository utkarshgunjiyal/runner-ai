"""MCP client boundary (Phase 39).

``MCPClient`` is an async Protocol independent of any SDK. A real transport
adapter (stdio / streamable_http) implements it behind the same interface and is
the *only* place a vendor MCP SDK may be imported. Everything upstream — the
registry manager, the adapter, the runtime — depends on this Protocol and the
provider-neutral models, never on SDK objects.

``FakeMCPClient`` is a deterministic in-memory client for tests and offline runs:
no network, no clock, no SDK. It is configured with per-server tool definitions
and per-tool results, and can simulate connection/timeout/remote-tool failures.
"""

import inspect
from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from app.agent.mcp.errors import (
    MCPConnectionError,
    MCPDiscoveryError,
    MCPToolInvocationError,
)
from app.agent.mcp.models import MCPServerConfig, MCPToolCallResult, MCPToolDefinition


@runtime_checkable
class MCPClient(Protocol):
    """Async MCP client contract. SDK-native objects never cross this boundary."""

    async def connect(self, server_config: MCPServerConfig) -> None:
        ...

    async def list_tools(self, server_config: MCPServerConfig) -> list[MCPToolDefinition]:
        ...

    async def call_tool(
        self, server_config: MCPServerConfig, tool_name: str, arguments: dict
    ) -> MCPToolCallResult:
        ...

    async def close(self, server: MCPServerConfig | str) -> None:
        ...


# A per-tool result can be a fixed result, an exception to raise, or a callable
# over the arguments returning a result.
_ResultSpec = MCPToolCallResult | Exception | Callable[[dict], MCPToolCallResult]


class FakeMCPClient:
    """Deterministic, in-memory ``MCPClient`` for tests.

    Args:
        tools: server_id -> list[MCPToolDefinition] advertised by that server.
        results: (server_id, tool_name) -> result | Exception | callable(args).
        fail_connect: server_ids whose ``connect`` raises MCPConnectionError.
        fail_discovery: server_ids whose ``list_tools`` raises MCPDiscoveryError.

    Records lifecycle interactions (connect/close/list/call counts) so lifecycle
    and sharing tests can assert against them.
    """

    def __init__(
        self,
        *,
        tools: dict[str, list[MCPToolDefinition]] | None = None,
        results: dict[tuple[str, str], _ResultSpec] | None = None,
        fail_connect: set[str] | None = None,
        fail_discovery: set[str] | None = None,
    ) -> None:
        self._tools = tools or {}
        self._results = results or {}
        self._fail_connect = fail_connect or set()
        self._fail_discovery = fail_discovery or set()

        self.connected: set[str] = set()
        self.closed: list[str] = []
        self.connect_calls: list[str] = []
        self.list_tools_calls: list[str] = []
        self.call_tool_calls: list[tuple[str, str, dict]] = []

    async def connect(self, server_config: MCPServerConfig) -> None:
        self.connect_calls.append(server_config.server_id)
        if server_config.server_id in self._fail_connect:
            raise MCPConnectionError(f"cannot connect to {server_config.server_id}")
        self.connected.add(server_config.server_id)

    async def list_tools(self, server_config: MCPServerConfig) -> list[MCPToolDefinition]:
        self.list_tools_calls.append(server_config.server_id)
        if server_config.server_id in self._fail_discovery:
            raise MCPDiscoveryError(f"discovery failed for {server_config.server_id}")
        if server_config.server_id not in self.connected:
            await self.connect(server_config)
        return list(self._tools.get(server_config.server_id, []))

    async def call_tool(
        self, server_config: MCPServerConfig, tool_name: str, arguments: dict
    ) -> MCPToolCallResult:
        self.call_tool_calls.append((server_config.server_id, tool_name, dict(arguments)))
        if server_config.server_id not in self.connected:
            await self.connect(server_config)

        spec = self._results.get((server_config.server_id, tool_name))
        if spec is None:
            # No scripted result: default deterministic echo.
            return MCPToolCallResult(
                success=True,
                content=[{"type": "text", "text": f"{tool_name} ok"}],
                structured_content={"tool": tool_name, "arguments": dict(arguments)},
            )
        if isinstance(spec, Exception):
            raise spec
        if callable(spec):
            out = spec(dict(arguments))
            if inspect.isawaitable(out):  # support async result factories
                out = await out
            return out
        return spec

    async def close(self, server: MCPServerConfig | str) -> None:
        server_id = server if isinstance(server, str) else server.server_id
        self.closed.append(server_id)
        self.connected.discard(server_id)


# Convenience alias for a plain async result factory used in a couple of tests.
ResultFactory = Callable[[dict], Awaitable[MCPToolCallResult]]
