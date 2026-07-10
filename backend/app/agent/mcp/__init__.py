"""MCP integration foundation (Phase 39).

Lets Runner.ai connect to MCP servers, discover their tools, register those tools
as first-class ``ToolSpec`` capabilities, and invoke them through the existing
Execution Bridge — normalizing results into ``AdapterResult``.

MCP is an **adapter boundary, not a second runtime**. The planner, orchestrator,
evaluator, repair runtime, and final context builder contain no MCP-specific
logic; discovered MCP tools are indistinguishable from internal ones to them.

No vendor MCP SDK is imported in this package. The client is a Protocol; a
``FakeMCPClient`` backs deterministic tests. A real transport adapter is added
only behind the same Protocol, and only when an MCP dependency is available.
"""
