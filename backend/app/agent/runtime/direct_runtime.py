"""Direct Runtime (Phase 14).

The first *complete* execution path — for requests the Behavior Gate routed to
DIRECT (no planning, no DAG, no multi-step execution):

    RunContext → Behavior Gate (already ran) → DIRECT → Capability Retrieval
    → Execution Bridge → deterministic Recovery → RunContext updated

One request → one capability → one AdapterResult → done. Deterministic recovery
(retry / fallback capability / partial result / ask user) is the *only* recovery
here — the Reflection LLM is never invoked (ARCHITECTURE.md §20).

Dependencies are injected so this module stays deterministic and config-free:
- a ``CapabilityRetriever`` (existing Capability Retrieval; DirectRuntime never
  sees the full registry, only the top matches it returns), and
- a ``CapabilityExecutor`` — the Execution Bridge contract
  ``async execute(tool, args) -> AdapterResult``. In the wired system this is
  backed by AdapterToolRunner → AdapterRegistry → adapters; here it is any object
  satisfying that contract. No LLM, no database, no application settings.
"""

from enum import Enum
from typing import Protocol

from app.agent.capabilities.models import CapabilityRetrievalRequest
from app.agent.capabilities.retriever import CapabilityRetriever
from app.agent.models.tool_spec import ToolSpec
from app.agent.runtime.context import (
    BehaviorPath,
    RunContext,
    ToolOutput,
)
from app.agent.tools.result import AdapterResult


class DirectRuntimeError(Exception):
    """Base error for the Direct Runtime."""


class NotDirectPathError(DirectRuntimeError):
    """Raised when run() is called on a RunContext not routed to DIRECT."""


class NoBehaviorDecisionError(DirectRuntimeError):
    """Raised when the RunContext carries no Behavior Gate decision."""


class RecoveryStrategy(str, Enum):
    RETRY = "retry"
    FALLBACK = "fallback"
    PARTIAL = "partial"
    ASK_USER = "ask_user"


class ExecutionStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    NEEDS_USER = "needs_user"


class CapabilityExecutor(Protocol):
    """Execution Bridge contract used by the Direct Runtime."""

    async def execute(self, tool: ToolSpec, args: dict) -> AdapterResult:
        ...


class DirectRuntime:
    def __init__(
        self,
        retriever: CapabilityRetriever,
        executor: CapabilityExecutor,
        *,
        top_k: int = 5,
        max_retries: int = 1,
    ) -> None:
        self._retriever = retriever
        self._executor = executor
        self._top_k = top_k
        self._max_retries = max(0, max_retries)

    async def run(self, run_context: RunContext) -> RunContext:
        # 1-2. Read the Behavior Gate decision and require the DIRECT path.
        path = self._resolve_path(run_context)
        if path != BehaviorPath.DIRECT:
            raise NotDirectPathError(
                f"DirectRuntime requires DIRECT path, got '{path.value}'"
            )

        # 3. Retrieve relevant capabilities (top matches only, not the registry).
        matches = self._retriever.retrieve(
            CapabilityRetrievalRequest(query=run_context.user_request, top_k=self._top_k)
        ).matches

        recovery: list[dict] = []
        if not matches:
            # Nothing to run: deterministic ask-user, no execution.
            recovery.append(
                {"strategy": RecoveryStrategy.ASK_USER.value, "reason": "no_capability"}
            )
            self._record(
                run_context,
                tool=None,
                result=None,
                status=ExecutionStatus.NEEDS_USER,
                attempts=0,
                recovery=recovery,
            )
            return run_context

        args = self._build_args(run_context)

        # 4-5-6. Select the best capability and execute it once.
        primary = matches[0].tool
        result = await self._executor.execute(primary, args)
        attempts = 1

        # 7. Deterministic recovery only. Retry a retryable failure...
        while (
            not result.success
            and result.retryable
            and attempts <= self._max_retries
        ):
            recovery.append(
                {
                    "strategy": RecoveryStrategy.RETRY.value,
                    "capability": primary.id,
                    "error_code": result.error_code,
                    "attempt": attempts,
                }
            )
            result = await self._executor.execute(primary, args)
            attempts += 1

        chosen = primary

        # ...then fall back to the next best capability, at most once.
        if not result.success and len(matches) > 1:
            fallback = matches[1].tool
            recovery.append(
                {
                    "strategy": RecoveryStrategy.FALLBACK.value,
                    "from": primary.id,
                    "capability": fallback.id,
                    "error_code": result.error_code,
                }
            )
            result = await self._executor.execute(fallback, args)
            attempts += 1
            chosen = fallback

        # Classify the terminal outcome.
        if result.success and result.partial:
            status = ExecutionStatus.PARTIAL
            recovery.append(
                {"strategy": RecoveryStrategy.PARTIAL.value, "capability": chosen.id}
            )
        elif result.success:
            status = ExecutionStatus.SUCCESS
        else:
            status = ExecutionStatus.NEEDS_USER
            recovery.append(
                {
                    "strategy": RecoveryStrategy.ASK_USER.value,
                    "capability": chosen.id,
                    "error_code": result.error_code,
                }
            )

        # 8-9. Append outputs/evidence/metadata and return the updated context.
        self._record(run_context, chosen, result, status, attempts, recovery)
        return run_context

    # -- Internals -----------------------------------------------------------

    def _resolve_path(self, run_context: RunContext) -> BehaviorPath:
        if run_context.behavior_profile is not None:
            return run_context.behavior_profile.path
        decision = run_context.metadata.get("behavior_decision")
        if isinstance(decision, dict) and "path" in decision:
            return BehaviorPath(decision["path"])
        raise NoBehaviorDecisionError(
            "RunContext has no behavior decision; run the Behavior Gate first"
        )

    def _build_args(self, run_context: RunContext) -> dict:
        args: dict = {
            "query": run_context.user_request,
            "user_id": run_context.user_id,
        }
        if run_context.thread_id is not None:
            args["thread_id"] = run_context.thread_id
        # Caller-supplied concrete args (e.g. job_id, document_id) win.
        args.update(run_context.metadata.get("capability_args") or {})
        return args

    def _record(
        self,
        run_context: RunContext,
        tool: ToolSpec | None,
        result: AdapterResult | None,
        status: ExecutionStatus,
        attempts: int,
        recovery: list[dict],
    ) -> None:
        if tool is not None:
            run_context.attach_selected_capabilities([tool.id])

        if tool is not None and result is not None:
            run_context.append_tool_output(
                ToolOutput(
                    capability_id=tool.id,
                    output=result.output,
                    metadata={
                        "success": result.success,
                        "confidence": result.confidence,
                        "partial": result.partial,
                        "error_code": result.error_code,
                        "retryable": result.retryable,
                    },
                )
            )
            for item in result.evidence:
                run_context.append_evidence(item)

        run_context.metadata["execution_status"] = status.value
        run_context.metadata["direct_runtime"] = {
            "status": status.value,
            "capability_id": tool.id if tool is not None else None,
            "attempts": attempts,
            "success": bool(result.success) if result is not None else False,
            "confidence": result.confidence if result is not None else 0.0,
            "partial": bool(result.partial) if result is not None else False,
            "error_code": result.error_code if result is not None else None,
        }
        if recovery:
            run_context.metadata["recovery_events"] = recovery
