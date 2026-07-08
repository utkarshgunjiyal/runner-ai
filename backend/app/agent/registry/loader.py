"""Registry loader — builds the default ToolRegistry with the initial tools."""

from app.agent.registry.registry import ToolRegistry
from app.agent.tools.internal.specs import internal_tool_specs


def get_default_tool_registry() -> ToolRegistry:
    """Create a ToolRegistry and register all initial internal tool specs."""
    registry = ToolRegistry()
    for spec in internal_tool_specs():
        registry.register(spec)
    return registry
