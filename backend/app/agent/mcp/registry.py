"""MCP discovery + registration manager (Phase 39).

``MCPRegistryManager`` owns the MCP side of the capability lifecycle:

    register_server(config)  → discover_server_tools(server_id)
      → MCPClient.list_tools → normalize → ToolSpec (kind=MCP)
      → register into the SHARED ToolRegistry
      → participates in the existing HybridCapabilityRetriever (no separate path)

It converts each untrusted ``MCPToolDefinition`` into a normalized ``ToolSpec``
with a stable ``mcp.<server_id>.<tool_name>`` capability id, keeps the
authoritative routing binding for the adapter (``resolve_tool``), and owns
connection/session lifecycle (close at shutdown; explicit refresh).

Security. Discovered metadata is untrusted: tool names and schemas are
validated, descriptions/schemas are size-capped, duplicates are rejected, and an
MCP server can never overwrite an internal capability id (ids are server-scoped
and the registry rejects duplicates). Secrets in the server config never reach a
``ToolSpec``. Server registration comes from trusted composition only.

Config-free: no SDK, no settings, no database. The client is injected.
"""

import asyncio
import json

from pydantic import BaseModel, ConfigDict

from app.agent.mcp.client import MCPClient
from app.agent.mcp.errors import (
    MCPProtocolError,
    MCPServerNotFoundError,
)
from app.agent.mcp.models import MCPServerConfig, MCPToolDefinition
from app.agent.models.tool_spec import (
    LatencyClass,
    RiskLevel,
    SideEffectType,
    ToolKind,
    ToolSpec,
)
from app.agent.registry.registry import DuplicateToolError, ToolRegistry

# Caps on untrusted server-advertised metadata.
_MAX_DESCRIPTION_CHARS = 4000
_MAX_SCHEMA_BYTES = 20_000
_MAX_TOOL_NAME_CHARS = 128

# Tool names may be namespaced by the server (snake_case, dots, slashes, colons)
# but must be a single safe token with no whitespace/control characters.
_ALLOWED_TOOL_NAME = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-:/"
)


def mcp_capability_id(server_id: str, tool_name: str) -> str:
    """Stable, server-namespaced capability id: ``mcp.<server_id>.<tool_name>``."""
    return f"mcp.{server_id}.{tool_name}"


def mcp_handler_ref(server_id: str, tool_name: str) -> str:
    """Inspectable routing hint stored on ToolSpec.handler_ref (no secrets)."""
    return f"mcp:{server_id}:{tool_name}"


class MCPToolBinding(BaseModel):
    """Authoritative routing binding the MCP adapter resolves a capability to."""

    model_config = ConfigDict(frozen=True)

    capability_id: str
    server_id: str
    tool_name: str


def _validate_tool_name(name: str) -> str:
    if not name or not name.strip():
        raise MCPProtocolError("MCP tool name must be a non-empty string")
    if len(name) > _MAX_TOOL_NAME_CHARS:
        raise MCPProtocolError("MCP tool name exceeds the maximum length")
    if any(ch not in _ALLOWED_TOOL_NAME for ch in name):
        raise MCPProtocolError(f"MCP tool name has invalid characters: {name!r}")
    return name


def _validate_schema(schema: dict) -> dict:
    if not isinstance(schema, dict):
        raise MCPProtocolError("MCP input_schema must be a JSON object")
    try:
        size = len(json.dumps(schema))
    except (TypeError, ValueError) as exc:
        raise MCPProtocolError("MCP input_schema is not JSON-serializable") from exc
    if size > _MAX_SCHEMA_BYTES:
        raise MCPProtocolError("MCP input_schema exceeds the maximum size")
    return schema


def _keywords(server_id: str, tool_name: str, description: str) -> list[str]:
    raw = tool_name.replace("/", " ").replace(":", " ").replace(".", " ").replace("_", " ")
    tokens = [t.lower() for t in raw.split() if t]
    # A few description tokens help keyword retrieval without bloating the spec.
    desc_tokens = [t.lower() for t in description.split()[:12] if t.isalpha()]
    seen: list[str] = []
    for tok in [*tokens, *desc_tokens]:
        if tok not in seen:
            seen.append(tok)
    return seen


