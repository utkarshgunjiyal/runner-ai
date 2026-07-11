"""Phase 46.1 — the document-inventory fast path through the real orchestrator.

Config-free: a fake ContextEngine seeds a RunContext; the real BehaviorGate /
DirectRuntime / PlannerRuntime / FinalContextBuilder / DeterministicFinalProvider
are wired with fake retrieval/execution; an injected inventory function returns
per-(user, thread) document records. No Mongo/Qdrant/Redis, no LLM. Async ``run``
is driven via ``asyncio.run``.

These assert the production routing: an inventory question is answered
deterministically from the thread's own document records — bypassing capability
retrieval, the planner, document chunk retrieval, and the final provider — with no
stale/foreign evidence and no E# citations, and identical streaming output.
"""

import asyncio
import re

from app.agent.context.final_builder import FinalContextBuilder
from app.agent.gate.behavior_gate import BehaviorGate
from app.agent.llm.final_provider import DeterministicFinalProvider
from app.agent.models.tool_spec import RiskLevel, SideEffectType, ToolKind, ToolSpec
from app.agent.runtime.context import EvidenceItem, RunContext
from app.agent.runtime.direct_runtime import DirectRuntime
from app.agent.runtime.events import RuntimeEventType as E
from app.agent.runtime.orchestrator import AgentOrchestrator
from app.agent.runtime.outcome import RuntimeOutcome
from app.agent.runtime.planner_runtime import PlannerRuntime
from app.agent.capabilities.models import CapabilityMatch, CapabilityRetrievalResponse
from app.agent.tools.result import AdapterResult


def run(coro):
    return asyncio.run(coro)


def _tool(tool_id):
    return ToolSpec(
        id=tool_id, name=tool_id, kind=ToolKind.INTERNAL, description=f"{tool_id} tool",
        input_schema={}, output_schema={}, risk_level=RiskLevel.LOW,
        side_effects=SideEffectType.READ, requires_approval=False,
    )


class FakeContextEngine:
    async def build(self, user_request, user_id, thread_id=None, metadata=None):
        return RunContext.create(
            user_request=user_request, user_id=user_id, thread_id=thread_id,
            working_context=[], metadata=dict(metadata or {}),
        )


class FakeRetriever:
    def __init__(self, tools):
        self._tools = tools

    def _resp(self, query):
        matches = [CapabilityMatch(tool=t, score=float(len(self._tools) - i)) for i, t in enumerate(self._tools)]
        return CapabilityRetrievalResponse(query=query, matches=matches)

    def retrieve(self, request):
        return self._resp(request.query)

    def retrieve_for_run_context(self, run_context, *, top_k=5, **kw):
        return self._resp(run_context.user_request)


class FakeExecutor:
    """Records executed capabilities; every call returns résumé-like evidence
    (so a leak into an inventory answer would be visible)."""

    def __init__(self):
        self.calls = []

    async def execute(self, tool, args):
        self.calls.append(tool.id)
        return AdapterResult.ok(
            output={"answer": "Python, FastAPI from an old resume"},
            evidence=[EvidenceItem(source="document:old_resume.pdf",
                                   content="Skilled in Python and FastAPI (old resume).", score=0.9)],
        )


class FakeInventory:
    """(user_id, thread_id) -> document records. Keyed for isolation tests."""

    def __init__(self, table):
        self._table = table  # {(user_id, thread_id): [ {filename,status}, ... ]}
        self.calls = []

    async def __call__(self, user_id, thread_id):
        self.calls.append((user_id, thread_id))
        return list(self._table.get((user_id, thread_id), []))


def build(inventory_table):
    executor = FakeExecutor()
    retriever = FakeRetriever([_tool("search_documents"), _tool("cap_b")])
    direct = DirectRuntime(retriever, executor)
    planner = PlannerRuntime(direct, retriever)
    inventory = FakeInventory(inventory_table)
    orch = AgentOrchestrator(
        context_engine=FakeContextEngine(),
        behavior_gate=BehaviorGate(),
        direct_runtime=direct,
        planner_runtime=planner,
        final_context_builder=FinalContextBuilder(),
        final_provider=DeterministicFinalProvider(),
        document_inventory_fn=inventory,
    )
    return orch, executor, inventory


def _no_evidence_ids(text):
    return re.search(r"\bE\d+\b", text) is None and "[E" not in text


# --------------------------------------------------------------------------- #
# A. Empty thread
# --------------------------------------------------------------------------- #

def test_empty_thread_says_no_documents_and_bypasses_execution():
    orch, executor, inventory = build({})
    result = run(orch.run("What documents are uploaded?", user_id="u", thread_id="tEmpty"))

    assert result.runtime_outcome == RuntimeOutcome.COMPLETED
    assert "no uploaded documents in this conversation" in result.answer.text
    # Deterministic fast path — no capability execution, no planner, no evidence.
    assert executor.calls == []
    assert result.run_context.evidence == []
    assert result.run_context.tool_outputs == []
    assert result.answer.used_citations == []
    assert _no_evidence_ids(result.answer.text)
    assert result.metadata["deterministic_fast_path"] is True
    assert result.metadata["resolved_intent"] == "document_inventory"
    assert result.metadata["document_count"] == 0
    assert result.answer.provider == "deterministic-inventory"


# --------------------------------------------------------------------------- #
# B / C. One and many documents
# --------------------------------------------------------------------------- #

