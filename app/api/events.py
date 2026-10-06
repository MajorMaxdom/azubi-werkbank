"""Server-Sent Events: tell open pages that a catalog changed.

Pages show a banner instead of reloading, so unsaved input is never lost.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

router = APIRouter()

KEEPALIVE_SECONDS = 20.0
_CLOSE = object()


class Broadcaster:
    """Fan-out of change notifications to all connected SSE clients.

    ``publish`` may be called from any thread (the registry reloads files in a
    worker thread); delivery happens on the event loop.
    """

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, workbook_ids: set[str]) -> None:
        payload = json.dumps({"workbooks": sorted(workbook_ids)})
        self._dispatch(payload)

    def close(self) -> None:
        """Ask every open stream to end. Safe to call from a signal handler."""
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._put_all, _CLOSE)

    def _dispatch(self, item: object) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._put_all(item)
        else:
            loop.call_soon_threadsafe(self._put_all, item)

    def _put_all(self, item: object) -> None:
        for queue in list(self._subscribers):
            if queue.full():
                continue  # slow client: it only needs to know that *something* changed
            queue.put_nowait(item)


async def event_stream(
    broadcaster: Broadcaster, queue: asyncio.Queue, keepalive: float = KEEPALIVE_SECONDS
) -> AsyncIterator[str]:
    try:
        yield "retry: 5000\n\n"
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=keepalive)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            if item is _CLOSE:
                return
            yield f"event: catalog\ndata: {item}\n\n"
    finally:
        broadcaster.unsubscribe(queue)


@router.get("/events")
async def events(request: Request) -> StreamingResponse:
    broadcaster: Broadcaster = request.app.state.broadcaster
    queue = broadcaster.subscribe()
    return StreamingResponse(
        event_stream(broadcaster, queue),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
