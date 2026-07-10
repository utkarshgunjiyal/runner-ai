"""MCP execution adapter (Phase 39).

Bridges an MCP-kind ``ToolSpec`` to an MCP server through the injected
``MCPClient``, and normalizes the result into an ``AdapterResult`` — the same
uniform shape internal adapters return. It satisfies the runtime Execution Bridge
contract (``async execute(tool, args) -> AdapterResult``), so ``DirectRuntime`` /
``PlannerRuntime`` invoke MCP tools without any MCP-specific knowledge.

Boundaries:
- Resolves ``(server_id, tool_name)`` from the registry manager (authoritative)
  — never trusts the caller for routing.
- Enforces the server's timeout; maps every MCP failure onto the existing
  ``AdapterResult`` / recovery taxonomy (``error_code`` + ``retryable``).
- Returns only safe provenance metadata (no credentials, headers, env, or URL).
- Never returns SDK-native objects and never leaks raw server/SDK exception text.
"""

import asyncio
import time

from app.agent.mcp.client import MCPClient
from app.agent.mcp.errors import (
    MCPConnectionError,
    MCPError,
    MCPServerNotFoundError,
    MCPTimeoutError,
    MCPToolInvocationError,
    MCPToolNotFoundError,
)
from app.agent.mcp.models import MCPToolCallResult
from app.agent.mcp.registry import MCPRegistryManager
from app.agent.models.tool_spec import ToolSpec
from app.agent.runtime.context import EvidenceItem
from app.agent.tools.result import AdapterResult, ErrorCode

# Map MCP domain error codes onto the existing recovery taxonomy so deterministic
# recovery keys off familiar ErrorCodes; the precise MCP code is kept in metadata.
_ERROR_CODE_MAP = {
    "mcp_timeout": ErrorCode.UPSTREAM_TIMEOUT,
    "mcp_connection_error": ErrorCode.UPSTREAM_UNAVAILABLE,
    "mcp_server_not_found": ErrorCode.UNKNOWN_CAPABILITY,
    "mcp_tool_not_found": ErrorCode.UNKNOWN_CAPABILITY,
    "mcp_discovery_error": ErrorCode.UPSTREAM_UNAVAILABLE,
    "mcp_tool_invocation_error": ErrorCode.UPSTREAM_ERROR,
    "mcp_protocol_error": ErrorCode.UPSTREAM_ERROR,
    "mcp_error": ErrorCode.UPSTREAM_ERROR,
}


class MCPAdapter:
    """Execution Bridge for MCP-kind capabilities."""

    kind_name = "mcp"

    def __init__(self, manager: MCPRegistryManager, *, client: MCPClient | None = None) -> None:
        self._manager = manager
        self._client = client or manager.client

    async def execute(self, tool: ToolSpec, args: dict) -> AdapterResult:
        capability_id = tool.id

        binding = self._manager.resolve_tool(capability_id)
        if binding is None:
            return self._failure(
                MCPToolNotFoundError(),
                server_id=None,
                tool_name=None,
                capability_id=capability_id,
            )

        config = self._manager.get_server_config(binding.server_id)
        if config is None:
            return self._failure(
                MCPServerNotFoundError(),
                server_id=binding.server_id,
                tool_name=binding.tool_name,
                capability_id=capability_id,
            )
        if not config.enabled:
            return self._failure(
                MCPServerNotFoundError("server is disabled"),
                server_id=binding.server_id,
                tool_name=binding.tool_name,
                capability_id=capability_id,
            )

        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(
                self._client.call_tool(config, binding.tool_name, dict(args or {})),
                timeout=config.timeout_seconds,
            )
        except asyncio.TimeoutError:
            return self._failure(
                MCPTimeoutError(), server_id=binding.server_id,
                tool_name=binding.tool_name, capability_id=capability_id,
            )
        except MCPError as exc:
            return self._failure(
                exc, server_id=binding.server_id,
                tool_name=binding.tool_name, capability_id=capability_id,
            )
        except Exception:  # noqa: BLE001 - never leak raw SDK/server exceptions
            return self._failure(
                MCPToolInvocationError(), server_id=binding.server_id,
                tool_name=binding.tool_name, capability_id=capability_id,
            )
        duration_ms = round((time.perf_counter() - started) * 1000, 3)

        return self._to_adapter_result(
            result, binding=binding, capability_id=capability_id, duration_ms=duration_ms
        )

    # -- Result conversion ---------------------------------------------------

    def _to_adapter_result(
        self, result: MCPToolCallResult, *, binding, capability_id, duration_ms
    ) -> AdapterResult:
        provenance = {
            "adapter_type": "mcp",
            "server_id": binding.server_id,
            "tool_name": binding.tool_name,
            "capability_id": capability_id,
            "duration_ms": duration_ms,
        }

        # A remote tool error: the call completed but the tool reported failure.
        if result.is_error or not result.success:
            return AdapterResult.failure(
                _ERROR_CODE_MAP["mcp_tool_invocation_error"],
                retryable=False,
                metadata={**provenance, "mcp_error_code": "mcp_tool_invocation_error",
                          "safe_message": MCPToolInvocationError.safe_message},
            )

        text_blocks = [
            block.get("text", "")
            for block in result.content
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
        ]
        evidence = [
            EvidenceItem(
                source=f"mcp:{binding.server_id}:{binding.tool_name}",
                content=text,
                metadata={"capability_id": capability_id},
            )
            for text in text_blocks
        ]
        output = {
            "content": list(result.content),
            "structured_content": result.structured_content,
        }
        return AdapterResult.ok(output=output, evidence=evidence, metadata=provenance)

    def _failure(self, exc: MCPError, *, server_id, tool_name, capability_id) -> AdapterResult:
        code = _ERROR_CODE_MAP.get(exc.error_code, ErrorCode.UPSTREAM_ERROR)
        return AdapterResult.failure(
            code,
            retryable=bool(exc.retryable),
            metadata={
                "adapter_type": "mcp",
                "server_id": server_id,
                "tool_name": tool_name,
                "capability_id": capability_id,
                "mcp_error_code": exc.error_code,
                # Safe, vendor-free message only — never the raw exception text.
                "safe_message": exc.safe_message,
            },
        )
