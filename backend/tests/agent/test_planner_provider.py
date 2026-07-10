"""Phase 36 tests — PlannerPrompt + PlannerProvider (deterministic + V1.5 adapter).

Config-free: no LLM, no credentials. The real adapter is driven by an injected
fake ``complete``. Async plan() driven via asyncio.run.
"""

import ast
import asyncio
import inspect

import pytest

from app.agent.capabilities.models import CapabilityMatch
from app.agent.llm import planner_provider as planner_module
from app.agent.llm.planner_provider import (
    DeterministicPlannerProvider,
    PlannerOutputParseError,
    PlannerOutputValidationError,
    V15PlannerProvider,
    parse_execution_plan,
)
from app.agent.models.planner_prompt import PlannerPrompt, build_planner_prompt
from app.agent.models.tool_spec import RiskLevel, SideEffectType, ToolKind, ToolSpec
from app.agent.runtime.context import BehaviorPath, BehaviorProfile, RunContext, WorkingContextItem
from app.agent.runtime.planner_runtime import ExecutionPlan


def run(coro):
    return asyncio.run(coro)


def make_match(tool_id, name="", desc=""):
    tool = ToolSpec(id=tool_id, name=name or tool_id, kind=ToolKind.INTERNAL, description=desc or f"{tool_id} tool",
                    input_schema={}, output_schema={}, risk_level=RiskLevel.LOW,
                    side_effects=SideEffectType.READ, requires_approval=False)
    return CapabilityMatch(tool=tool, score=1.0)


def rc():
    r = RunContext.create("summarize and email the team", user_id="u", thread_id="t1",
                          working_context=[WorkingContextItem(source="thread_summary", content="prior")])
    r.attach_behavior_profile(BehaviorProfile(path=BehaviorPath.PLANNER, reason="multi", confidence=0.8))
    return r


# --------------------------------------------------------------------------- #
# PlannerPrompt
# --------------------------------------------------------------------------- #

def test_prompt_contains_request_context_and_topk_capabilities():
    matches = [make_match("search_documents", "Search"), make_match("get_job_status", "Job")]
    prompt = build_planner_prompt(rc(), matches)
    assert prompt.user_request == "summarize and email the team"
    assert [w["content"] for w in prompt.working_context] == ["prior"]
    assert prompt.behavior_profile["path"] == "planner"
    assert {c.id for c in prompt.capabilities} == {"search_documents", "get_job_status"}


def test_prompt_does_not_expose_full_registry():
    # Only the passed top-k matches appear; nothing pulls the registry in.
    prompt = build_planner_prompt(rc(), [make_match("search_documents")], max_capabilities=1)
    assert len(prompt.capabilities) == 1
    assert prompt.allowed_capability_ids() == {"search_documents"}
    # no raw RunContext / tool objects leaked into the prompt payload
    dumped = prompt.model_dump()
    assert "run_context" not in dumped
    assert all(set(c.keys()) <= {"id", "name", "description", "tags"} for c in dumped["capabilities"])


# --------------------------------------------------------------------------- #
# DeterministicPlannerProvider
# --------------------------------------------------------------------------- #

def test_deterministic_provider_produces_valid_plan():
    prompt = build_planner_prompt(rc(), [make_match("search_documents", "Search"), make_match("get_job_status")])
    plan = run(DeterministicPlannerProvider().plan(prompt))
    assert isinstance(plan, ExecutionPlan)
    assert plan.tasks
    assert plan.tasks[0].metadata["capability_id"] in {"search_documents", "get_job_status"}
    assert plan.tasks[0].optional is False  # first task required


def test_deterministic_provider_no_capabilities_falls_back_to_single_task():
    plan = run(DeterministicPlannerProvider().plan(build_planner_prompt(rc(), [])))
    assert len(plan.tasks) == 1
    assert plan.tasks[0].request == "summarize and email the team"


