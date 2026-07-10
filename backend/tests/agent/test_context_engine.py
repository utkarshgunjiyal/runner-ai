"""Phase 10B tests — context providers + Context Engine."""

import asyncio

import pytest

from app.agent.context.engine import (
    ContextEngine,
    ContextEngineError,
    default_context_engine,
)
from app.agent.context.providers import (
    ContextProvider,
    ContextRequest,
    RecentMessagesProvider,
    ThreadSummaryProvider,
    UserPreferencesProvider,
)
from app.agent.runtime.context import RunContext, WorkingContextItem


def run(coro):
    return asyncio.run(coro)


def item(source, content, **md):
    return WorkingContextItem(source=source, content=content, metadata=md)


class FakeProvider(ContextProvider):
    def __init__(self, name, items=None, error=None, required=False):
        self.name = name
        self._items = items or []
        self._error = error
        self.required = required
        self.calls = 0

    async def provide(self, request):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return list(self._items)


# --------------------------------------------------------------------------- #
# Context Engine
# --------------------------------------------------------------------------- #

def test_engine_creates_run_context():
    engine = ContextEngine([FakeProvider("p", items=[item("p", "a")])])
    rc = run(engine.build("what is X?", "dev_user", "t1"))
    assert isinstance(rc, RunContext)
    assert rc.run_id
    assert rc.user_request == "what is X?"
    assert rc.user_id == "dev_user"
    assert rc.thread_id == "t1"


def test_providers_are_called():
    p1, p2 = FakeProvider("p1"), FakeProvider("p2")
    run(ContextEngine([p1, p2]).build("q", "u"))
    assert p1.calls == 1
    assert p2.calls == 1


def test_working_context_items_added_in_order():
    p1 = FakeProvider("p1", items=[item("recent_message", "hi", role="user")])
    p2 = FakeProvider("p2", items=[item("user_preference", "dark mode")])
    rc = run(ContextEngine([p1, p2]).build("q", "u"))
    assert len(rc.working_context) == 2
    assert [i.source for i in rc.working_context] == ["recent_message", "user_preference"]


def test_optional_provider_failure_does_not_crash():
    bad = FakeProvider("bad", error=RuntimeError("boom"), required=False)
    good = FakeProvider("good", items=[item("good", "ok")])
    rc = run(ContextEngine([bad, good]).build("q", "u"))
    assert len(rc.working_context) == 1
    report = rc.metadata["context_providers"]
    assert report["bad"]["ok"] is False
    assert "boom" in report["bad"]["error"]
    assert report["good"]["ok"] is True


def test_required_provider_failure_is_surfaced():
    bad = FakeProvider("bad", error=RuntimeError("boom"), required=True)
    with pytest.raises(ContextEngineError):
        run(ContextEngine([bad]).build("q", "u"))


def test_context_item_source_metadata_preserved():
    p = FakeProvider("p", items=[item("recent_message", "hi", role="user", seq=3)])
    rc = run(ContextEngine([p]).build("q", "u"))
    wc = rc.working_context[0]
    assert wc.source == "recent_message"
    assert wc.metadata == {"role": "user", "seq": 3}


def test_empty_providers_produce_valid_run_context():
    rc = run(ContextEngine([]).build("q", "u"))
    assert isinstance(rc, RunContext)
    assert rc.working_context == []

    empty = FakeProvider("empty", items=[])
    rc2 = run(ContextEngine([empty]).build("q", "u"))
    assert rc2.working_context == []
    assert rc2.metadata["context_providers"]["empty"]["count"] == 0


def test_working_context_immutable_after_creation():
    p = FakeProvider("p", items=[item("s", "c")])
    rc = run(ContextEngine([p]).build("q", "u"))
    rc.working_context.append(item("x", "y"))
    assert len(rc.working_context) == 1


# --------------------------------------------------------------------------- #
# Real providers (via injected fake fetch — no V1.5 import needed)
# --------------------------------------------------------------------------- #

def test_recent_messages_provider_maps_and_preserves_metadata():
    async def fake_fetch(user_id, thread_id, limit):
        return [
            {"content": "hello", "role": "user", "seq": 1},
            {"content": "hi", "role": "assistant", "seq": 2},
        ]

    prov = RecentMessagesProvider(fetch=fake_fetch, limit=5)
    items = run(prov.provide(ContextRequest(user_request="q", user_id="u", thread_id="t")))
    assert [i.content for i in items] == ["hello", "hi"]
    assert items[0].source == "recent_message"
    assert items[0].metadata == {"role": "user", "seq": 1}


def test_recent_messages_provider_no_thread_returns_empty():
    async def fake_fetch(user_id, thread_id, limit):  # pragma: no cover - must not run
        raise AssertionError("should not be called without a thread_id")

    prov = RecentMessagesProvider(fetch=fake_fetch)
    items = run(prov.provide(ContextRequest(user_request="q", user_id="u", thread_id=None)))
    assert items == []


def test_thread_summary_provider_maps_and_skips_empty():
    async def with_summary(user_id, thread_id):
        return {"summary": "we discussed X", "last_summarized_seq": 5}

    prov = ThreadSummaryProvider(fetch=with_summary)
    items = run(prov.provide(ContextRequest(user_request="q", user_id="u", thread_id="t")))
    assert len(items) == 1
    assert items[0].source == "thread_summary"
    assert items[0].content == "we discussed X"

    async def empty_summary(user_id, thread_id):
        return {"summary": ""}

    prov2 = ThreadSummaryProvider(fetch=empty_summary)
    assert run(prov2.provide(ContextRequest(user_request="q", user_id="u", thread_id="t"))) == []


def test_user_preferences_provider_maps():
    async def fake_prefs(user_id, limit):
        return [{"_id": "1", "text": "dark mode"}, {"_id": "2", "text": "metric units"}]

    prov = UserPreferencesProvider(fetch=fake_prefs)
    items = run(prov.provide(ContextRequest(user_request="q", user_id="u")))
    assert [i.content for i in items] == ["dark mode", "metric units"]
    assert items[0].source == "user_preference"
    assert items[0].metadata == {"preference_id": "1"}


def test_default_engine_wiring():
    engine = default_context_engine()
    assert isinstance(engine, ContextEngine)
    names = {p.name for p in engine._providers}
    assert names == {"recent_message", "thread_summary", "user_preference", "user_knowledge"}
