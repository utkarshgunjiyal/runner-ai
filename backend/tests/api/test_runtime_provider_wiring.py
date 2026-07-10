"""Phase 37 tests — runtime provider composition wiring.

Config-free: exercises configure_agent_runtime / build_default_runtime provider
selection and the shared-orchestrator guarantee. No LLM, no credentials.
"""

import ast
import inspect

import app.routes.agent as agent_module
from app.agent.llm.final_provider import DeterministicFinalProvider
from app.agent.llm.planner_provider import DeterministicPlannerProvider, V15PlannerProvider
from app.agent.llm.provider_adapter import V15FinalAnswerProvider
from app.agent.runtime.factory import build_default_runtime


def _reset():
    agent_module.configure_agent_runtime(use_real_llm=False)
    agent_module._orchestrator = None
    agent_module._coordinator = None


# --------------------------------------------------------------------------- #
# Factory selection
# --------------------------------------------------------------------------- #

def test_default_selects_deterministic_providers():
    orch = build_default_runtime()
    assert isinstance(orch._final_provider, DeterministicFinalProvider)
    assert isinstance(orch._planner_provider, DeterministicPlannerProvider)


def test_use_real_llm_selects_v15_providers():
    orch = build_default_runtime(use_real_llm=True)
    assert isinstance(orch._final_provider, V15FinalAnswerProvider)
    assert isinstance(orch._planner_provider, V15PlannerProvider)


# --------------------------------------------------------------------------- #
# Composition hook + sharing
# --------------------------------------------------------------------------- #

def test_configure_agent_runtime_false_is_deterministic():
    try:
        agent_module.configure_agent_runtime(use_real_llm=False)
        orch = agent_module._get_orchestrator()
        assert isinstance(orch._final_provider, DeterministicFinalProvider)
        assert isinstance(orch._planner_provider, DeterministicPlannerProvider)
    finally:
        _reset()


def test_configure_agent_runtime_true_is_v15():
    try:
        agent_module.configure_agent_runtime(use_real_llm=True)
        orch = agent_module._get_orchestrator()
        assert isinstance(orch._final_provider, V15FinalAnswerProvider)
        assert isinstance(orch._planner_provider, V15PlannerProvider)
    finally:
        _reset()


def test_orchestrator_and_providers_are_process_shared():
    try:
        _reset()
        o1 = agent_module._get_orchestrator()
        o2 = agent_module._get_orchestrator()
        assert o1 is o2  # not rebuilt per request
        # /run, /resume and /run/stream resolve to the same orchestrator
        coordinator = agent_module.get_resume_coordinator()
        streamer = agent_module.get_runtime_streamer()
        assert coordinator._orchestrator is o1
        assert streamer._orchestrator is o1
        # providers are the same objects across calls (not recreated)
        assert o1._final_provider is o2._final_provider
        assert o1._planner_provider is o2._planner_provider
    finally:
        _reset()


def test_configure_rebuilds_shared_orchestrator():
    try:
        agent_module.configure_agent_runtime(use_real_llm=False)
        det = agent_module._get_orchestrator()
        assert isinstance(det._planner_provider, DeterministicPlannerProvider)
        agent_module.configure_agent_runtime(use_real_llm=True)  # switch mode
        real = agent_module._get_orchestrator()
        assert real is not det
        assert isinstance(real._planner_provider, V15PlannerProvider)
    finally:
        _reset()


# --------------------------------------------------------------------------- #
# Hygiene
# --------------------------------------------------------------------------- #

def test_routes_config_free_at_import():
    tree = ast.parse(inspect.getsource(agent_module))
    targets = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            targets += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            targets.append(node.module or "")
    for banned in ("app.config", "app.database", "openai", "anthropic", "genai"):
        assert not any(banned in t for t in targets), (banned, targets)
