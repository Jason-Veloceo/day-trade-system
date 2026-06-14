"""Replay a captured DTD `/alert?widget=...` JSON file through the live pipeline.

Two modes:
  - fast: process every event back-to-back, then return. Used for tests + initial
    historical backfill.
  - timed: pace events at their original cadence (configurable speed multiplier).
    Used to dev the dashboard with the market closed - looks indistinguishable
    from live ingestion.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from pathlib import Path

from day_trade.db.repositories.pipeline import ingest_event
from day_trade.db.session import session_scope
from day_trade.ingest.dtd.parser import parse_alert_response
from day_trade.normalize.scanner_events import RawDtdEvent
from day_trade.ws.broker import get_broker
from day_trade.ws.topics import CANDIDATE_UPDATE, SCANNER_EVENT

logger = logging.getLogger(__name__)


def load_events(path: Path) -> list[RawDtdEvent]:
    payload = path.read_bytes()
    events = parse_alert_response(payload)
    events.sort(key=lambda e: e.ts)
    return events


async def _process_one(event: RawDtdEvent) -> None:
    async for session in session_scope():
        result = await ingest_event(session, event)
        broker = get_broker()
        await broker.publish(
            SCANNER_EVENT,
            {
                "symbol": event.symbol,
                "widget": event.widget,
                "strategy": event.strategy,
                "ts": event.ts.isoformat(),
            },
        )
        await broker.publish(
            CANDIDATE_UPDATE,
            {
                "candidate_id": result.candidate_id,
                "symbol": result.snapshot.symbol,
                "status": "passed" if result.decision.passed else "failed_filter",
                "is_new": result.is_new_candidate,
                "failed_rules": result.decision.failed_rules,
                "last_alert_at": result.snapshot.last_alert_at.isoformat(),
            },
        )


async def replay_fast(path: Path, *, limit: int | None = None) -> int:
    """Process every event as fast as possible. Returns the count processed."""
    events = load_events(path)
    if limit is not None:
        events = events[:limit]
    for ev in events:
        await _process_one(ev)
    return len(events)


async def replay_timed(path: Path, *, speed: float = 60.0, limit: int | None = None) -> int:
    """Pace events at their original cadence divided by `speed`.

    speed=60 means 1 minute of real time becomes 1 second in replay; speed=1.0 is
    real-time replay.
    """
    events = load_events(path)
    if limit is not None:
        events = events[:limit]
    if not events:
        return 0

    base_ts = events[0].ts
    base_wall = dt.datetime.now(tz=dt.UTC)
    for ev in events:
        offset = (ev.ts - base_ts).total_seconds() / speed
        target_wall = base_wall + dt.timedelta(seconds=offset)
        now = dt.datetime.now(tz=dt.UTC)
        sleep_for = (target_wall - now).total_seconds()
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)
        await _process_one(ev)
    return len(events)
