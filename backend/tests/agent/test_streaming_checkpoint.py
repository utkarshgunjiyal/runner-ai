"""Phase 41B tests — RuntimeStreamer optional checkpointer.

Config-free. A streamed WAITING_* run persists a checkpoint (via the injected
checkpointer) and surfaces the id in the terminal ``runtime_completed`` event, so
a streamed human-in-the-loop run is resumable. Default (no checkpointer) is
unchanged; non-waiting outcomes are never checkpointed; a persistence failure
never breaks the stream.
"""

import asyncio

from app.agent.runtime.events import RuntimeEventType as E
from app.agent.runtime.outcome import RuntimeOutcome
from app.agent.runtime.streaming import RuntimeStreamer


def collect(agen):
    async def _run():
        return [e async for e in agen]
    return asyncio.run(_run())


class _Result:
    def __init__(self, outcome):
        self.runtime_outcome = outcome
        self.run_id = "run-1"
        self.pending_action = "ask_user_for_clarification"
        self.pending_reason = "need more info"
        self.metadata = {}


class StubOrchestrator:
    """Returns a fixed terminal result; emits no mid-stream events."""

    def __init__(self, outcome):
        self._outcome = outcome

    async def run(self, *a, stream_sink=None, **kw):
        return _Result(self._outcome)


class RecordingCheckpointer:
    def __init__(self, cid="ckpt-1", *, boom=False):
        self._cid = cid
        self._boom = boom
        self.calls = []

    async def __call__(self, result):
        self.calls.append(result)
        if self._boom:
            raise RuntimeError("store unavailable")
        return self._cid


def terminal(events):
    return events[-1]


def test_waiting_outcome_persists_and_surfaces_checkpoint_id():
    ckpt = RecordingCheckpointer("ckpt-42")
    streamer = RuntimeStreamer(StubOrchestrator(RuntimeOutcome.WAITING_FOR_USER), checkpointer=ckpt)
    events = collect(streamer.run_stream("do a thing", user_id="u"))
    t = terminal(events)
    assert t.type == E.RUNTIME_COMPLETED
    assert t.data["runtime_outcome"] == "waiting_for_user"
    assert t.data["checkpoint_id"] == "ckpt-42"
    assert len(ckpt.calls) == 1  # checkpointed exactly once


def test_all_waiting_outcomes_are_checkpointed():
    for outcome in (
        RuntimeOutcome.WAITING_FOR_USER,
        RuntimeOutcome.WAITING_FOR_APPROVAL,
        RuntimeOutcome.WAITING_FOR_CONTEXT,
        RuntimeOutcome.WAITING_FOR_REPLAN,
    ):
        ckpt = RecordingCheckpointer()
        events = collect(RuntimeStreamer(StubOrchestrator(outcome), checkpointer=ckpt)
                         .run_stream("x", user_id="u"))
        assert terminal(events).data["checkpoint_id"] == "ckpt-1"
        assert len(ckpt.calls) == 1


def test_completed_outcome_is_not_checkpointed():
    ckpt = RecordingCheckpointer()
    events = collect(RuntimeStreamer(StubOrchestrator(RuntimeOutcome.COMPLETED), checkpointer=ckpt)
                     .run_stream("x", user_id="u"))
    t = terminal(events)
    assert t.type == E.RUNTIME_COMPLETED
    assert t.data["checkpoint_id"] is None
    assert ckpt.calls == []  # not a waiting outcome → never persisted


def test_no_checkpointer_leaves_id_null():
    events = collect(RuntimeStreamer(StubOrchestrator(RuntimeOutcome.WAITING_FOR_USER))
                     .run_stream("x", user_id="u"))
    assert terminal(events).data["checkpoint_id"] is None


def test_checkpoint_failure_does_not_break_stream():
    ckpt = RecordingCheckpointer(boom=True)
    events = collect(RuntimeStreamer(StubOrchestrator(RuntimeOutcome.WAITING_FOR_APPROVAL), checkpointer=ckpt)
                     .run_stream("x", user_id="u"))
    t = terminal(events)
    # best-effort: the stream still completes, just without a resumable id
    assert t.type == E.RUNTIME_COMPLETED
    assert t.data["checkpoint_id"] is None
