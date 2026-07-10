"""Tool runner abstraction.

The executor calls a ToolRunner and stays ignorant of internal/API/MCP details.
Phase 7 ships only a FakeToolRunner; real dispatch adapters come later.
"""

from abc import ABC, abstractmethod

from app.agent.models.plan import PlanStep


class ToolRunner(ABC):
    @abstractmethod
    def run(self, step: PlanStep, args: dict) -> dict:
        ...


class FakeToolRunner(ToolRunner):
    """Deterministic stand-in. Returns configured outputs, else an echo.

    Records the order of invoked step ids in ``calls`` for test assertions.
    """

    def __init__(self, outputs: dict[str, dict] | None = None) -> None:
        self._outputs = outputs or {}
        self.calls: list[str] = []

    def run(self, step: PlanStep, args: dict) -> dict:
        self.calls.append(step.id)
        if step.id in self._outputs:
            return self._outputs[step.id]
        return {"ok": True, "step_id": step.id, "args": args}