def convert_tool_definition(
    server_config: MCPServerConfig, definition: MCPToolDefinition
) -> ToolSpec:
    """Normalize one untrusted MCP tool definition into a ToolSpec (kind=MCP).

    Validates/caps the name, description, and schema. Carries only non-secret
    routing metadata (server id + tool name via handler_ref + tags). Never copies
    environment/headers/url from the server config.
    """
    tool_name = _validate_tool_name(definition.name)
    server_id = server_config.server_id
    schema = _validate_schema(definition.input_schema or {})

    description = (definition.description or "").strip()
    if len(description) > _MAX_DESCRIPTION_CHARS:
        description = description[:_MAX_DESCRIPTION_CHARS]
    if not description:
        description = f"MCP tool '{tool_name}' from server '{server_id}'."

    return ToolSpec(
        id=mcp_capability_id(server_id, tool_name),
        name=tool_name,
        kind=ToolKind.MCP,
        description=description,
        tags=["mcp", server_id],
        capability_tags=["mcp", server_id],
        keywords=_keywords(server_id, tool_name, description),
        input_schema=schema,
        output_schema={},
        # MCP tools reach an external system: medium risk, external side effect,
        # data egress. Not forced to approval (that is a policy-layer decision),
        # and never cacheable (governance invariant for EXTERNAL side effects).
        risk_level=RiskLevel.MEDIUM,
        side_effects=SideEffectType.EXTERNAL,
        requires_approval=False,
        data_egress=True,
        handler_ref=mcp_handler_ref(server_id, tool_name),
        latency_class=LatencyClass.HIGH,
        cacheable=False,
    )


