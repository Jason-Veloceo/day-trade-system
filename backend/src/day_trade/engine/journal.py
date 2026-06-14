"""Engine event journal.

Every interesting thing the engine does (bar received, indicator updated,
signal emitted, risk decision, order submitted, fill received, ...) flows
through here. Each event is:

  1. persisted to the `engine_events` table (audit trail), AND
  2. published to the in-process broker on a per-topic channel so the
     /engine page can stream it live over WebSocket.

The journal is engine-instance-scoped (one Journal per EngineRun).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from decimal import Decimal
from typing import Any

from day_trade.db.models import EngineEvent
from day_trade.db.session import session_scope
from day_trade.ws.broker import MessageBroker
from day_trade.ws import topics as T

logger = logging.getLogger(__name__)


# Map (engine event_type) -> (broker topic). Events not in the map are
# persisted but not published; persisted-only events are rare.
_TOPIC_MAP: dict[str, str] = {
    "bar": T.ENGINE_BAR,
    "indicator": T.ENGINE_INDICATOR,
    "signal": T.ENGINE_SIGNAL,
    "ready_for_approval": T.ENGINE_APPROVAL_NEEDED,
    "approval_granted": T.ENGINE_APPROVAL_NEEDED,
    "approval_rejected": T.ENGINE_APPROVAL_NEEDED,
    "decision": T.ENGINE_SIGNAL,
    "risk_block": T.ENGINE_ERROR,
    "order_submit": T.ENGINE_FILL,
    "order_status": T.ENGINE_FILL,
    "fill": T.ENGINE_FILL,
    "slippage": T.ENGINE_FILL,
    "position_open": T.ENGINE_POSITION,
    "position_close": T.ENGINE_POSITION,
    "error": T.ENGINE_ERROR,
    "engine_start": T.ENGINE_RUN_STATE,
    "engine_stop": T.ENGINE_RUN_STATE,
    "ibkr_connected": T.ENGINE_RUN_STATE,
    "ibkr_disconnected": T.ENGINE_RUN_STATE,
    # v1.1
    "depth_update": T.ENGINE_DEPTH,
    "tape_print": T.ENGINE_TAPE,
    "exit_trigger": T.ENGINE_SIGNAL,
    "feature_snapshot": T.ENGINE_FEATURES,
}


def _jsonable(obj: Any) -> Any:
    """Convert a payload into something JSON-serialisable."""
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, dt.datetime):
        return obj.isoformat()
    if isinstance(obj, dt.date):
        return obj.isoformat()
    return obj


class Journal:
    def __init__(self, run_id: int, broker: MessageBroker) -> None:
        self.run_id = run_id
        self._broker = broker

    async def record(self, event_type: str, payload: dict[str, Any]) -> None:
        """Persist + publish."""
        clean = _jsonable(payload) if payload else {}

        # Roundtrip through JSON to catch any non-serialisable surprises early
        # (cheaper than failing at INSERT time).
        try:
            json.dumps(clean)
        except (TypeError, ValueError) as e:
            logger.error("event payload not JSON-serialisable: %s", e)
            clean = {"_error": "payload_not_serialisable", "_repr": repr(payload)}

        try:
            async with session_scope() as s:
                s.add(EngineEvent(run_id=self.run_id, event_type=event_type, payload=clean))
        except Exception:
            # We don't want a DB hiccup to crash the engine - log and keep going.
            logger.exception("failed to persist engine_event type=%s", event_type)

        topic = _TOPIC_MAP.get(event_type)
        if topic is not None:
            try:
                await self._broker.publish(
                    topic,
                    {
                        "run_id": self.run_id,
                        "event_type": event_type,
                        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "payload": clean,
                    },
                )
            except Exception:
                logger.exception("failed to publish engine_event type=%s", event_type)
