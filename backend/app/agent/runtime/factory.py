"""Runtime Factory / Composition Root (Phase 19; Phase 40 capability platform).

The single place that constructs and wires the default Runner.ai V2 runtime.
This is *only* dependency assembly — no runtime, orchestration, planner, or
provider logic; it reuses the existing implementations and returns a fully wired
``AgentOrchestrator``.

Wiring order:
    Capability Sources → Unified Capability Registry → Context Engine
    → Behavior Gate → HybridCapabilityRetriever (over the unified registry)
    → Execution Bridge (by-kind executor) → Direct/Planner Runtime
    → Final Context Builder → provider → AgentOrchestrator

Phase 40: capabilities come from *sources* (internal, optional MCP, future)
mounted into one ``UnifiedCapabilityRegistry``. The retriever reads that one
registry; the execution bridge routes by ``ToolKind`` to each source's executor.
Nothing downstream knows a capability's origin. The default runtime (internal
only) is byte-identical to before; adding MCP is composition only.

Config-free at construction: internal adapters lazy-import V1.5 only when
executed; MCP is referenced only through the config-free manager. Every default
is overridable via injection.
"""

from app.agent.context.engine import ContextEngine, default_context_engine
from app.agent.context.final_builder import FinalContextBuilder
from app.agent.gate.behavior_gate import BehaviorGate
from app.agent.capabilities.keyword_retriever import KeywordCapabilityRetriever
from app.agent.execution.capability_executor import (
    CompositeCapabilityExecutor,
    InternalCapabilityExecutor,
)
from app.agent.models.tool_spec import ToolKind
from app.agent.llm.final_provider import DeterministicFinalProvider, FinalAnswerProvider
from app.agent.llm.planner_provider import DeterministicPlannerProvider, V15PlannerProvider
from app.agent.llm.provider_adapter import V15FinalAnswerProvider
from app.agent.registry.registry import ToolRegistry
from app.agent.registry.sources import (
    CapabilitySource,
    InternalCapabilitySource,
    MCPCapabilitySource,
)
from app.agent.registry.unified import UnifiedCapabilityRegistry
from app.agent.retriever.capability_retriever import HybridCapabilityRetriever
from app.agent.retriever.embedding_retriever import NullEmbeddingRetriever
from app.agent.retriever.reranker import NullReranker
from app.agent.runtime.direct_runtime import DirectRuntime
from app.agent.runtime.orchestrator import AgentOrchestrator
from app.agent.runtime.planner_runtime import PlannerRuntime

# Re-exported for backward compatibility (relocated to execution/ in Phase 40).
__all__ = [
    "InternalCapabilityExecutor",
    "CompositeCapabilityExecutor",
    "build_capability_platform",
    "build_default_runtime",
    "build_default_orchestrator",
]


def _default_sources(mcp_registry_manager, sources, mcp_result_normalizers=None) -> list[CapabilitySource]:
    """The default capability sources: internal, plus optional MCP.

    An explicit ``sources`` list fully overrides the defaults. Otherwise the
    internal source is always present; the MCP source is added only when an MCP
    manager is provided (composition-only, as in Phase 39).
    """
    if sources is not None:
        return list(sources)
    result: list[CapabilitySource] = [InternalCapabilitySource()]
    if mcp_registry_manager is not None:
        result.append(
            MCPCapabilitySource(mcp_registry_manager, result_normalizers=mcp_result_normalizers)
        )
    return result


def build_capability_platform(
    *,
    mcp_registry_manager=None,
    sources: list[CapabilitySource] | None = None,
    registry: ToolRegistry | None = None,
    mcp_result_normalizers=None,
) -> UnifiedCapabilityRegistry:
    """Compose capability sources into a ``UnifiedCapabilityRegistry`` (sync).

    Uses each source's already-known specs (``mount_preloaded``) — the composition
    root does any async discovery on a source *before* calling this. The returned
    platform is the lifecycle owner (``refresh`` / ``shutdown``); the runtime reads
    its shared ``tool_registry``.
    """
    unified = UnifiedCapabilityRegistry(registry=registry)
    for source in _default_sources(mcp_registry_manager, sources, mcp_result_normalizers):
        unified.mount_preloaded(source)
    return unified


