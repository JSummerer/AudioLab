"""Live updates: `GET /api/events` streams job and system events (Server-Sent Events)."""

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import APP_NAME

SSE_PING_S = 15.0

router = APIRouter()


class Hub:
    """Fan-out of job/system events from worker threads to SSE clients."""

    def __init__(self):
        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients: list[asyncio.Queue] = []
        self.closed = False

    def publish(self, event: str, payload: dict[str, Any]) -> None:
        if self.loop is None:
            return
        for q in list(self.clients):
            try:
                self.loop.call_soon_threadsafe(self._put, q, (event, payload))
            except RuntimeError:
                pass  # loop closing

    @staticmethod
    def _put(q: asyncio.Queue, item) -> None:
        if q.full():
            try:
                q.get_nowait()  # slow client: drop the oldest update rather than block
            except asyncio.QueueEmpty:
                pass
        q.put_nowait(item)

    def close(self) -> None:
        """End every stream and refuse new ones (the app is stopping): open streams never end
        on their own and would hold the server's shutdown for its whole graceful timeout.
        Safe from any thread and more than once."""
        if self.loop is None:
            self.closed = True
            return
        try:
            self.loop.call_soon_threadsafe(self._close)
        except RuntimeError:
            pass  # loop closed: no streams left

    def _close(self) -> None:  # on the event loop, like the streams themselves
        self.closed = True
        for q in self.clients:
            self._put(q, None)


@router.get('/api/events')
async def events(request: Request):
    hub: Hub = request.app.state.audiolab.hub
    if hub.closed:
        return JSONResponse({'detail': f'{APP_NAME} is shutting down.'}, status_code=503)
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    hub.clients.append(q)

    async def stream():
        try:
            yield 'retry: 3000\n\n'
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=SSE_PING_S)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        break
                    yield ': ping\n\n'
                    continue
                if item is None:  # Hub.close()
                    break
                event, payload = item
                yield f'event: {event}\ndata: {json.dumps(payload, default=str)}\n\n'
        except asyncio.CancelledError:
            pass
        finally:
            if q in hub.clients:
                hub.clients.remove(q)

    return StreamingResponse(
        stream(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )
