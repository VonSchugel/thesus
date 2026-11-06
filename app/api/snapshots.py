from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from hashlib import blake2s
from time import monotonic

from fastapi import Request
from fastapi.responses import StreamingResponse


def stream_snapshots(request: Request, event: str, load: Callable[[], Awaitable[str]]) -> StreamingResponse:
    async def snapshots():
        previous = request.headers.get('last-event-id')
        heartbeat = monotonic()
        yield 'retry: 1000\n\n'
        while not await request.is_disconnected():
            payload = await load()
            current = blake2s(payload.encode('utf-8'), digest_size=12).hexdigest()
            if current != previous:
                previous = current
                heartbeat = monotonic()
                yield f'id: {current}\nevent: {event}\ndata: {payload}\n\n'
            elif monotonic() - heartbeat >= 12:
                heartbeat = monotonic()
                yield ': keepalive\n\n'
            await asyncio.sleep(0.4)

    return StreamingResponse(snapshots(), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})
