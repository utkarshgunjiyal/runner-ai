"""MCP transport composition (Phase 41A).

Builds the production transport stack from *trusted* server configs — the only
place that maps a ``MCPTransport`` enum to a concrete transport. Takes configs as
arguments (never reads ``app.config``), so the MCP package stays config-free and
the real composition root (``app/main.py``) owns lifecycle: it passes the
settings-derived configs in and calls ``shutdown`` on the manager at teardown.

    configs → default_transport_factory → MCPConnectionManager → TransportMCPClient
            → MCPRegistryManager → MCPCapabilitySource → runtime (unchanged)
"""

from app.agent.mcp.connection import MCPConnectionManager, TransportMCPClient
from app.agent.mcp.models import MCPServerConfig, MCPTransport


def default_transport_factory(*, clock=None):
    """Return a factory ``(MCPServerConfig) -> MCPTransport`` selecting by transport.

    Concrete transports are imported lazily so importing this module needs no
    transport dependency until a server is actually built.
    """

    def factory(config: MCPServerConfig):
        if config.transport == MCPTransport.STDIO:
            from app.agent.mcp.transports.stdio import StdioTransport

            return StdioTransport(config, clock=clock)
        if config.transport == MCPTransport.STREAMABLE_HTTP:
            from app.agent.mcp.transports.http import StreamableHTTPTransport

            return StreamableHTTPTransport(config, clock=clock)
        raise ValueError(f"unsupported MCP transport: {config.transport!r}")

    return factory


def build_connection_manager(
    *,
    transport_factory=None,
    clock=None,
    sleep=None,
    idle_timeout: float | None = None,
) -> MCPConnectionManager:
    """Compose a connection manager over the default (or injected) transport factory."""
    return MCPConnectionManager(
        transport_factory or default_transport_factory(clock=clock),
        clock=clock,
        sleep=sleep,
        idle_timeout=idle_timeout,
    )


def build_transport_client(
    *,
    transport_factory=None,
    clock=None,
    sleep=None,
    idle_timeout: float | None = None,
) -> TransportMCPClient:
    """Build a production ``MCPClient`` (transport-backed) plus its manager.

    Returns the ``TransportMCPClient``; its ``.manager`` is the lifecycle owner
    the composition root closes at shutdown.
    """
    manager = build_connection_manager(
        transport_factory=transport_factory, clock=clock, sleep=sleep, idle_timeout=idle_timeout
    )
    return TransportMCPClient(manager)


def validate_server_configs(configs: list[MCPServerConfig]) -> list[MCPServerConfig]:
    """Reject duplicate server ids in a trusted config set (fail fast at startup)."""
    seen: set[str] = set()
    for config in configs:
        if config.server_id in seen:
            raise ValueError(f"duplicate MCP server_id in configuration: {config.server_id!r}")
        seen.add(config.server_id)
    return list(configs)


async def build_mcp_registry_manager(
    configs: list[MCPServerConfig],
    *,
    tool_registry=None,
    transport_factory=None,
    client=None,
    connection_manager=None,
    clock=None,
    sleep=None,
    idle_timeout: float | None = None,
    discover: bool = True,
    spec_transform=None,
):
    """Compose the full transport → registry stack from trusted configs.

    Returns ``(manager, connection_manager)``: the ``MCPRegistryManager`` to hand
    to ``build_default_runtime(mcp_registry_manager=...)`` (or the route hook), and
    the ``MCPConnectionManager`` whose lifecycle the composition root owns
    (``await connection_manager.shutdown()`` at teardown). ``connection_manager``
    is ``None`` when a custom ``client`` is injected (tests).

    Wiring: ConnectionManager → TransportMCPClient → MCPRegistryManager
    (register + optionally discover each enabled server).
    """
    from app.agent.mcp.registry import MCPRegistryManager
    from app.agent.registry.registry import ToolRegistry

    validate_server_configs(configs)

    conn_manager = None
    if client is None:
        conn_manager = connection_manager or build_connection_manager(
            transport_factory=transport_factory, clock=clock, sleep=sleep,
            idle_timeout=idle_timeout,
        )
        client = TransportMCPClient(conn_manager)

    manager = MCPRegistryManager(
        tool_registry or ToolRegistry(), client, spec_transform=spec_transform
    )
    for config in configs:
        if not config.enabled:
            continue
        await manager.register_server(config)
        if discover:
            await manager.discover_server_tools(config.server_id)
    return manager, conn_manager
