"""Unit tests for the L2 depth watchdog.

The watchdog auto-stops any engine that asked for `enable_depth=True`
but never received a bid/ask within `depth_watchdog_seconds`. This
covers the IBKR-only-permits-3-concurrent-reqMktDepth failure mode
where a 4th engine's subscription is silently rejected.

We test the coroutine directly against a lightweight harness rather
than spinning up a full TradingEngine — the watchdog only touches
`market_state.depth`, `journal.record`, `_stop_event`, `spec.display`,
and calls `_stop_from_error`.
"""
from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import pytest

from day_trade.engine.engine import TradingEngine
from day_trade.engine.orderbook import DepthLevel, MarketState


@dataclass
class _FakeJournal:
    records: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def record(self, kind: str, payload: dict[str, Any]) -> None:
        self.records.append((kind, payload))


@dataclass
class _FakeSpec:
    display: str = "TEST"


class _Harness:
    """Minimal duck-typed stand-in for TradingEngine, containing only
    what `_depth_watchdog_run` and `_stop_from_error` reach for."""

    def __init__(self, *, market_state: MarketState) -> None:
        self.market_state = market_state
        self.journal = _FakeJournal()
        self.spec = _FakeSpec()
        self._stop_event = asyncio.Event()
        self.stop_calls: list[str] = []

    async def stop(self, reason: str = "user_stop") -> None:
        self._stop_event.set()
        self.stop_calls.append(reason)

    async def _stop_from_error(self, reason: str) -> None:
        await self.stop(reason=reason)


def _bind(method: Any, obj: _Harness) -> Any:
    """Bind an unbound TradingEngine coroutine to a harness instance."""
    return method.__get__(obj, type(obj))


@pytest.mark.asyncio
async def test_watchdog_stops_engine_when_depth_never_populates() -> None:
    """The main failure case: enable_depth was True, 15s (here 50ms)
    elapsed, and no bid/ask ever appeared. Watchdog should journal an
    `error` event with kind=`no_l2_depth_after_arm` and stop."""
    state = MarketState()  # empty book
    harness = _Harness(market_state=state)

    run = _bind(TradingEngine._depth_watchdog_run, harness)
    await run(0.05)

    assert harness.stop_calls == ["no_l2_depth_after_arm"]
    error_records = [r for r in harness.journal.records if r[0] == "error"]
    assert len(error_records) == 1
    _, payload = error_records[0]
    assert payload["kind"] == "no_l2_depth_after_arm"
    assert payload["timeout_seconds"] == pytest.approx(0.05)
    assert payload["best_bid"] is None
    assert payload["best_ask"] is None


@pytest.mark.asyncio
async def test_watchdog_no_op_when_book_populated_before_timeout() -> None:
    """Depth arrived in time. Watchdog should return silently: no
    journal event, no stop() call, engine keeps running."""
    state = MarketState()
    state.depth.bids = [DepthLevel(side="bid", price=5.50, size=1000)]
    state.depth.asks = [DepthLevel(side="ask", price=5.52, size=1200)]
    state.depth.updated_at = dt.datetime.now(dt.timezone.utc)

    harness = _Harness(market_state=state)
    run = _bind(TradingEngine._depth_watchdog_run, harness)
    await run(0.05)

    assert harness.stop_calls == []
    assert harness.journal.records == []


@pytest.mark.asyncio
async def test_watchdog_no_op_when_engine_already_stopped() -> None:
    """If the engine is stopped before the timeout elapses (normal
    user shutdown or a different error path), the watchdog must not
    journal or call stop again."""
    state = MarketState()  # empty book, would normally trigger drop
    harness = _Harness(market_state=state)
    harness._stop_event.set()  # engine already gone

    run = _bind(TradingEngine._depth_watchdog_run, harness)
    await run(0.05)

    assert harness.stop_calls == []
    assert harness.journal.records == []


@pytest.mark.asyncio
async def test_watchdog_cancel_returns_cleanly_without_journalling() -> None:
    """If `stop()` cancels the watchdog task mid-sleep, the coroutine
    should return without journalling or trying to stop the engine."""
    state = MarketState()  # empty book — would drop if allowed to run
    harness = _Harness(market_state=state)
    run = _bind(TradingEngine._depth_watchdog_run, harness)

    task = asyncio.create_task(run(60.0))  # long sleep
    await asyncio.sleep(0.01)  # let it enter sleep
    task.cancel()
    # Cancellation is caught inside the coroutine; the task should
    # complete normally (result = None), NOT propagate CancelledError.
    await task

    assert harness.stop_calls == []
    assert harness.journal.records == []


@pytest.mark.asyncio
async def test_watchdog_stops_when_only_ask_present() -> None:
    """Half-populated books also count as "no depth". Both sides must
    be present for microstructure gates to evaluate spread/imbalance,
    so a one-sided book still means we can't trade."""
    state = MarketState()
    state.depth.asks = [DepthLevel(side="ask", price=5.52, size=1200)]
    # bids empty
    harness = _Harness(market_state=state)
    run = _bind(TradingEngine._depth_watchdog_run, harness)
    await run(0.05)

    assert harness.stop_calls == ["no_l2_depth_after_arm"]


@pytest.mark.asyncio
async def test_watchdog_stops_when_only_bid_present() -> None:
    state = MarketState()
    state.depth.bids = [DepthLevel(side="bid", price=5.50, size=1000)]
    # asks empty
    harness = _Harness(market_state=state)
    run = _bind(TradingEngine._depth_watchdog_run, harness)
    await run(0.05)

    assert harness.stop_calls == ["no_l2_depth_after_arm"]
