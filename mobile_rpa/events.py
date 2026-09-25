"""In-process pub/sub that feeds the dashboard's Server-Sent Events stream."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

QUEUE_LIMIT = 500


class Broker:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()

    @property
    def listeners(self) -> int:
        return len(self._subscribers)

    def publish(self, kind: str, data: Any) -> None:
        message = f"event: {kind}\ndata: {json.dumps(data, default=str)}\n\n"
        for queue in list(self._subscribers):
            if queue.qsize() >= QUEUE_LIMIT:
                continue  # a stalled browser tab must not grow memory without bound
            queue.put_nowait(message)

    async def stream(self, heartbeat: float = 15.0) -> AsyncIterator[str]:
        queue: asyncio.Queue[str] = asyncio.Queue()
        self._subscribers.add(queue)
        try:
            yield "retry: 2000\n\n"
            while True:
                try:
                    yield await asyncio.wait_for(queue.get(), heartbeat)
                except TimeoutError:
                    yield ": ping\n\n"
        finally:
            self._subscribers.discard(queue)
