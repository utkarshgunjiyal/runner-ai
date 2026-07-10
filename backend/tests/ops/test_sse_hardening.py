"""Phase 42A tests — SSE heartbeat + disconnect cancellation (config-free)."""

import asyncio

from app.sse import HEARTBEAT_FRAME, sse_event_source


def collect(agen):
    async def _run():
        return [frame async for frame in agen]
    return asyncio.run(_run())


def serialize(event) -> str:
    return f"event: {event}\ndata: {{}}\n\n"


async def _never_disconnected() -> bool:
    return False


# --------------------------------------------------------------------------- #
# Heartbeats
# --------------------------------------------------------------------------- #

def test_heartbeat_emitted_while_idle():
    async def slow_events():
        await asyncio.sleep(0.03)  # idle gap > heartbeat interval
        yield "answer_chunk"

    frames = collect(sse_event_source(
        slow_events(), serialize=serialize,
        is_disconnected=_never_disconnected, heartbeat_seconds=0.01,
    ))
    assert HEARTBEAT_FRAME in frames                      # kept the stream alive
    assert any("answer_chunk" in f for f in frames)       # real event still delivered


def test_no_heartbeat_when_events_flow_fast():
    async def fast_events():
        yield "a"
        yield "b"

    frames = collect(sse_event_source(
        fast_events(), serialize=serialize,
        is_disconnected=_never_disconnected, heartbeat_seconds=5.0,
    ))
    assert HEARTBEAT_FRAME not in frames
    assert [f for f in frames if "event:" in f]


# --------------------------------------------------------------------------- #
# Disconnect cancellation
# --------------------------------------------------------------------------- #

def test_disconnect_stops_stream_and_cancels_producer():
    closed = {"value": False}

    async def long_events():
        try:
            for i in range(1000):
                yield f"e{i}"
                await asyncio.sleep(0.001)
        finally:
            closed["value"] = True  # generator closed on cancellation

    disconnect_after = {"n": 0}

    async def is_disconnected() -> bool:
        disconnect_after["n"] += 1
        return disconnect_after["n"] > 2  # disconnect after a couple of iterations

    frames = collect(sse_event_source(
        long_events(), serialize=serialize,
        is_disconnected=is_disconnected, heartbeat_seconds=5.0,
    ))
    # stream stopped early (far fewer than 1000 frames) and emitted no terminal
    assert len(frames) < 1000
    assert closed["value"] is True  # producer/generator was cancelled + cleaned up


def test_no_terminal_frame_after_disconnect():
    async def events():
        yield "runtime_started"
        yield "answer_chunk"
        yield "runtime_completed"  # a "terminal" — must not be reached after disconnect

    async def is_disconnected() -> bool:
        return True  # already disconnected

    frames = collect(sse_event_source(
        events(), serialize=serialize,
        is_disconnected=is_disconnected, heartbeat_seconds=5.0,
    ))
    assert frames == []  # nothing sent to a disconnected client


def test_runtime_streamer_cancels_background_task_on_early_close():
    # The RuntimeStreamer must cancel its background run when the consumer stops
    # early — no orphaned orchestrator/provider work.
    from app.agent.runtime.events import RuntimeEventType as E
    from app.agent.runtime.streaming import RuntimeStreamer

    started = {"cancelled": False}

    class SlowOrchestrator:
        async def run(self, *a, stream_sink=None, **kw):
            if stream_sink is not None:
                await stream_sink(E.CONTEXT_STARTED, None, {})  # emit one live event
            try:
                await asyncio.sleep(10)  # then a long-running run
            except asyncio.CancelledError:
                started["cancelled"] = True
                raise

    async def go():
        streamer = RuntimeStreamer(SlowOrchestrator())
        agen = streamer.run_stream("x", user_id="u")
        first = await agen.__anext__()      # runtime_started (before the bg task)
        second = await agen.__anext__()     # context_started (bg task now running)
        await agen.aclose()                 # consumer stops early → cancel bg run
        return first, second

    first, second = asyncio.run(go())
    assert first.type.value == "runtime_started"
    assert second.type.value == "context_started"
    assert started["cancelled"] is True     # background run was cancelled cleanly