def _executor_for(kind_map: dict, override):
    """Pick the Execution Bridge executor for the mounted source kinds.

    A single source uses its own executor directly (so an internal-only runtime is
    byte-identical to before); multiple sources route by kind via
    ``CompositeCapabilityExecutor``.

    An ``override`` is the caller's internal-execution bridge (e.g. the
    composition root wiring the internal document adapter to real retrieval). With
    a single mounted kind it IS the whole bridge (internal-only runtimes stay
    byte-identical). With multiple kinds it must govern ONLY ``INTERNAL``
    execution — the other kinds (MCP, future) keep their own source executors, so a
    selected MCP capability still reaches its ``MCPAdapter``. Returning the override
    verbatim here would hand every MCP-kind tool to the internal-only executor,
    which has no binding for ``mcp.*`` ids and fails ``unknown_capability`` before
    the transport is ever reached.
    """
    if override is not None:
        if len(kind_map) <= 1:
            return override
        composed = dict(kind_map)
        composed[ToolKind.INTERNAL] = override
        return CompositeCapabilityExecutor(composed)
    if len(kind_map) == 1:
        return next(iter(kind_map.values()))
    return CompositeCapabilityExecutor(kind_map)


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
    answer_evaluator=None,
    max_repair_rounds: int = 1,
    scope_gate=None,
    document_inventory_fn=None,
    connector_eligibility: bool = False,
    mcp_registry_manager=None,
    capability_sources: list[CapabilitySource] | None = None,
    capability_registry: UnifiedCapabilityRegistry | None = None,
    mcp_result_normalizers=None,
    capability_argument_builder=None,
) -> AgentOrchestrator:
    """Construct and wire the default runtime, returning an AgentOrchestrator.

    All defaults are real components; each is overridable via injection. The
    ``final_provider`` defaults to the LLM-free ``DeterministicFinalProvider``.

    Capability platform (Phase 40). Capabilities come from *sources* mounted into
    a ``UnifiedCapabilityRegistry``; the hybrid retriever reads that one registry
    and the execution bridge routes by ``ToolKind``:
    - default: internal only → executor is ``InternalCapabilityExecutor``
      (byte-identical to before);
    - ``mcp_registry_manager=...``: adds the MCP source → by-kind
      ``CompositeCapabilityExecutor`` (internal + MCP); the manager must already
      have discovered its servers (the factory never connects);
    - ``capability_sources=[...]`` / ``capability_registry=...``: full control for
      custom/future sources and for lifecycle ownership.

    ``tool_registry`` (legacy) uses the caller's registry directly, bypassing the
    source machinery. Config-free at construction.
    """

    engine = context_engine or default_context_engine()

    if tool_registry is not None:
        # Legacy direct-registry mode: caller owns the registry; no source mounts.
        registry = tool_registry
        executor = capability_executor or InternalCapabilityExecutor()
    else:
        platform = capability_registry or build_capability_platform(
            mcp_registry_manager=mcp_registry_manager, sources=capability_sources,
            mcp_result_normalizers=mcp_result_normalizers,
        )
        registry = platform.tool_registry
        executor = _executor_for(platform.executors_by_kind(), capability_executor)

    retriever = HybridCapabilityRetriever(
        KeywordCapabilityRetriever(registry),
        embedding=embedding or NullEmbeddingRetriever(),
        reranker=reranker or NullReranker(),
    )

    # Phase 43: when connector eligibility is enabled, wrap the retriever so the
    # planner never sees a capability whose connector is missing/unhealthy or
    # lacks required scopes. Default off → the retriever is unchanged.
    if connector_eligibility:
        from app.agent.connectors import EligibilityCapabilityRetriever

        retriever = EligibilityCapabilityRetriever(retriever)

    # Phase 44: intent-based capability gating (page/preference tools) — active
    # only when the scope gate is present (it populates the excluded-id set).
    # Without a scope gate the excluded set is empty, so this is a no-op.
    if scope_gate is not None:
        from app.agent.interpret.capability_gate import IntentCapabilityRetriever

        retriever = IntentCapabilityRetriever(retriever)

    direct_runtime = DirectRuntime(
        retriever, executor, top_k=top_k, argument_builder=capability_argument_builder
    )
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
        answer_evaluator=answer_evaluator,
        max_repair_rounds=max_repair_rounds,
        scope_gate=scope_gate,
        document_inventory_fn=document_inventory_fn,
    )


# Alias — either name is acceptable per the phase spec.
build_default_orchestrator = build_default_runtime
