"""Phase 1 tests — ToolSpec model, registry, and default internal tool specs."""

import pytest
from pydantic import ValidationError

from app.agent.models.tool_spec import (
    RiskLevel,
    SideEffectType,
    ToolKind,
    ToolSpec,
)
from app.agent.registry.loader import get_default_tool_registry
from app.agent.registry.registry import (
    DuplicateToolError,
    ToolNotFoundError,
    ToolRegistry,
)

EXPECTED_TOOL_IDS = {
    "search_documents",
    "get_document_summary",
    "get_page_summary",
    "get_thread_summary",
    "get_recent_messages",
    "get_user_preferences",
    "save_user_preference",
    "get_job_status",
    "list_documents",
    "answer_from_context",
}


def make_spec(**overrides) -> ToolSpec:
    """A minimal valid ToolSpec for validation-rule tests."""
    base = dict(
        id="sample_tool",
        name="Sample Tool",
        kind=ToolKind.INTERNAL,
        description="A sample tool.",
        input_schema={},
        output_schema={},
        risk_level=RiskLevel.LOW,
        side_effects=SideEffectType.READ,
        requires_approval=False,
    )
    base.update(overrides)
    return ToolSpec(**base)


# --------------------------------------------------------------------------- #
# Default registry contents
# --------------------------------------------------------------------------- #

def test_default_registry_contains_all_expected_tool_ids():
    registry = get_default_tool_registry()
    ids = {tool.id for tool in registry.list_all()}
    assert ids == EXPECTED_TOOL_IDS


def test_all_default_tools_are_internal():
    registry = get_default_tool_registry()
    assert all(tool.kind == ToolKind.INTERNAL for tool in registry.list_all())
    assert len(registry.filter_by_kind(ToolKind.INTERNAL)) == len(EXPECTED_TOOL_IDS)
    assert registry.filter_by_kind(ToolKind.API) == []


def test_all_default_tools_have_non_empty_descriptions():
    registry = get_default_tool_registry()
    assert all(tool.description.strip() for tool in registry.list_all())


def test_all_default_tools_have_examples_or_typical_questions():
    registry = get_default_tool_registry()
    for tool in registry.list_all():
        assert tool.examples or tool.typical_user_questions, tool.id


# --------------------------------------------------------------------------- #
# Registry behavior
# --------------------------------------------------------------------------- #

def test_duplicate_registration_raises():
    registry = ToolRegistry()
    registry.register(make_spec(id="dup"))
    with pytest.raises(DuplicateToolError):
        registry.register(make_spec(id="dup"))


def test_unknown_get_raises():
    registry = get_default_tool_registry()
    with pytest.raises(ToolNotFoundError):
        registry.get("does_not_exist")


def test_exists():
    registry = get_default_tool_registry()
    assert registry.exists("search_documents")
    assert not registry.exists("nope")


def test_list_all_is_deterministic_and_sorted():
    registry = ToolRegistry()
    for tid in ["c_tool", "a_tool", "b_tool"]:
        registry.register(make_spec(id=tid))
    ids = [tool.id for tool in registry.list_all()]
    assert ids == ["a_tool", "b_tool", "c_tool"]
    # stable across repeated calls
    assert [t.id for t in registry.list_all()] == [t.id for t in registry.list_all()]


def test_list_enabled_excludes_disabled():
    registry = ToolRegistry()
    registry.register(make_spec(id="on", enabled=True))
    registry.register(make_spec(id="off", enabled=False))
    assert [t.id for t in registry.list_enabled()] == ["on"]


def test_filter_by_kind():
    registry = ToolRegistry()
    registry.register(make_spec(id="internal_a", kind=ToolKind.INTERNAL))
    registry.register(
        make_spec(id="api_b", kind=ToolKind.API, handler_ref=None)
    )
    assert [t.id for t in registry.filter_by_kind(ToolKind.INTERNAL)] == ["internal_a"]
    assert [t.id for t in registry.filter_by_kind(ToolKind.API)] == ["api_b"]


def test_filter_by_risk():
    registry = get_default_tool_registry()
    medium = registry.filter_by_risk(RiskLevel.MEDIUM)
    assert [t.id for t in medium] == ["save_user_preference"]
    assert registry.filter_by_risk(RiskLevel.HIGH) == []
    assert len(registry.filter_by_risk(RiskLevel.LOW)) == len(EXPECTED_TOOL_IDS) - 1


def test_filter_by_tag():
    registry = get_default_tool_registry()
    document_ids = {t.id for t in registry.filter_by_tag("documents")}
    assert document_ids == {
        "search_documents",
        "get_document_summary",
        "get_page_summary",
        "list_documents",
    }
    memory_ids = {t.id for t in registry.filter_by_tag("memory")}
    assert "save_user_preference" in memory_ids
    assert registry.filter_by_tag("no_such_tag") == []


# --------------------------------------------------------------------------- #
# Spec metadata invariants
# --------------------------------------------------------------------------- #

def test_save_user_preference_is_medium_and_write():
    registry = get_default_tool_registry()
    tool = registry.get("save_user_preference")
    assert tool.risk_level == RiskLevel.MEDIUM
    assert tool.side_effects == SideEffectType.WRITE
    assert tool.cacheable is False


# --------------------------------------------------------------------------- #
# ToolSpec validation rules
# --------------------------------------------------------------------------- #

def test_high_risk_without_approval_fails_validation():
    with pytest.raises(ValidationError):
        make_spec(risk_level=RiskLevel.HIGH, requires_approval=False)


def test_high_risk_with_approval_is_valid():
    spec = make_spec(risk_level=RiskLevel.HIGH, requires_approval=True)
    assert spec.requires_approval is True


def test_write_cacheable_tool_fails_validation():
    with pytest.raises(ValidationError):
        make_spec(side_effects=SideEffectType.WRITE, cacheable=True)


def test_external_cacheable_tool_fails_validation():
    with pytest.raises(ValidationError):
        make_spec(side_effects=SideEffectType.EXTERNAL, cacheable=True)


def test_empty_id_name_description_fail_validation():
    for field in ("id", "name", "description"):
        with pytest.raises(ValidationError):
            make_spec(**{field: "   "})


def test_non_positive_timeout_and_negative_retries_fail():
    with pytest.raises(ValidationError):
        make_spec(timeout_seconds=0)
    with pytest.raises(ValidationError):
        make_spec(max_retries=-1)
