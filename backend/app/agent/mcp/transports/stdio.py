"""stdio MCP transport (Phase 41A).

Speaks newline-delimited JSON-RPC 2.0 over a child process's stdin/stdout — the
common MCP stdio framing — using only stdlib ``asyncio`` (no vendor SDK). The
subprocess ``spawn`` is injectable so the real protocol path is tested with an
in-memory fake process (no live server).
"""

import asyncio
import json

from app.agent.mcp.errors import (
    TransportConnectionLost,
    TransportProtocolError,
    TransportUnavailable,
)
from app.agent.mcp.models import MCPServerConfig
from app.agent.mcp.transports.base import BaseJsonRpcTransport


async def _default_spawn(config: MCPServerConfig):
    """Launch the server as a child process (real I/O; not exercised in tests)."""
    if not config.command:
        raise TransportUnavailable("stdio transport requires a command")
    env = dict(config.environment) if config.environment else None
    return await asyncio.create_subprocess_exec(
        *config.command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=config.working_directory,
    )


class StdioTransport(BaseJsonRpcTransport):
    """MCP over a subprocess stdio pipe (newline-delimited JSON-RPC)."""

    def __init__(self, config: MCPServerConfig, *, spawn=None, clock=None) -> None:
        super().__init__(config, clock=clock)
        self._spawn = spawn or _default_spawn
        self._proc = None
        self._lock = asyncio.Lock()  # serialize request/response over the pipe

    async def _open_channel(self) -> None:
        self._proc = await self._spawn(self._config)

    async def _write(self, message: dict) -> None:
        line = (json.dumps(message) + "\n").encode()
        stdin = self._proc.stdin
        if stdin is None:
            raise TransportConnectionLost("stdio stdin is closed")
        stdin.write(line)
        drain = getattr(stdin, "drain", None)
        if drain is not None:
            await drain()

    async def _rpc(self, request_id: int, method: str, params: dict) -> dict:
        async with self._lock:
            await self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            # Read until the matching response id (skip notifications / other ids).
            while True:
                raw = await self._proc.stdout.readline()
                if not raw:
                    raise TransportConnectionLost("stdio stream closed")
                try:
                    message = json.loads(raw.decode().strip())
                except (ValueError, UnicodeDecodeError) as exc:
                    raise TransportProtocolError("invalid JSON on stdio") from exc
                if isinstance(message, dict) and message.get("id") == request_id:
                    return message
                # else: a notification or an unrelated id — keep reading.

    async def _notify(self, method: str, params: dict) -> None:
        async with self._lock:
            await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def _close_channel(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        stdin = getattr(proc, "stdin", None)
        if stdin is not None:
            close = getattr(stdin, "close", None)
            if close is not None:
                close()
        terminate = getattr(proc, "terminate", None)
        if terminate is not None:
            try:
                terminate()
            except ProcessLookupError:
                pass
        wait = getattr(proc, "wait", None)
        if wait is not None:
            await wait()
