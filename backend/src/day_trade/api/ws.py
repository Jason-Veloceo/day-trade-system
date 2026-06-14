"""WebSocket endpoint that streams candidate updates, scanner events and rule changes."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from day_trade.ws.broker import get_broker
from day_trade.ws import topics as T

logger = logging.getLogger(__name__)
router = APIRouter(tags=["ws"])


@router.websocket("/ws")
async def stream(socket: WebSocket) -> None:
    """Single multiplexed stream. Each message is `{topic, payload}` JSON."""
    await socket.accept()
    broker = get_broker()
    topics = [
        T.CANDIDATE_UPDATE,
        T.SCANNER_EVENT,
        T.RULE_SET_CHANGED,
        T.ENGINE_BAR,
        T.ENGINE_INDICATOR,
        T.ENGINE_SIGNAL,
        T.ENGINE_APPROVAL_NEEDED,
        T.ENGINE_POSITION,
        T.ENGINE_FILL,
        T.ENGINE_PNL,
        T.ENGINE_ERROR,
        T.ENGINE_RUN_STATE,
    ]

    queues = [await broker.subscribe(t) for t in topics]

    async def relay(topic: str, q: asyncio.Queue) -> None:
        try:
            while True:
                msg = await q.get()
                await socket.send_json({"topic": msg.topic, "payload": msg.payload})
        except WebSocketDisconnect:
            return
        except Exception:
            logger.exception("ws relay error topic=%s", topic)
            return

    tasks = [asyncio.create_task(relay(t, q)) for t, q in zip(topics, queues, strict=True)]

    try:
        while True:
            await socket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        for t in tasks:
            t.cancel()
        for t, q in zip(topics, queues, strict=True):
            await broker.unsubscribe(t, q)