def test_single_document_listed():
    orch, executor, _ = build({("u", "tA"): [{"filename": "resume.pdf", "status": "completed"}]})
    result = run(orch.run("list my documents", user_id="u", thread_id="tA"))
    assert "1 document is available" in result.answer.text
    assert "- resume.pdf — Ready" in result.answer.text
    assert executor.calls == []


def test_multiple_documents_listed_once_with_count():
    docs = [
        {"filename": "resume.pdf", "status": "completed"},
        {"filename": "report.pdf", "status": "processing"},
        {"filename": "invoice.pdf", "status": "failed"},
    ]
    orch, _, _ = build({("u", "tA"): docs})
    text = run(orch.run("how many documents are attached?", user_id="u", thread_id="tA")).answer.text
    assert "3 documents are available" in text
    for name in ("resume.pdf", "report.pdf", "invoice.pdf"):
        assert text.count(name) == 1
    assert "Ready" in text and "Indexing" in text and "Failed" in text


# --------------------------------------------------------------------------- #
# D. Thread isolation
# --------------------------------------------------------------------------- #

def test_thread_isolation():
    table = {("u", "tA"): [{"filename": "resume.pdf", "status": "completed"}]}  # tB has none
    orch, _, inventory = build(table)
    text_b = run(orch.run("what documents are uploaded?", user_id="u", thread_id="tB")).answer.text
    assert "no uploaded documents" in text_b
    assert "resume.pdf" not in text_b
    # The inventory query was scoped to the ACTIVE thread.
    assert inventory.calls[-1] == ("u", "tB")


# --------------------------------------------------------------------------- #
# E. User isolation
# --------------------------------------------------------------------------- #

def test_user_isolation():
    table = {("owner", "tA"): [{"filename": "secret.pdf", "status": "completed"}]}
    orch, _, inventory = build(table)
    text = run(orch.run("which files do I have?", user_id="intruder", thread_id="tA")).answer.text
    assert "secret.pdf" not in text
    assert "no uploaded documents" in text
    assert inventory.calls[-1] == ("intruder", "tA")


# --------------------------------------------------------------------------- #
# G. Intent variants all take the fast path
# --------------------------------------------------------------------------- #

def test_intent_variants_take_fast_path():
    variants = [
        "Which PDFs do I have?",
        "Show uploaded files",
        "How many documents are attached?",
        "Do I have any documents uploaded?",
    ]
    for text in variants:
        orch, executor, _ = build({("u", "tA"): [{"filename": "a.pdf", "status": "completed"}]})
        result = run(orch.run(text, user_id="u", thread_id="tA"))
        assert result.metadata.get("deterministic_fast_path") is True, text
        assert executor.calls == [], text


# --------------------------------------------------------------------------- #
# H. Negative intents do NOT take the fast path
# --------------------------------------------------------------------------- #

def test_negative_intents_use_normal_path():
    for text in ["Summarize resume.pdf", "Compare these documents",
                 "What does the document say about Python?"]:
        orch, executor, _ = build({("u", "tA"): [{"filename": "a.pdf", "status": "completed"}]})
        result = run(orch.run(text, user_id="u", thread_id="tA"))
        # Not the inventory provider — the normal pipeline ran (executor invoked).
        assert result.answer.provider != "deterministic-inventory", text
        assert result.metadata.get("deterministic_fast_path") is not True, text
        assert executor.calls, text


# --------------------------------------------------------------------------- #
# I. State-leakage regression: retrieval in Thread A, inventory in empty Thread B
# --------------------------------------------------------------------------- #

def test_no_stale_evidence_leaks_into_inventory():
    table = {("u", "tA"): [{"filename": "old_resume.pdf", "status": "completed"}]}
    orch, executor, _ = build(table)

    # First: a real content query in Thread A produces résumé evidence.
    a = run(orch.run("what does the document say about Python?", user_id="u", thread_id="tA"))
    assert executor.calls  # retrieval ran
    assert a.run_context.evidence  # evidence produced

    # Then: an inventory question in an EMPTY Thread B must be clean.
    b = run(orch.run("what documents are uploaded?", user_id="u", thread_id="tB"))
    assert "no uploaded documents" in b.answer.text
    assert "resume" not in b.answer.text.lower()
    assert "Python" not in b.answer.text
    assert b.run_context.evidence == []
    assert _no_evidence_ids(b.answer.text)


# --------------------------------------------------------------------------- #
# J. Streaming equals non-streaming
# --------------------------------------------------------------------------- #

def test_streaming_equals_non_streaming():
    docs = [{"filename": "resume.pdf", "status": "completed"},
            {"filename": "report.pdf", "status": "processing"}]
    orch, _, _ = build({("u", "tA"): docs})

    events = []

    async def sink(event_type, run_id, data):
        events.append((event_type, data))

    async def _drive():
        return await orch.run("list documents", user_id="u", thread_id="tA", stream_sink=sink)

    streamed = run(_drive())
    chunks = [d.get("text", "") for (t, d) in events if t == E.ANSWER_CHUNK]
    completed = [d for (t, d) in events if t == E.ANSWER_COMPLETED]

    non_streamed = run(orch.run("list documents", user_id="u", thread_id="tA")).answer.text
    assert "".join(chunks) == non_streamed
    assert streamed.answer.text == non_streamed
    assert completed and completed[0]["text"] == non_streamed
