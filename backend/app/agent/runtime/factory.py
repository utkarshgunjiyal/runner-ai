"""Runtime Factory / Composition Root (Phase 19).

The single place that constructs and wires the default Runner.ai V2 runtime.
This phase is *only* dependency assembly — it contains no runtime, orchestration,
planner, or provider logic; it reuses the existing implementations and returns a
fully wired ``AgentOrchestrator``.

Wiring order:
    Context Engine → Behavior Gate → KeywordCapabilityRetriever → Tool Registry
    → internal adapters (Execution Bridge) → Direct Runtime → Planner Runtime
    → Final Context Builder → DeterministicFinalProvider → AgentOrchestrator

Config-free at construction: the internal adapters lazy-import V1.5 services only
when actually executed, and the default context engine's providers do the same,
so building the runtime touches no database, LLM, or application settings. Every
default is overridable via injection (provider, executor, context engine, …).
"""

from app.agent.context.engine import ContextEngine, default_context_engine
from app.agent.context.final_builder import FinalContextBuilder
from app.agent.gate.behavior_gate import BehaviorGate
from app.agent.capabilities.keyword_retriever import KeywordCapabilityRetriever
from app.agent.llm.final_provider import DeterministicFinalProvider, FinalAnswerProvider
from app.agent.models.tool_spec import ToolSpec
from app.agent.registry.loader import get_default_tool_registry
from app.agent.registry.registry import ToolRegistry
from app.agent.retriever.capability_retriever import HybridCapabilityRetriever
from app.agent.retriever.embedding_retriever import NullEmbeddingRetriever
from app.agent.retriever.reranker import NullReranker
from app.agent.runtime.direct_runtime import DirectRuntime
from app.agent.runtime.orchestrator import AgentOrchestrator
from app.agent.runtime.planner_runtime import PlannerRuntime
from app.agent.tools.internal.document_adapter import DocumentAdapter
from app.agent.tools.internal.job_adapter import JobAdapter
from app.agent.tools.internal.memory_adapter import MemoryAdapter
from app.agent.tools.result import AdapterResult, ErrorCode


class InternalCapabilityExecutor:
    """Execution Bridge for internal tools: binds a ToolSpec id to a Phase 13
    internal adapter capability and returns its AdapterResult.

    Composition glue only — each adapter owns the actual V1.5 call and its
    exception→AdapterResult translation; this just routes by tool id. Satisfies
    DirectRuntime's CapabilityExecutor contract (async execute(tool, args)).
    """

    def __init__(
        self,
        *,
        document_adapter: DocumentAdapter | None = None,
        job_adapter: JobAdapter | None = None,
        memory_adapter: MemoryAdapter | None = None,
    ) -> None:
        documents = document_adapter or DocumentAdapter()
        jobs = job_adapter or JobAdapter()
        memory = memory_adapter or MemoryAdapter()
        # ToolSpec.id -> (adapter, adapter-capability id)
        self._bindings = {
            "search_documents": (documents, DocumentAdapter.RETRIEVE_CHUNKS),
            "get_document_summary": (documents, DocumentAdapter.GET_SUMMARY),
            "get_job_status": (jobs, JobAdapter.GET_STATUS),
            "get_thread_summary": (memory, MemoryAdapter.GET_THREAD_SUMMARY),
            "get_user_preferences": (memory, MemoryAdapter.GET_PREFERENCES),
        }

    def bound_tool_ids(self) -> list[str]:
        return sorted(self._bindings.keys())

    async def execute(self, tool: ToolSpec, args: dict) -> AdapterResult:
        binding = self._bindings.get(tool.id)
        if binding is None:
            return AdapterResult.failure(
                ErrorCode.UNKNOWN_CAPABILITY,
                retryable=False,
                metadata={"tool_id": tool.id, "reason": "no internal adapter binding"},
            )
        adapter, capability = binding
        return await adapter.execute(capability, args)


def build_default_runtime(
    *,
    context_engine: ContextEngine | None = None,
    tool_registry: ToolRegistry | None = None,
    capability_executor=None,
    final_provider: FinalAnswerProvider | None = None,
    plan_source=None,
    top_k: int = 5,
    embedding=None,
    reranker=None,
    final_hybrid_pipeline=None,
) -> AgentOrchestrator:
    """Construct and wire the default runtime, returning an AgentOrchestrator.

    All defaults are real components; each is overridable via injection. The
    ``final_provider`` defaults to the LLM-free ``DeterministicFinalProvider``.

    Capability retrieval runs through the Phase 28 hybrid pipeline: the keyword
    retriever is Stage 1, wrapped by ``HybridCapabilityRetriever``. The default
    ``embedding``/``reranker`` are Null, so ordering is identical to the pure
    keyword retriever until real stages are injected.
    """

    engine = context_engine or default_context_engine()
    registry = tool_registry or get_default_tool_registry()
    retriever = HybridCapabilityRetriever(
        KeywordCapabilityRetriever(registry),
        embedding=embedding or NullEmbeddingRetriever(),
        reranker=reranker or NullReranker(),
    )
    executor = capability_executor or InternalCapabilityExecutor()

    direct_runtime = DirectRuntime(retriever, executor, top_k=top_k)
    planner_runtime = PlannerRuntime(direct_runtime, retriever, top_k=top_k)

    return AgentOrchestrator(
        context_engine=engine,
        behavior_gate=BehaviorGate(),
        direct_runtime=direct_runtime,
        planner_runtime=planner_runtime,
        final_context_builder=FinalContextBuilder(hybrid_pipeline=final_hybrid_pipeline),
        final_provider=final_provider or DeterministicFinalProvider(),
        plan_source=plan_source,
    )


# Alias — either name is acceptable per the phase spec.
build_default_orchestrator = build_default_runtime
