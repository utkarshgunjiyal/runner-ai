"""AdapterToolRunner — bridges the executor's ToolRunner to the AdapterRegistry.

Flow: PlanExecutor → ToolRunner → ToolRegistry (ToolSpec) → AdapterRegistry
(ToolAdapter by kind) → output dict → ExecutionState.

Still no real adapters: this only wires the dispatch path. See
docs/architecture/v2.md §11.
"""

from app.agent.execution.runner import ToolRunner
from app.agent.models.plan import PlanStep
from app.agent.registry.registry import ToolRegistry
from app.agent.tools.adapter_registry import AdapterRegistry


class AdapterToolRunnerError(Exception):
    """Base error for the adapter tool runner."""


class StepCapabilityMissingError(AdapterToolRunnerError):
    """Raised when a step has no capability_id to dispatch on."""


class AdapterToolRunner(ToolRunner):
    def __init__(
        self,
        tool_registry: ToolRegistry,
        adapter_registry: AdapterRegistry,
    ) -> None:
        self._tool_registry = tool_registry
        self._adapter_registry = adapter_registry

    def run(self, step: PlanStep, args: dict) -> dict:
        if not step.capability_id:
            raise StepCapabilityMissingError(
                f"step '{step.id}' has no capability_id to dispatch"
            )

        # ToolNotFoundError / AdapterNotFoundError intentionally propagate.
        tool = self._tool_registry.get(step.capability_id)
        adapter = self._adapter_registry.get(tool.kind)
        return adapter.execute(tool, args)
