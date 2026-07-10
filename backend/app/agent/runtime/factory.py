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
from app.agent.llm.planner_provider import DeterministicPlannerProvider, V15PlannerProvider
from app.agent.llm.provider_adapter import V15FinalAnswerProvider
from app.agent.models.tool_spec import ToolKind, ToolSpec
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


class CompositeCapabilityExecutor:
    """Kind-routing Execution Bridge (Phase 39).

    Dispatches on ``ToolSpec.kind`` to the executor for that kind — the live-path
    analogue of the Phase 8 AdapterRegistry (which returns ``dict``; this returns
    ``AdapterResult``). Internal tools route to the existing
    ``InternalCapabilityExecutor``; MCP tools route to the ``MCPAdapter``. This is
    the single seam where MCP joins execution, keeping DirectRuntime/PlannerRuntime
    MCP-agnostic. With only the internal executor it behaves identically to it.
    """

    def __init__(self, executors: dict[ToolKind, object]) -> None:
        self._executors = dict(executors)

    async def execute(self, tool: ToolSpec, args: dict) -> AdapterResult:
        executor = self._executors.get(tool.kind)
        if executor is None:
            return AdapterResult.failure(
                ErrorCode.UNKNOWN_CAPABILITY,
                retryable=False,
                metadata={"tool_id": tool.id, "kind": tool.kind.value,
                          "reason": "no executor registered for tool kind"},
            )
        return await executor.execute(tool, args)


def _build_capability_executor(mcp_registry_manager):
    """Default Execution Bridge. Internal-only unless an MCP manager is provided.

    The MCP adapter is imported lazily *inside* this helper so the default runtime
    (and the whole default test suite) never imports the MCP package unless MCP is
    actually configured.
    """
    if mcp_registry_manager is None:
        return InternalCapabilityExecutor()

    from app.agent.tools.mcp_adapter import MCPAdapter  # lazy: MCP-only path

    return CompositeCapabilityExecutor(
        {
            ToolKind.INTERNAL: InternalCapabilityExecutor(),
            ToolKind.MCP: MCPAdapter(mcp_registry_manager),
        }
    )


def build_default_runtime(
    *,
    context_engine: ContextEngine | None = None,
    tool_registry: ToolRegistry | None = None,
    capability_executor=None,
    final_provider: FinalAnswerProvider | None = None,
    final_answer_provider: FinalAnswerProvider | None = None,
    planner_provider=None,
    plan_source=None,
    use_real_llm: bool = False,
    top_k: int = 5,
    embedding=None,
    reranker=None,
    final_hybrid_pipeline=None,
    mcp_registry_manager=None,
) -> AgentOrchestrator:
    """Construct and wire the default runtime, returning an AgentOrchestrator.

    All defaults are real components; each is overridable via injection. The
    ``final_provider`` defaults to the LLM-free ``DeterministicFinalProvider``.

    Capability retrieval runs through the Phase 28 hybrid pipeline: the keyword
    retriever is Stage 1, wrapped by ``HybridCapabilityRetriever``. The default
    ``embedding``/``reranker`` are Null, so ordering is identical to the pure
    keyword retriever until real stages are injected.

    MCP (Phase 39, additive). ``mcp_registry_manager`` is the optional MCP
    composition seam. When ``None`` (the default) the runtime is byte-identical to
    before — no MCP, plain ``InternalCapabilityExecutor``, no MCP imports touched.
    When provided, its *shared* registry becomes the runtime registry (so any
    already-discovered MCP tools participate in the existing hybrid retrieval), and
    execution routes by kind: internal → ``InternalCapabilityExecutor``, MCP →
    ``MCPAdapter``. Server registration/discovery is the composition root's job and
    must happen on the manager *before* calling this; the factory never connects.
    """

    engine = context_engine or default_context_engine()
    if mcp_registry_manager is not None:
        # Share the manager's registry so discovered MCP tools are retrievable.
        registry = mcp_registry_manager.tool_registry
    else:
        registry = tool_registry or get_default_tool_registry()
    retriever = HybridCapabilityRetriever(
        KeywordCapabilityRetriever(registry),
        embedding=embedding or NullEmbeddingRetriever(),
        reranker=reranker or NullReranker(),
    )
    executor = capability_executor or _build_capability_executor(mcp_registry_manager)

    direct_runtime = DirectRuntime(retriever, executor, top_k=top_k)
    planner_runtime = PlannerRuntime(direct_runtime, retriever, top_k=top_k)

    # Providers: deterministic by default (config-free, credential-free); the
    # real V1.5-backed adapters are selected by use_real_llm or explicit
    # injection. Providers are built once here and shared (never per request).
    final_answer = (
        final_answer_provider
        or final_provider
        or (V15FinalAnswerProvider() if use_real_llm else DeterministicFinalProvider())
    )
    planner = planner_provider or (
        V15PlannerProvider() if use_real_llm else DeterministicPlannerProvider()
    )

    return AgentOrchestrator(
        context_engine=engine,
        behavior_gate=BehaviorGate(),
        direct_runtime=direct_runtime,
        planner_runtime=planner_runtime,
        final_context_builder=FinalContextBuilder(hybrid_pipeline=final_hybrid_pipeline),
        final_provider=final_answer,
        planner_provider=planner,
        capability_retriever=retriever,
        plan_source=plan_source,
    )


# Alias — either name is acceptable per the phase spec.
build_default_orchestrator = build_default_runtime
