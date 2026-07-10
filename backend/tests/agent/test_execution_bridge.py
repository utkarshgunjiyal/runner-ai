"""Phase 13 tests — Execution Bridge (adapter results + internal adapters).

Config-free: every adapter is exercised with injected fake callables, so no
Mongo/Qdrant/Redis, no application settings, and no LLM. Async handlers run via
``asyncio.run`` (no pytest-asyncio dependency).
"""

import ast
import asyncio
import inspect

from app.agent.runtime.context import EvidenceItem
from app.agent.tools import result as result_module
from app.agent.tools.internal import (
    base as base_module,
    document_adapter as document_module,
    job_adapter as job_module,
    memory_adapter as memory_module,
)
from app.agent.tools.internal.document_adapter import DocumentAdapter
from app.agent.tools.internal.job_adapter import JobAdapter
from app.agent.tools.internal.memory_adapter import MemoryAdapter
from app.agent.tools.result import AdapterResult, ErrorCode, classify_exception


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# AdapterResult shape
# --------------------------------------------------------------------------- #

def test_adapter_result_success_shape():
    r = AdapterResult.ok(output={"a": 1}, confidence=0.8)
    assert r.success is True
    assert r.output == {"a": 1}
    assert r.error_code is None
    assert r.retryable is False
    assert r.partial is False
    assert r.confidence == 0.8
    assert r.evidence == []


def test_adapter_result_failure_shape():
    r = AdapterResult.failure(ErrorCode.NOT_FOUND, retryable=False, metadata={"x": 1})
    assert r.success is False
    assert r.error_code == "not_found"
    assert r.retryable is False
    assert r.confidence == 0.0
    assert r.evidence == []
    assert r.metadata == {"x": 1}


def test_confidence_clamped():
    assert AdapterResult.ok(confidence=5.0).confidence == 1.0
    assert AdapterResult.ok(confidence=-1.0).confidence == 0.0


# --------------------------------------------------------------------------- #
# Document adapter
# --------------------------------------------------------------------------- #

async def _fake_retrieve(query, user_id, top_k, document_id=None, page=None):
    return [
        {"text": "chunk one", "page": 1, "document_id": "d1", "chunk_index": 0, "score": 0.9},
        {"text": "chunk two", "page": 2, "document_id": "d1", "chunk_index": 1, "score": 0.7},
    ]


def test_document_adapter_returns_evidence():
    adapter = DocumentAdapter(retrieve_fn=_fake_retrieve)
    r = run(adapter.execute("documents.retrieve_chunks", {"query": "q", "user_id": "u"}))
    assert r.success is True
    assert len(r.evidence) == 2
    assert all(isinstance(e, EvidenceItem) for e in r.evidence)
    assert r.evidence[0].content == "chunk one"
    assert r.evidence[0].source == "document"
    assert r.evidence[0].score == 0.9
    assert r.confidence == 0.9  # top hit score
    assert r.output["hits"][1]["chunk_index"] == 1


def test_document_adapter_empty_is_partial():
    async def empty(query, user_id, top_k, document_id=None, page=None):
        return []

    r = run(DocumentAdapter(retrieve_fn=empty).execute(
        "documents.retrieve_chunks", {"query": "q", "user_id": "u"}))
    assert r.success is True
    assert r.evidence == []
    assert r.partial is True
    assert r.confidence == 0.0


def test_document_adapter_missing_args_is_invalid():
    r = run(DocumentAdapter(retrieve_fn=_fake_retrieve).execute(
        "documents.retrieve_chunks", {"query": "q"}))  # no user_id
    assert r.success is False
    assert r.error_code == ErrorCode.INVALID_ARGS


def test_document_adapter_get_summary_maps_output():
    async def summary_fn(document_id, user_id=None):
        return {"summary": "a short summary"}

    r = run(DocumentAdapter(summary_fn=summary_fn).execute(
        "documents.get_summary", {"document_id": "d1"}))
    assert r.success is True
    assert r.output["summary"] == "a short summary"
    assert r.evidence[0].source == "document_summary"


# --------------------------------------------------------------------------- #
# Job adapter
# --------------------------------------------------------------------------- #

def test_job_adapter_maps_service_output():
    async def get_job(job_id, user_id=None):
        return {"job_id": job_id, "status": "completed", "progress": 100}

    r = run(JobAdapter(get_job_fn=get_job).execute("jobs.get_status", {"job_id": "j1"}))
    assert r.success is True
    assert r.output["status"] == "completed"
    assert r.output["job"]["progress"] == 100


def test_job_adapter_not_found():
    async def get_job(job_id, user_id=None):
        return None

    r = run(JobAdapter(get_job_fn=get_job).execute("jobs.get_status", {"job_id": "ghost"}))
    assert r.success is False
    assert r.error_code == ErrorCode.NOT_FOUND
    assert r.retryable is False


# --------------------------------------------------------------------------- #
# Memory adapter
# --------------------------------------------------------------------------- #

def test_memory_adapter_maps_thread_summary():
    async def fetch(user_id, thread_id):
        return {"summary": "we discussed pricing", "last_summarized_seq": 4}

    r = run(MemoryAdapter(thread_summary_fn=fetch).execute(
        "memory.get_thread_summary", {"user_id": "u", "thread_id": "t1"}))
    assert r.success is True
    assert r.output["summary"] == "we discussed pricing"


def test_memory_adapter_empty_summary_is_partial():
    async def fetch(user_id, thread_id):
        return None

    r = run(MemoryAdapter(thread_summary_fn=fetch).execute(
        "memory.get_thread_summary", {"user_id": "u", "thread_id": "t1"}))
    assert r.success is True
    assert r.output["summary"] == ""
    assert r.partial is True


def test_memory_adapter_maps_preferences():
    async def fetch(user_id, limit):
        return [{"text": "concise answers"}, {"text": "metric units"}]

    r = run(MemoryAdapter(preferences_fn=fetch).execute(
        "memory.get_preferences", {"user_id": "u"}))
    assert r.success is True
    assert [p["text"] for p in r.output["preferences"]] == ["concise answers", "metric units"]


# --------------------------------------------------------------------------- #
# Error translation (retryable vs non-retryable)
# --------------------------------------------------------------------------- #

def test_timeout_becomes_retryable_result():
    async def flaky(job_id, user_id=None):
        raise TimeoutError("upstream slow")

    r = run(JobAdapter(get_job_fn=flaky).execute("jobs.get_status", {"job_id": "j1"}))
    assert r.success is False
    assert r.retryable is True
    assert r.error_code == ErrorCode.UPSTREAM_TIMEOUT
    assert r.metadata["exception"] == "TimeoutError"


def test_value_error_becomes_non_retryable_result():
    async def bad(user_id, limit):
        raise ValueError("bad input")

    r = run(MemoryAdapter(preferences_fn=bad).execute("memory.get_preferences", {"user_id": "u"}))
    assert r.success is False
    assert r.retryable is False
    assert r.error_code == ErrorCode.INVALID_ARGS


def test_connection_error_is_retryable():
    async def down(query, user_id, top_k, document_id=None, page=None):
        raise ConnectionError("qdrant unreachable")

    r = run(DocumentAdapter(retrieve_fn=down).execute(
        "documents.retrieve_chunks", {"query": "q", "user_id": "u"}))
    assert r.retryable is True
    assert r.error_code == ErrorCode.UPSTREAM_UNAVAILABLE


def test_classify_exception_defaults_non_retryable():
    assert classify_exception(RuntimeError("x")) == (ErrorCode.UPSTREAM_ERROR, False)


def test_unknown_capability_is_non_retryable_failure():
    r = run(JobAdapter(get_job_fn=None).execute("jobs.nope", {}))
    assert r.success is False
    assert r.error_code == ErrorCode.UNKNOWN_CAPABILITY
    assert r.retryable is False


def test_capabilities_listed():
    assert DocumentAdapter().capabilities() == [
        "documents.get_summary",
        "documents.retrieve_chunks",
    ]
    assert JobAdapter().capabilities() == ["jobs.get_status"]
    assert set(MemoryAdapter().capabilities()) == {
        "memory.get_thread_summary",
        "memory.get_preferences",
    }


# --------------------------------------------------------------------------- #
# No config / no V1.5 imports at module import time
# --------------------------------------------------------------------------- #

def _module_level_import_targets(module):
    """All module-level (column-0) import targets in a module's source."""
    tree = ast.parse(inspect.getsource(module))
    targets: list[str] = []
    for node in tree.body:  # only top-level statements
        if isinstance(node, ast.Import):
            targets += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            targets.append(node.module or "")
    return targets


def test_no_config_or_v15_imports_at_module_level():
    for module in (
        result_module,
        base_module,
        document_module,
        job_module,
        memory_module,
    ):
        targets = _module_level_import_targets(module)
        assert not any(t.startswith("app.services") for t in targets), (module.__name__, targets)
        assert not any(t.startswith("app.config") for t in targets), (module.__name__, targets)


def test_v15_service_imports_are_lazy_inside_methods():
    # The real service imports must appear in the source (inside resolvers),
    # proving they exist but are deferred rather than top-level.
    for module in (job_module, memory_module):
        src = inspect.getsource(module)
        assert "from app.services." in src  # present...
        assert src.index("def ") < src.index("from app.services.")  # ...but after a def
