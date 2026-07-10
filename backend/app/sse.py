"""SSE transport helpers (Phase 42A) — config-free, transport-only.

Wraps an async iterator of runtime events with:
- heartbeat comments during idle periods (so proxies do not drop the stream),
- cooperative cancellation on client disconnect (the producer task is cancelled,
  which closes the underlying runtime stream — no orphaned work), and
- no terminal event after a disconnect (the client is gone).

This contains no runtime/planning logic; it only shapes bytes on the wire.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

_SENTINEL = object()

# An SSE comment line — ignored by every SSE parser, so a safe keep-alive.
HEARTBEAT_FRAME = ": heartbeat\n\n"


async def sse_event_source(
    events: AsyncIterator,
    *,
    serialize: Callable[[object], str],
    is_disconnected: Callable[[], Awaitable[bool]],
    heartbeat_seconds: float,
) -> AsyncIterator[str]:
    """Yield serialized SSE frames, emitting heartbeats and stopping on disconnect.

    On disconnect (or generator close) the producer task is cancelled — which
    propagates cancellation into ``events`` (e.g. RuntimeStreamer), cancelling the
    background runtime/provider stream. No terminal frame is emitted after a
    disconnect.
    """
    queue: asyncio.Queue = asyncio.Queue()

    async def _produce() -> None:
        try:
            async for event in events:
                await queue.put(event)
        finally:
            await queue.put(_SENTINEL)

    task = asyncio.create_task(_produce())
    try:
        while True:
            if await is_disconnected():
                break
            try:
                if heartbeat_seconds and heartbeat_seconds > 0:
                    item = await asyncio.wait_for(queue.get(), timeout=heartbeat_seconds)
                else:
                    item = await queue.get()
            except asyncio.TimeoutError:
                yield HEARTBEAT_FRAME  # idle keep-alive
                continue
            if item is _SENTINEL:
                break
            yield serialize(item)
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
