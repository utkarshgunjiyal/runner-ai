"""MCP connection manager + transport-backed client (Phase 41A).

``MCPConnectionManager`` owns transport sessions: it pools one ``MCPTransport``
per server, connects lazily, reuses sessions across calls, reconnects dropped
sessions (bounded retry with backoff), recycles idle sessions, and closes
everything on shutdown. No transport is created per request.

``TransportMCPClient`` implements the *unchanged* ``MCPClient`` Protocol on top of
the manager, so ``MCPRegistryManager`` / ``MCPAdapter`` / the runtime are the swap
target: replace ``FakeMCPClient`` with ``TransportMCPClient`` and nothing above
changes.

Observability is in-memory (counts + per-server health + last connect latency),
read by the composition root. Config-free: transports and clock/sleep are
injected; timestamps are deterministic in tests.
"""

import asyncio
import time

from app.agent.mcp.errors import MCPError, TransportUnavailable
from app.agent.mcp.models import MCPServerConfig
from app.agent.mcp.transport import MCPTransport, ServerHealth


class MCPConnectionManager:
    """Pools and manages ``MCPTransport`` sessions, one per server."""

    def __init__(
        self,
        transport_factory,
        *,
        clock=None,
        sleep=None,
        idle_timeout: float | None = None,
    ) -> None:
        self._factory = transport_factory
        self._clock = clock or time.time
        self._sleep = sleep or asyncio.sleep
        self._idle_timeout = idle_timeout

        self._transports: dict[str, MCPTransport] = {}
        self._configs: dict[str, MCPServerConfig] = {}
        self._last_used: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._connect_latency_ms: dict[str, float] = {}

        # Observability counters (secret-free).
        self._stats = {
            "connect_attempts": 0,
            "connects": 0,
            "reuses": 0,
            "idle_recycles": 0,
            "failed_reconnects": 0,
            "disconnects": 0,
        }

    # -- Acquisition / lifecycle --------------------------------------------

    async def acquire(self, config: MCPServerConfig) -> MCPTransport:
        """Return a connected transport for ``config`` (lazy connect + reuse)."""
        sid = config.server_id
        self._configs[sid] = config
        lock = self._locks.setdefault(sid, asyncio.Lock())
        async with lock:
            transport = self._transports.get(sid)

            # Idle recycle: drop a stale-but-connected session.
            if (
                transport is not None
                and transport.is_connected
                and self._idle_timeout is not None
                and self._clock() - self._last_used.get(sid, self._clock()) > self._idle_timeout
            ):
                await self._safe_disconnect(transport)
                self._stats["idle_recycles"] += 1
                transport = None

            newly_created = False
            if transport is None:
                transport = self._factory(config)
                self._transports[sid] = transport
                newly_created = True

            if not transport.is_connected:
                await self._connect_with_retry(transport, config.retry, reconnect=not newly_created)
            else:
                self._stats["reuses"] += 1

            self._last_used[sid] = self._clock()
            return transport

    async def connect(self, config: MCPServerConfig) -> MCPTransport:
        """Eagerly connect (and pool) a server's transport."""
        return await self.acquire(config)

    async def disconnect(self, server: MCPServerConfig | str) -> None:
        sid = server if isinstance(server, str) else server.server_id
        lock = self._locks.get(sid)
        if lock is not None:
            async with lock:
                await self._drop(sid)
        else:
            await self._drop(sid)

    async def shutdown(self) -> None:
        """Close every pooled session (best-effort, graceful)."""
        for sid in list(self._transports):
            await self._drop(sid)

    # -- Health / observability ---------------------------------------------

    def health(self, server_id: str) -> ServerHealth | None:
        transport = self._transports.get(server_id)
        return transport.health() if transport is not None else None

    def all_health(self) -> dict[str, dict]:
        return {sid: t.health().snapshot() for sid, t in sorted(self._transports.items())}

    def stats(self) -> dict:
        active = sum(1 for t in self._transports.values() if t.is_connected)
        return {
            **self._stats,
            "pooled": len(self._transports),
            "active_sessions": active,
            "connect_latency_ms": dict(self._connect_latency_ms),
        }

    # -- Internals -----------------------------------------------------------

    async def _connect_with_retry(self, transport, retry, *, reconnect: bool) -> None:
        attempts = max(1, retry.max_attempts)
        delay = retry.base_delay_seconds
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            self._stats["connect_attempts"] += 1
            started = self._clock()
            try:
                await transport.connect()
                self._connect_latency_ms[transport.server_id] = round(
                    (self._clock() - started) * 1000, 3
                )
                self._stats["connects"] += 1
                return
            except MCPError as exc:
                last_exc = exc
                if attempt < attempts:
                    await self._sleep(min(delay, retry.max_delay_seconds))
                    delay *= retry.backoff
        if reconnect:
            self._stats["failed_reconnects"] += 1
        raise last_exc or TransportUnavailable("connect failed")

    async def _drop(self, sid: str) -> None:
        transport = self._transports.pop(sid, None)
        self._last_used.pop(sid, None)
        if transport is not None:
            await self._safe_disconnect(transport)
            self._stats["disconnects"] += 1

    async def _safe_disconnect(self, transport) -> None:
        try:
            await transport.close()
        except Exception:  # noqa: BLE001 - shutdown/recycle must not raise
            pass


class TransportMCPClient:
    """``MCPClient`` implemented over an ``MCPConnectionManager`` (transport swap-in)."""

    def __init__(self, manager: MCPConnectionManager) -> None:
        self._manager = manager

    @property
    def manager(self) -> MCPConnectionManager:
        return self._manager

    async def connect(self, server_config: MCPServerConfig) -> None:
        await self._manager.connect(server_config)

    async def list_tools(self, server_config: MCPServerConfig):
        transport = await self._manager.acquire(server_config)
        return await transport.list_tools()

    async def call_tool(self, server_config: MCPServerConfig, tool_name: str, arguments: dict):
        transport = await self._manager.acquire(server_config)
        return await transport.call_tool(tool_name, arguments)

    async def close(self, server: MCPServerConfig | str) -> None:
        await self._manager.disconnect(server)
