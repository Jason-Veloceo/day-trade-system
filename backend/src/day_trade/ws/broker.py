"""In-process pub/sub between ingestion and the API/WS layer.

Simple typed broadcaster: subscribers get an async queue; publishers fire-and-forget.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BrokerMessage:
    topic: str
    payload: dict[str, Any]


class MessageBroker:
    """Topic-aware in-process broker. Each subscriber holds an asyncio.Queue.

    Slow subscribers can block other subscribers if they don't drain. For our local
    single-user use, that's an acceptable trade-off.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[BrokerMessage]]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, topic: str) -> asyncio.Queue[BrokerMessage]:
        queue: asyncio.Queue[BrokerMessage] = asyncio.Queue(maxsize=1024)
        async with self._lock:
            self._subscribers.setdefault(topic, set()).add(queue)
        return queue

    async def unsubscribe(self, topic: str, queue: asyncio.Queue[BrokerMessage]) -> None:
        async with self._lock:
            self._subscribers.get(topic, set()).discard(queue)

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        msg = BrokerMessage(topic=topic, payload=payload)
        async with self._lock:
            subs = list(self._subscribers.get(topic, set()))
        for q in subs:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                logger.warning("Dropped message on full queue for topic=%s", topic)

    async def stream(self, topic: str) -> AsyncIterator[BrokerMessage]:
        queue = await self.subscribe(topic)
        try:
            while True:
                msg = await queue.get()
                yield msg
        finally:
            await self.unsubscribe(topic, queue)


_BROKER: MessageBroker | None = None


def get_broker() -> MessageBroker:
    global _BROKER
    if _BROKER is None:
        _BROKER = MessageBroker()
    return _BROKER