class MCPRegistryManager:
    """Discovers MCP tools and registers them as capabilities in a shared registry."""

    def __init__(
        self,
        tool_registry: ToolRegistry,
        client: MCPClient,
        *,
        spec_transform=None,
    ) -> None:
        self._registry = tool_registry
        self._client = client
        self._servers: dict[str, MCPServerConfig] = {}
        self._server_tool_ids: dict[str, list[str]] = {}
        self._bindings: dict[str, MCPToolBinding] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        # Optional per-server ToolSpec enrichment (Phase 46.2). A trusted callable
        # ``(MCPServerConfig, tool_name, ToolSpec) -> ToolSpec`` that adds provider/
        # scope tags, richer retrieval metadata, timeouts, etc. Must preserve the
        # spec id and never inject secrets. ``None`` → specs are used as converted.
        self._spec_transform = spec_transform
        # Per-server discovery stats (safe, secret-free) for observability/status.
        self._discovery_stats: dict[str, dict] = {}

    # -- Accessors -----------------------------------------------------------

    @property
    def tool_registry(self) -> ToolRegistry:
        return self._registry

    @property
    def client(self) -> MCPClient:
        return self._client

    def list_servers(self) -> list[dict]:
        """Secret-free public metadata for every registered server."""
        return [self._servers[sid].public_metadata() for sid in sorted(self._servers)]

    def list_discovered_tools(self) -> list[str]:
        """All discovered capability ids, sorted (stable)."""
        return sorted(self._bindings.keys())

    def resolve_tool(self, capability_id: str) -> MCPToolBinding | None:
        """Authoritative (server_id, tool_name) binding for a capability id."""
        return self._bindings.get(capability_id)

    def get_server_config(self, server_id: str) -> MCPServerConfig | None:
        return self._servers.get(server_id)

    # -- Server registration -------------------------------------------------

    async def register_server(self, config: MCPServerConfig) -> None:
        if config.server_id in self._servers:
            raise ValueError(f"MCP server already registered: {config.server_id!r}")
        self._servers[config.server_id] = config
        self._server_tool_ids.setdefault(config.server_id, [])
        self._locks.setdefault(config.server_id, asyncio.Lock())

    async def unregister_server(self, server_id: str) -> None:
        if server_id not in self._servers:
            raise MCPServerNotFoundError(f"unknown MCP server: {server_id!r}")
        lock = self._locks.get(server_id)
        if lock is not None:
            async with lock:
                self._remove_server_tools(server_id)
        else:
            self._remove_server_tools(server_id)
        await self._client.close(server_id)
        self._servers.pop(server_id, None)
        self._server_tool_ids.pop(server_id, None)
        self._locks.pop(server_id, None)

    # -- Discovery -----------------------------------------------------------

    async def discover_server_tools(self, server_id: str) -> list[ToolSpec]:
        """Discover + register a server's tools (idempotent under concurrency).

        A per-server lock serializes concurrent discovery for the same server, so
        a duplicate concurrent call does not double-register. Already-discovered
        servers return their existing specs without re-calling the client.
        """
        config = self._require_server(server_id)
        async with self._lock_for(server_id):
            if self._server_tool_ids.get(server_id):
                return self._current_specs(server_id)
            return await self._discover_locked(config)

    async def refresh_server_tools(self, server_id: str) -> list[ToolSpec]:
        """Re-discover a server's tools, replacing stale definitions safely.

        Discovery/conversion happen *before* any registry mutation, so a discovery
        failure leaves the previously registered tools intact.
        """
        config = self._require_server(server_id)
        async with self._lock_for(server_id):
            return await self._discover_locked(config, replace=True)

    async def close(self) -> None:
        """Close all client sessions (shutdown boundary). Never raises."""
        for server_id in list(self._servers):
            try:
                await self._client.close(server_id)
            except Exception:  # noqa: BLE001 - shutdown must be best-effort
                pass

    # -- Internals -----------------------------------------------------------

    def _require_server(self, server_id: str) -> MCPServerConfig:
        config = self._servers.get(server_id)
        if config is None:
            raise MCPServerNotFoundError(f"unknown MCP server: {server_id!r}")
        return config

    def _lock_for(self, server_id: str) -> asyncio.Lock:
        return self._locks.setdefault(server_id, asyncio.Lock())

    async def _discover_locked(
        self, config: MCPServerConfig, *, replace: bool = False
    ) -> list[ToolSpec]:
        # 1. Fetch + normalize first (may raise MCPDiscoveryError/MCPProtocolError)
        #    — no registry mutation yet, so failures don't corrupt existing tools.
        definitions = await self._client.list_tools(config)
        specs = self._normalize_definitions(config, definitions)

        # 2. Only now mutate the shared registry.
        if replace:
            self._remove_server_tools(config.server_id)

        registered: list[ToolSpec] = []
        for spec, binding in specs:
            try:
                self._registry.register(spec)
            except DuplicateToolError as exc:
                # An id already exists and is NOT owned by this server → a real
                # collision (e.g. an internal capability). Roll back this batch.
                self._rollback(config.server_id, registered)
                raise MCPProtocolError(
                    f"MCP capability id collides with an existing capability: {spec.id}"
                ) from exc
            self._server_tool_ids.setdefault(config.server_id, []).append(spec.id)
            self._bindings[spec.id] = binding
            registered.append(spec)
        return registered

    def _normalize_definitions(
        self, config: MCPServerConfig, definitions: list[MCPToolDefinition]
    ) -> list[tuple[ToolSpec, MCPToolBinding]]:
        out: list[tuple[ToolSpec, MCPToolBinding]] = []
        seen_ids: set[str] = set()
        allowlist = set(config.tool_allowlist) if config.tool_allowlist is not None else None
        discovered = 0
        excluded = 0
        for definition in definitions:
            discovered += 1
            # Read-only allowlist (Phase 46.2): a server can advertise write tools
            # (create issue, merge PR, push files, …); when an allowlist is set,
            # only listed tools are ever registered — the rest are excluded here,
            # before they can become eligible or reach the planner.
            if allowlist is not None and definition.name not in allowlist:
                excluded += 1
                continue
            spec = convert_tool_definition(config, definition)
            if self._spec_transform is not None:
                spec = self._spec_transform(config, definition.name, spec)
                if spec.id != mcp_capability_id(config.server_id, definition.name):
                    raise MCPProtocolError("spec_transform must not change the capability id")
            if spec.id in seen_ids:
                # One server advertised the same tool twice.
                raise MCPProtocolError(f"duplicate MCP tool id in discovery: {spec.id}")
            seen_ids.add(spec.id)
            binding = MCPToolBinding(
                capability_id=spec.id,
                server_id=config.server_id,
                tool_name=definition.name,
            )
            out.append((spec, binding))
        self._discovery_stats[config.server_id] = {
            "discovered_tool_count": discovered,
            "excluded_tool_count": excluded,
            "allowed_tool_count": len(out),
        }
        return out

    def discovery_stats(self, server_id: str) -> dict:
        """Safe, secret-free discovery counts for a server (observability/status)."""
        return dict(self._discovery_stats.get(server_id, {}))

    def _remove_server_tools(self, server_id: str) -> None:
        for tool_id in self._server_tool_ids.get(server_id, []):
            self._registry.unregister(tool_id)
            self._bindings.pop(tool_id, None)
        self._server_tool_ids[server_id] = []

    def _rollback(self, server_id: str, registered: list[ToolSpec]) -> None:
        owned = self._server_tool_ids.get(server_id, [])
        for spec in registered:
            self._registry.unregister(spec.id)
            self._bindings.pop(spec.id, None)
            if spec.id in owned:
                owned.remove(spec.id)

    def _current_specs(self, server_id: str) -> list[ToolSpec]:
        return [
            self._registry.get(tool_id)
            for tool_id in self._server_tool_ids.get(server_id, [])
        ]
