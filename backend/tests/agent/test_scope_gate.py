"""Phase 43 — Scope Gate integration through the real orchestrator + coordinator.

Config-free: fake thread-documents + fake scoped retriever injected. Proves
genuine document-ambiguity clarification (WAITING_FOR_USER) and resumed,
validated, grounded continuation over the SAME run.
"""

import asyncio

from app.agent.checkpoint.resume import ResumeResolution
from app.agent.checkpoint.store import InMemoryCheckpointStore
from app.agent.runtime.context import RunContext
from app.agent.runtime.factory import build_default_runtime
from app.agent.runtime.outcome import RuntimeOutcome
from app.agent.runtime.resume_coordinator import AsyncResumeCoordinator
from app.agent.runtime.scope_gate import ScopeGate
from app.agent.tools.result import AdapterResult


def run(coro):
    return asyncio.run(coro)


DOCS = [
    {"document_id": "d1", "filename": "Q3 Report.pdf", "created_at": "2026-01-01"},
    {"document_id": "d2", "filename": "Q4 Report.pdf", "created_at": "2026-02-01"},
]


class FakeContextEngine:
    async def build(self, user_request, user_id, thread_id=None, metadata=None):
        return RunContext.create(
            user_request=user_request, user_id=user_id, thread_id=thread_id,
            metadata=dict(metadata or {}),
        )


class FakeExecutor:
    async def execute(self, tool, args):
        return AdapterResult.ok(output={"a": 1})


class RetrieverSpy:
    def __init__(self):
        self.calls = []

    async def __call__(self, *, query, user_id, document_ids, pages, top_k):
        self.calls.append({"document_ids": list(document_ids), "pages": pages})
        return [
            {"text": f"chunk from {d}", "document_id": d, "page": 1, "score": 0.9}
            for d in document_ids
        ]


async def _thread_docs(user_id, thread_id):
    return DOCS


def _orchestrator(retriever):
    gate = ScopeGate(thread_documents_fn=_thread_docs, document_retriever_fn=retriever)
    return build_default_runtime(
        context_engine=FakeContextEngine(),
        capability_executor=FakeExecutor(),
        scope_gate=gate,
    )


def _coordinator(retriever):
    return AsyncResumeCoordinator(_orchestrator(retriever), InMemoryCheckpointStore())


def test_ambiguous_reference_pauses_for_document_selection():
    retriever = RetrieverSpy()
    coord = _coordinator(retriever)
    result = run(coord.start("summarize the report", "u", thread_id="t1"))
    assert result.result.runtime_outcome == RuntimeOutcome.WAITING_FOR_USER
    assert result.result.pending_action == "select_document"
    assert result.checkpoint_id is not None
    candidates = result.result.metadata["document_candidates"]
    assert {c["document_id"] for c in candidates} == {"d1", "d2"}
    # SAFE candidates only — no content, no storage keys.
    assert set(candidates[0]) == {"document_id", "filename", "created_at"}
    # No retrieval happened while ambiguous.
    assert retriever.calls == []


def test_resume_with_selected_document_validates_and_grounds_the_answer():
    retriever = RetrieverSpy()
    coord = _coordinator(retriever)
    start = run(coord.start("summarize the report", "u", thread_id="t1"))
    checkpoint_id = start.checkpoint_id

    resumed = run(coord.resume(
        checkpoint_id,
        ResumeResolution(kind="clarification", value=["d2"]),
    ))
    assert resumed.result.runtime_outcome == RuntimeOutcome.COMPLETED
    # Retrieval ran for exactly the selected document.
    assert retriever.calls == [{"document_ids": ["d2"], "pages": None}]
    # Same run id across the pause/resume.
    assert resumed.result.run_id == start.result.run_id
    # The resolved chunk is attached as grounding evidence.
    sources = [e.source for e in resumed.result.run_context.evidence]
    assert any("Q4 Report.pdf" in s for s in sources)


def test_resume_rejects_a_document_not_in_the_thread():
    retriever = RetrieverSpy()
    coord = _coordinator(retriever)
    start = run(coord.start("summarize the report", "u", thread_id="t1"))
    resumed = run(coord.resume(
        start.checkpoint_id,
        ResumeResolution(kind="clarification", value=["NOT_MINE"]),
    ))
    # Unauthorized selection → re-clarify (still WAITING), no retrieval.
    assert resumed.result.runtime_outcome == RuntimeOutcome.WAITING_FOR_USER
    assert retriever.calls == []


def test_selected_document_id_skips_clarification_entirely():
    retriever = RetrieverSpy()
    orch = _orchestrator(retriever)
    result = run(orch.run(
        "what does it say about pricing?", "u", thread_id="t1",
        metadata={"selected_document_ids": ["d1"]},
    ))
    assert result.runtime_outcome == RuntimeOutcome.COMPLETED
    assert retriever.calls == [{"document_ids": ["d1"], "pages": None}]


def test_no_document_intent_does_not_invoke_retrieval():
    retriever = RetrieverSpy()
    orch = _orchestrator(retriever)
    result = run(orch.run("hello there", "u", thread_id="t1"))
    assert result.runtime_outcome == RuntimeOutcome.COMPLETED
    assert retriever.calls == []


def test_default_runtime_without_scope_gate_is_unaffected():
    orch = build_default_runtime(context_engine=FakeContextEngine(), capability_executor=FakeExecutor())
    result = run(orch.run("summarize the report", "u", thread_id="t1"))
    # No gate → no pause, no document scope.
    assert result.runtime_outcome == RuntimeOutcome.COMPLETED


def test_scope_gate_sets_intent_excluded_capabilities():
    # Phase 44: a casual message excludes the preference-write + page tools.
    from app.agent.runtime.context import RunContext

    gate = ScopeGate(thread_documents_fn=_thread_docs, document_retriever_fn=RetrieverSpy())
    rc = RunContext.create("this is my persistence test message", user_id="u", thread_id="t1")
    run(gate.evaluate(rc))
    excluded = set(rc.metadata["excluded_capability_ids"])
    assert "save_user_preference" in excluded
    assert "get_page_summary" in excluded


def test_prior_turn_reference_resolves_vague_request():
    # A genuine prior-turn reference (recent_document_fn) resolves a vague phrase
    # even with multiple documents — but "newest doc" is NOT used as the signal.
    retriever = RetrieverSpy()

    async def recent(user_id, thread_id):
        return "d2"

    gate = ScopeGate(
        thread_documents_fn=_thread_docs, document_retriever_fn=retriever, recent_document_fn=recent,
    )
    orch = build_default_runtime(
        context_engine=FakeContextEngine(), capability_executor=FakeExecutor(), scope_gate=gate,
    )
    result = run(orch.run("summarize this document", "u", thread_id="t1"))
    assert result.runtime_outcome == RuntimeOutcome.COMPLETED
    assert retriever.calls == [{"document_ids": ["d2"], "pages": None}]


def test_no_prior_reference_stays_ambiguous():
    retriever = RetrieverSpy()

    async def no_recent(user_id, thread_id):
        return None

    gate = ScopeGate(
        thread_documents_fn=_thread_docs, document_retriever_fn=retriever, recent_document_fn=no_recent,
    )
    orch = build_default_runtime(
        context_engine=FakeContextEngine(), capability_executor=FakeExecutor(), scope_gate=gate,
    )
    result = run(orch.run("summarize this document", "u", thread_id="t1"))
    assert result.runtime_outcome == RuntimeOutcome.WAITING_FOR_USER
    assert retriever.calls == []