# --------------------------------------------------------------------------- #
# Strict parsing
# --------------------------------------------------------------------------- #

VALID_JSON = """
{"id": "p1", "goal": "do it", "final_response_mode": "summarize_results",
 "tasks": [
   {"id": "t1", "request": "summarize", "capability_id": "search_documents"},
   {"id": "t2", "request": "email", "optional": true, "depends_on": ["t1"]}
 ]}
"""


def test_parse_valid_json_produces_plan():
    plan = parse_execution_plan(VALID_JSON, {"search_documents", "get_job_status"})
    assert plan.id == "p1"
    assert [t.id for t in plan.tasks] == ["t1", "t2"]
    assert plan.tasks[0].metadata["capability_id"] == "search_documents"
    assert plan.tasks[1].optional is True                       # optional preserved
    assert plan.tasks[1].metadata["depends_on"] == ["t1"]        # dependency preserved
    assert plan.tasks[0].metadata["final_response_mode"] == "summarize_results"


def test_malformed_json_raises_parse_error():
    with pytest.raises(PlannerOutputParseError):
        parse_execution_plan("not json {", {"search_documents"})


def test_missing_required_fields_raise_validation_error():
    with pytest.raises(PlannerOutputValidationError):
        parse_execution_plan('{"tasks": []}', set())  # empty tasks
    with pytest.raises(PlannerOutputValidationError):
        parse_execution_plan('{"tasks": [{"id": "t1"}]}', set())  # missing request


def test_unknown_capability_id_rejected():
    bad = '{"tasks": [{"id": "t1", "request": "x", "capability_id": "ghost"}]}'
    with pytest.raises(PlannerOutputValidationError):
        parse_execution_plan(bad, {"search_documents"})


def test_invalid_final_response_mode_rejected():
    bad = '{"final_response_mode": "nope", "tasks": [{"id": "t1", "request": "x"}]}'
    with pytest.raises(PlannerOutputValidationError):
        parse_execution_plan(bad, set())


def test_unknown_dependency_rejected():
    bad = '{"tasks": [{"id": "t1", "request": "x", "depends_on": ["t9"]}]}'
    with pytest.raises(PlannerOutputValidationError):
        parse_execution_plan(bad, set())


# --------------------------------------------------------------------------- #
# V15PlannerProvider (injected fake complete)
# --------------------------------------------------------------------------- #

def test_v15_provider_parses_valid_json():
    async def fake_complete(system, prompt, **kw):
        return VALID_JSON

    prompt = build_planner_prompt(rc(), [make_match("search_documents")])
    plan = run(V15PlannerProvider(complete=fake_complete).plan(prompt))
    assert [t.id for t in plan.tasks] == ["t1", "t2"]


def test_v15_provider_malformed_output_raises_parse_error():
    async def fake_complete(system, prompt, **kw):
        return "```\nnope\n```"

    with pytest.raises(PlannerOutputParseError):
        run(V15PlannerProvider(complete=fake_complete).plan(build_planner_prompt(rc(), [])))


def test_v15_provider_wraps_backend_error():
    from app.agent.llm.provider_adapter import ProviderUnavailableError

    async def boom(system, prompt, **kw):
        raise RuntimeError("vendor exploded")

    with pytest.raises(ProviderUnavailableError):
        run(V15PlannerProvider(complete=boom).plan(build_planner_prompt(rc(), [])))


# --------------------------------------------------------------------------- #
# Hygiene
# --------------------------------------------------------------------------- #

def test_no_vendor_sdk_or_config_imports():
    import app.agent.models.planner_prompt as prompt_mod
    for module in (planner_module, prompt_mod):
        tree = ast.parse(inspect.getsource(module))
        targets = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                targets += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                targets.append(node.module or "")
        for banned in ("openai", "anthropic", "google.generativeai", "genai",
                       "app.config", "app.services"):
            assert not any(banned in t for t in targets), (module.__name__, banned, targets)
