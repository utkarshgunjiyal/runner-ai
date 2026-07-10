"""Phase 17 tests — Final LLM Provider Boundary.

Config-free: FinalPrompts are built via FinalContextBuilder (Phase 16) from
hand-constructed RunContexts, and generation uses the deterministic fake
provider. No Mongo/Qdrant/Redis, no application settings, no real LLM. Async
``generate`` is driven via ``asyncio.run`` (no pytest-asyncio dependency).
"""

import ast
import asyncio
import inspect

from app.agent.context.final_builder import FinalContextBuilder
from app.agent.llm import final_provider as provider_module
from app.agent.llm.final_provider import (
    DeterministicFinalProvider,
    FinalAnswer,
    FinalAnswerProvider,
    MessageRole,
    attach_final_answer,
    render_final_prompt,
)
from app.agent.runtime.context import (
    BehaviorPath,
    BehaviorProfile,
    EvidenceItem,
    RunContext,
    ToolOutput,
    WorkingContextItem,
)


def run(coro):
    return asyncio.run(coro)


def sample_run_context():
    rc = RunContext.create(
        "What does the document say about pricing?",
        user_id="u",
        thread_id="t1",
        working_context=[
            WorkingContextItem(source="thread_summary", content="earlier we discussed billing"),
            WorkingContextItem(source="recent_message", content="tell me about pricing", metadata={"seq": 3}),
        ],
    )
    rc.attach_behavior_profile(BehaviorProfile(path=BehaviorPath.DIRECT, reason="doc qa", confidence=0.85))
    rc.attach_selected_capabilities(["search_documents"])
    rc.append_tool_output(
        ToolOutput(capability_id="search_documents", output={"hits": [{"text": "price is $10"}]})
    )
    rc.append_evidence(
        EvidenceItem(source="document", content="The price is $10 per month.", score=0.9,
                     metadata={"page": 2})
    )
    rc.metadata["execution_status"] = "success"
    rc.metadata["direct_runtime"] = {"status": "success", "capability_id": "search_documents"}
    return rc


def sample_prompt():
    return FinalContextBuilder().build(sample_run_context())


# --------------------------------------------------------------------------- #
# Models + protocol
# --------------------------------------------------------------------------- #

def test_final_answer_shape():
    answer = FinalAnswer(
        text="hi",
        used_citations=["E1"],
        usage_metadata={"total_tokens": 3},
        provider="deterministic",
        model="fake-final-1",
        finish_reason="stop",
        metadata={"grounded": True},
    )
    assert answer.text == "hi"
    assert answer.used_citations == ["E1"]
    assert answer.provider == "deterministic"
    assert answer.finish_reason == "stop"


def test_fake_provider_satisfies_protocol():
    provider = DeterministicFinalProvider()
    assert isinstance(provider, FinalAnswerProvider)
    assert hasattr(provider, "generate")
    assert provider.provider and provider.model


# --------------------------------------------------------------------------- #
# Renderer
# --------------------------------------------------------------------------- #

def test_renderer_includes_all_parts():
    messages = render_final_prompt(sample_prompt())
    roles = [m.role for m in messages]
    assert MessageRole.SYSTEM in roles
    assert MessageRole.CONTEXT in roles
    assert MessageRole.EVIDENCE in roles
    assert MessageRole.TOOL in roles
    assert MessageRole.USER in roles
    assert MessageRole.INSTRUCTION in roles


def test_renderer_preserves_ordering():
    messages = render_final_prompt(sample_prompt())
    roles = [m.role for m in messages]
    # System first, instruction last, user request just before instructions.
    assert roles[0] == MessageRole.SYSTEM
    assert roles[-1] == MessageRole.INSTRUCTION
    assert roles[-2] == MessageRole.USER
    # Grounding order: context precedes evidence precedes tool.
    assert roles.index(MessageRole.CONTEXT) < roles.index(MessageRole.EVIDENCE)
    assert roles.index(MessageRole.EVIDENCE) < roles.index(MessageRole.TOOL)


def test_renderer_preserves_within_section_order():
    prompt = sample_prompt()
    messages = render_final_prompt(prompt)
    ctx_contents = [m.content for m in messages if m.role == MessageRole.CONTEXT]
    assert ctx_contents == [s.content for s in prompt.context_sections]


def test_renderer_system_and_user_content():
    prompt = sample_prompt()
    messages = render_final_prompt(prompt)
    system = next(m for m in messages if m.role == MessageRole.SYSTEM)
    user = next(m for m in messages if m.role == MessageRole.USER)
    assert system.content == prompt.system_prompt
    assert user.content == "What does the document say about pricing?"


def test_renderer_evidence_carries_citation_id():
    prompt = sample_prompt()
    messages = render_final_prompt(prompt)
    evidence = next(m for m in messages if m.role == MessageRole.EVIDENCE)
    assert evidence.content.startswith("[E1]")
    assert evidence.metadata["id"] == "E1"


# --------------------------------------------------------------------------- #
# Fake provider
# --------------------------------------------------------------------------- #

def test_fake_provider_deterministic_text():
    prompt = sample_prompt()
    a = run(DeterministicFinalProvider().generate(prompt))
    b = run(DeterministicFinalProvider().generate(prompt))
    assert a.text == b.text
    assert "What does the document say about pricing?" in a.text
    assert a.provider == "deterministic"
    assert a.usage_metadata["total_tokens"] == b.usage_metadata["total_tokens"]


def test_fake_provider_includes_citations():
    prompt = sample_prompt()
    answer = run(DeterministicFinalProvider().generate(prompt))
    assert answer.used_citations == [c.id for c in prompt.citations]
    assert answer.used_citations  # at least the one evidence item
    assert "[E1]" in answer.text


def test_fake_provider_usage_metadata_present():
    answer = run(DeterministicFinalProvider().generate(sample_prompt()))
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        assert key in answer.usage_metadata


# --------------------------------------------------------------------------- #
# RunContext integration
# --------------------------------------------------------------------------- #

def test_attach_final_answer_stores_metadata():
    rc = sample_run_context()
    answer = run(DeterministicFinalProvider().generate(FinalContextBuilder().build(rc)))
    attach_final_answer(rc, answer)

    stored = rc.metadata["final_answer"]
    assert stored["text"] == answer.text
    assert stored["used_citations"] == answer.used_citations
    assert stored["provider"] == "deterministic"
    assert stored["model"] == "fake-final-1"
    assert stored["usage_metadata"]["total_tokens"] == answer.usage_metadata["total_tokens"]


def test_attach_final_answer_preserves_working_context():
    rc = sample_run_context()
    before = [w.content for w in rc.working_context]
    answer = run(DeterministicFinalProvider().generate(FinalContextBuilder().build(rc)))
    attach_final_answer(rc, answer)
    assert [w.content for w in rc.working_context] == before
    assert len(rc.working_context) == 2


# --------------------------------------------------------------------------- #
# Hygiene
# --------------------------------------------------------------------------- #

def _module_level_import_targets(module):
    tree = ast.parse(inspect.getsource(module))
    targets = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            targets += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            targets.append(node.module or "")
    return targets


def test_no_config_db_or_vendor_imports():
    targets = _module_level_import_targets(provider_module)
    banned = (
        "app.config", "app.services", "app.db", "motor", "redis", "qdrant",
        "openai", "anthropic", "google.generativeai", "genai", "llm",
    )
    for name in banned:
        assert not any(name in t for t in targets), (name, targets)
