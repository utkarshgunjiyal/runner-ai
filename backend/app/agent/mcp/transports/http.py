"""Streamable HTTP MCP transport (Phase 41A).

Speaks JSON-RPC 2.0 over HTTP POST (the MCP Streamable HTTP transport) using
``httpx`` — no vendor MCP SDK. A JSON response body is parsed directly; a
``text/event-stream`` body is scanned for the first JSON-RPC ``data:`` frame.
The POST primitive is injectable so the protocol path is tested without a live
server. (Long-lived server→client SSE streaming is out of scope for 41A.)

Auth/transport failures map to the transport error taxonomy; raw HTTP/vendor
detail never escapes.
"""

import json

import httpx

from app.agent.mcp.errors import (
    TransportAuthenticationError,
    TransportProtocolError,
    TransportTimeout,
    TransportUnavailable,
)
from app.agent.mcp.models import MCPServerConfig
from app.agent.mcp.transports.base import BaseJsonRpcTransport

_SESSION_HEADER = "mcp-session-id"


class StreamableHTTPTransport(BaseJsonRpcTransport):
    """MCP over HTTP POST (JSON-RPC), with an injectable ``post`` for tests."""

    def __init__(self, config: MCPServerConfig, *, post=None, clock=None) -> None:
        super().__init__(config, clock=clock)
        self._post = post
        self._client: httpx.AsyncClient | None = None
        self._session_id: str | None = None

    async def _open_channel(self) -> None:
        if self._post is None and self._client is None:
            self._client = httpx.AsyncClient(timeout=self._config.timeout_seconds)

    def _headers(self) -> dict:
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
        }
        if self._config.headers:
            headers.update(self._config.headers)
        if self._session_id:
            headers["mcp-session-id"] = self._session_id
        return headers

    async def _do_post(self, body: dict):
        headers = self._headers()
        url = self._config.url
        if self._post is not None:
            return await self._post(url, headers=headers, json=body,
                                    timeout=self._config.timeout_seconds)
        try:
            return await self._client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise TransportTimeout(f"http timeout: {exc}") from exc
        except httpx.TransportError as exc:
            raise TransportUnavailable(f"http transport error: {exc}") from exc

    @staticmethod
    def _extract_message(response) -> dict:
        status = getattr(response, "status_code", 200)
        if status in (401, 403):
            raise TransportAuthenticationError(f"http {status}")
        if status in (408, 425, 429, 500, 502, 503, 504):
            raise TransportUnavailable(f"http {status}")
        if status >= 400:
            raise TransportProtocolError(f"http {status}")

        headers = getattr(response, "headers", {}) or {}
        content_type = str(headers.get("content-type", "")).lower()
        if "text/event-stream" in content_type:
            text = getattr(response, "text", "") or ""
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    payload = line[len("data:"):].strip()
                    if payload:
                        try:
                            return json.loads(payload)
                        except ValueError as exc:
                            raise TransportProtocolError("invalid SSE JSON") from exc
            raise TransportProtocolError("no data frame in event stream")
        try:
            message = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise TransportProtocolError("invalid JSON http body") from exc
        if not isinstance(message, dict):
            raise TransportProtocolError("http body is not a JSON object")
        return message

    async def _rpc(self, request_id: int, method: str, params: dict) -> dict:
        response = await self._do_post(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        # Capture a server-assigned session id from the initialize response.
        headers = getattr(response, "headers", {}) or {}
        session = headers.get(_SESSION_HEADER)
        if session:
            self._session_id = session
        return self._extract_message(response)

    async def _notify(self, method: str, params: dict) -> None:
        await self._do_post({"jsonrpc": "2.0", "method": method, "params": params})

    async def _close_channel(self) -> None:
        self._session_id = None
        if self._client is not None:
            client, self._client = self._client, None
            await client.aclose()
