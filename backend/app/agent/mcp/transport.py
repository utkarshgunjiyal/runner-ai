"""MCP transport abstraction + health model (Phase 41A).

``MCPTransport`` is one live session to a single MCP server. Concrete transports
(``StdioTransport``, ``StreamableHTTPTransport``) implement it; the connection
manager pools them; a ``TransportMCPClient`` adapts them to the unchanged
``MCPClient`` Protocol. The runtime never sees a transport.

Health is a small, secret-free state machine (healthy → degraded → offline) with
``last_success`` / ``last_failure`` / ``last_ping`` timestamps. Transport
internals (pipes, sockets, headers, env) are never exposed — only the health
snapshot and safe error codes cross this boundary.

Config-free: stdlib + pydantic only. Timestamps come from an injected clock so
tests are deterministic.
"""

from abc import ABC, abstractmethod
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.agent.mcp.models import MCPServerConfig, MCPToolCallResult, MCPToolDefinition


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    OFFLINE = "offline"


class ServerHealth(BaseModel):
    """Per-server health record. Mutable; updated by the owning transport/manager.

    Transitions: a success → HEALTHY; a failure degrades (HEALTHY → DEGRADED →
    OFFLINE) once consecutive failures cross the thresholds. Exposes only
    non-sensitive fields via ``snapshot()``.
    """

    model_config = ConfigDict(extra="forbid")

    status: HealthStatus = HealthStatus.OFFLINE
    last_success: float | None = None
    last_failure: float | None = None
    last_ping: float | None = None
    consecutive_failures: int = 0
    #: consecutive failures to reach DEGRADED / OFFLINE
    degrade_after: int = Field(default=1, ge=1)
    offline_after: int = Field(default=3, ge=1)

    def record_success(self, ts: float) -> None:
        self.status = HealthStatus.HEALTHY
        self.consecutive_failures = 0
        self.last_success = ts
        self.last_ping = ts

    def record_failure(self, ts: float) -> None:
        self.consecutive_failures += 1
        self.last_failure = ts
        self.last_ping = ts
        if self.consecutive_failures >= self.offline_after:
            self.status = HealthStatus.OFFLINE
        elif self.consecutive_failures >= self.degrade_after:
            self.status = HealthStatus.DEGRADED

    def mark_offline(self, ts: float) -> None:
        self.status = HealthStatus.OFFLINE
        self.last_ping = ts

    def snapshot(self) -> dict:
        """Secret-free health view for observability/APIs (no transport internals)."""
        return {
            "status": self.status.value,
            "last_success": self.last_success,
            "last_failure": self.last_failure,
            "last_ping": self.last_ping,
            "consecutive_failures": self.consecutive_failures,
        }


class MCPTransport(ABC):
    """One live session to a single MCP server. SDK/transport internals stay here."""

    def __init__(self, config: MCPServerConfig) -> None:
        self._config = config

    @property
    def server_id(self) -> str:
        return self._config.server_id

    @property
    def config(self) -> MCPServerConfig:
        return self._config

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        ...

    @abstractmethod
    async def connect(self) -> None:
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        ...

    @abstractmethod
    async def list_tools(self) -> list[MCPToolDefinition]:
        ...

    @abstractmethod
    async def call_tool(self, tool_name: str, arguments: dict) -> MCPToolCallResult:
        ...

    @abstractmethod
    def health(self) -> ServerHealth:
        ...

    @abstractmethod
    async def close(self) -> None:
        ...


class FakeTransport(MCPTransport):
    """Deterministic in-memory transport for connection/lifecycle/health tests.

    Scripts tool definitions and call results; can simulate connect/call failures
    and a slow (never-returning) call for timeout tests. Records lifecycle calls.
    """

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        tools: list[MCPToolDefinition] | None = None,
        results: dict | None = None,
        clock=None,
        fail_connect: int = 0,
        fail_calls: int = 0,
    ) -> None:
        super().__init__(config)
        self._tools = list(tools or [])
        self._results = dict(results or {})
        self._clock = clock or (lambda: 0.0)
        self._fail_connect = fail_connect
        self._fail_calls = fail_calls
        self._connected = False
        self._health = ServerHealth()
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.close_calls = 0
        self.list_calls = 0
        self.call_calls: list[tuple[str, dict]] = []

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self.connect_calls += 1
        if self._fail_connect > 0:
            self._fail_connect -= 1
            self._health.record_failure(self._clock())
            from app.agent.mcp.errors import TransportUnavailable

            raise TransportUnavailable(f"cannot connect to {self.server_id}")
        self._connected = True
        self._health.record_success(self._clock())

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._connected = False

    async def list_tools(self) -> list[MCPToolDefinition]:
        self.list_calls += 1
        if not self._connected:
            await self.connect()
        return list(self._tools)

    async def call_tool(self, tool_name: str, arguments: dict) -> MCPToolCallResult:
        self.call_calls.append((tool_name, dict(arguments)))
        if not self._connected:
            await self.connect()
        if self._fail_calls > 0:
            self._fail_calls -= 1
            self._health.record_failure(self._clock())
            from app.agent.mcp.errors import TransportConnectionLost

            self._connected = False
            raise TransportConnectionLost(f"lost connection to {self.server_id}")
        spec = self._results.get(tool_name)
        if isinstance(spec, Exception):
            self._health.record_failure(self._clock())
            raise spec
        self._health.record_success(self._clock())
        if spec is not None:
            return spec
        return MCPToolCallResult(
            success=True, content=[{"type": "text", "text": f"{tool_name} ok"}],
            structured_content={"tool": tool_name, "arguments": dict(arguments)},
        )

    def health(self) -> ServerHealth:
        return self._health

    async def close(self) -> None:
        self.close_calls += 1
        self._connected = False
