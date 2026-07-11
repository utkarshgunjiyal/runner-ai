"""Phase 43 — deterministic end-to-end: threads + documents + ambiguity resume +
no cross-thread leakage. Config-free (in-memory fakes, deterministic providers).

Covers: two threads each with documents, a thread-wide question, a specific-
document question, an ambiguous reference → WAITING_FOR_USER → select → resume
(same run), and a thread switch that never sees the other thread's documents.
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


# A tiny in-memory "database": thread_id -> owned documents.
DB = {
    "threadA": [
        {"document_id": "A1", "filename": "Q3 Report.pdf", "created_at": "2026-01-01"},
        {"document_id": "A2", "filename": "Q4 Report.pdf", "created_at": "2026-02-01"},
    ],
    "threadB": [
        {"document_id": "B1", "filename": "Onboarding.pdf", "created_at": "2026-03-01"},
    ],
}


class FakeContextEngine:
    async def build(self, user_request, user_id, thread_id=None, metadata=None):
        return RunContext.create(user_request, user_id=user_id, thread_id=thread_id,
                                 metadata=dict(metadata or {}))


class FakeExecutor:
    async def execute(self, tool, args):
        return AdapterResult.ok(output={"a": 1})


class Retriever:
    def __init__(self):
        self.calls = []

    async def __call__(self, *, query, user_id, document_ids, pages, top_k):
        self.calls.append(list(document_ids))
        # Only ever returns hits for the requested (already-owned-validated) ids.
        return [{"text": f"chunk {d}", "document_id": d, "page": 1, "score": 0.9} for d in document_ids]


async def _thread_docs(user_id, thread_id):
    # Ownership boundary: a thread only ever sees its OWN documents.
    return list(DB.get(thread_id, []))


def _coordinator(retriever):
    gate = ScopeGate(thread_documents_fn=_thread_docs, document_retriever_fn=retriever)
    orch = build_default_runtime(
        context_engine=FakeContextEngine(), capability_executor=FakeExecutor(), scope_gate=gate,
    )
    return AsyncResumeCoordinator(orch, InMemoryCheckpointStore())


def test_thread_wide_question_searches_only_that_threads_documents():
    retriever = Retriever()
    coord = _coordinator(retriever)
    result = run(coord.start("what do these contracts cover?", "u", thread_id="threadA"))
    assert result.result.runtime_outcome == RuntimeOutcome.COMPLETED
    # Phase 44: multi-document scope retrieves balanced per-document (one call
    # each). The union stays within thread A — nothing from B.
    called = {d for call in retriever.calls for d in call}
    assert called == {"A1", "A2"}


def test_specific_document_question_scopes_to_one_document():
    retriever = Retriever()
    coord = _coordinator(retriever)
    result = run(coord.start("what does Onboarding.pdf say?", "u", thread_id="threadB"))
    assert result.result.runtime_outcome == RuntimeOutcome.COMPLETED
    assert retriever.calls == [["B1"]]


def test_ambiguous_reference_pauses_then_resume_scopes_to_choice():
    retriever = Retriever()
    coord = _coordinator(retriever)
    start = run(coord.start("summarize the report", "u", thread_id="threadA"))
    assert start.result.runtime_outcome == RuntimeOutcome.WAITING_FOR_USER
    assert {c["document_id"] for c in start.result.metadata["document_candidates"]} == {"A1", "A2"}
    assert retriever.calls == []  # nothing retrieved while ambiguous

    resumed = run(coord.resume(start.checkpoint_id, ResumeResolution(kind="clarification", value=["A2"])))
    assert resumed.result.runtime_outcome == RuntimeOutcome.COMPLETED
    assert resumed.result.run_id == start.result.run_id  # same run
    assert retriever.calls == [["A2"]]                    # scoped to the choice


def test_no_cross_thread_leakage_selecting_other_threads_document():
    retriever = Retriever()
    coord = _coordinator(retriever)
    start = run(coord.start("summarize the report", "u", thread_id="threadA"))
    # Attempt to resume with thread B's document id → rejected (not owned by A).
    resumed = run(coord.resume(start.checkpoint_id, ResumeResolution(kind="clarification", value=["B1"])))
    assert resumed.result.runtime_outcome == RuntimeOutcome.WAITING_FOR_USER  # re-clarify
    assert retriever.calls == []  # never retrieved another thread's document


def test_thread_switch_never_sees_other_threads_documents():
    retriever = Retriever()
    coord = _coordinator(retriever)
    # Same user, different thread — B must only ever retrieve B's document.
    run(coord.start("what do these contracts cover?", "u", thread_id="threadA"))
    retriever.calls.clear()
    run(coord.start("what do these contracts cover?", "u", thread_id="threadB"))
    assert retriever.calls == [["B1"]]
    assert "A1" not in sum(retriever.calls, []) and "A2" not in sum(retriever.calls, [])
