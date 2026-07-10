"""Tool adapter interface.

An adapter knows how to execute one *kind* of tool (INTERNAL / API / MCP). The
Executor will (in a later phase) dispatch on ToolSpec.kind to the matching
adapter and stay ignorant of the underlying implementation.
See docs/architecture/v2.md §11.
"""

from abc import ABC, abstractmethod

from app.agent.models.tool_spec import ToolSpec


class ToolAdapter(ABC):
    @abstractmethod
    def execute(self, tool: ToolSpec, args: dict) -> dict:
        ...
