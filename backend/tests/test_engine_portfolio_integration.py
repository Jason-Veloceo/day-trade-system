"""Integration tests for the TradingEngine ↔ PortfolioRiskGate wiring.

Validates the four integration points wired in Day 2:
  1. `_handle_enter` acquires the mutex BEFORE order submission (after
     the approval gate) and journals `entry_blocked_by_portfolio_mutex`
     when denied.
  2. `_handle_enter` releases the mutex when the order submit fails
     (executor returns None).
  3. `_handle_exit_decision` releases the mutex on full position close.
  4. `_handle_exit_signal` releases the mutex on full position close.
  5. `stop()` releases the mutex when called while we hold it.

Uses real `PortfolioRiskGate` + `RiskGate` + `Strategy.mark_*` instances,
with the IBKR / DB / broker / executor dependencies stubbed out. We
drive `_handle_enter` and `_handle_exit_decision` directly so the test
doesn't depend on the full BarFeed plumbing.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import pytest

from day_trade.engine.engine import EngineConfig, TradingEngine
from day_trade.engine.exits import (
    ExitConfig,
    ExitDecision,
    ExitTriggerKind,
    ExitTriggerSet,
)
from day_trade.engine.portfolio_risk import PortfolioRiskCaps, PortfolioRiskGate
from day_trade.engine.risk import RiskCaps, RiskGate
from day_trade.engine.strategies.base import Bar, Signal, SignalKind

# ----- stubs ---------------------------------------------------------------


@dataclass
class _FakeJournal:
    records: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def record(self, kind: str, payload: dict[str, Any]) -> None:
        self.records.append((kind, payload))

    def kinds(self) -> list[str]:
        return [k for k, _ in self.records]

    def payloads_for(self, kind: str) -> list[dict[str, Any]]:
        return [p for k, p in self.records if k == kind]


class _FakeEvent:
    """Drop-in replacement for an ib_async event that supports `+=` to
    register subscribers. We don't actually fire from these in tests —
    the integration tests drive `_on_entry_fill` / `_on_entry_status`
    directly via the helpers below, which is the same path ib_async
    would take in production."""

    def __init__(self) -> None:
        self._subs: list[Any] = []

    def __iadd__(self, fn: Any) -> _FakeEvent:
        self._subs.append(fn)
        return self


@dataclass
class _FakeOrder:
    orderId: int = 99


@dataclass
class _FakeOrderStatus:
    status: str = "Submitted"
    filled: float = 0.0
    remaining: float = 0.0
    avgFillPrice: float = 0.0


class _FakeTrade:
    """Stand-in for ib_async.Trade. Has the `fillEvent` / `statusEvent`
    surfaces the engine subscribes to, plus mutable `orderStatus` so
    tests can simulate fills and cancellations."""

    def __init__(self) -> None:
        self.order = _FakeOrder()
        self.orderStatus = _FakeOrderStatus()
        self.fillEvent = _FakeEvent()
        self.statusEvent = _FakeEvent()


class _FakeExecutor:
    """Stub executor with configurable return value. By default returns
    a `_FakeTrade` so the engine treats the submit as successful and
    can wire its fillEvent / statusEvent subscribers. Set
    `return_none=True` to simulate a submit failure."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.return_none = False
        self.last_trade: _FakeTrade | None = None

    async def execute(self, **kwargs: Any) -> _FakeTrade | None:
        self.calls.append(kwargs)
        if self.return_none:
            return None
        trade = _FakeTrade()
        self.last_trade = trade
        return trade


async def _simulate_full_fill(
    engine: TradingEngine, *, price: float, qty: int
) -> None:
    """Drive the engine's pending-entry → in-position transition by hand
    in tests. In production this happens automatically when ib_async
    fires fillEvent + statusEvent on the IBKR Trade, but the test
    harness doesn't go through the broker, so we mutate the FakeTrade
    and call the engine's handlers ourselves.

    Mirrors the v1.2 "position open immediately after _handle_enter"
    semantics in tests that aren't specifically exercising the new
    fill-confirmation gate.
    """
    pe = engine._pending_entry
    assert pe is not None, "no pending entry to fill"
    trade = pe.trade
    trade.orderStatus.status = "Filled"
    trade.orderStatus.filled = float(qty)
    trade.orderStatus.avgFillPrice = float(price)
    await engine._on_entry_fill(trade)
    await engine._on_entry_status(trade)


async def _simulate_cancel_no_fill(engine: TradingEngine) -> None:
    """Drive the cancellation path: BUY came back Cancelled with 0 fills,
    so the engine rolls back to no-position state and releases the mutex.
    """
    pe = engine._pending_entry
    assert pe is not None, "no pending entry to cancel"
    trade = pe.trade
    trade.orderStatus.status = "Cancelled"
    trade.orderStatus.filled = 0.0
    trade.orderStatus.avgFillPrice = 0.0
    await engine._on_entry_status(trade)


class _FakeStrategy:
    def __init__(self) -> None:
        self.in_position = False
        self.entered_calls = 0
        self.exited_calls = 0
        self.failed_setups = 0

    def mark_entered(self) -> None:
        self.in_position = True
        self.entered_calls += 1

    def mark_exited(self) -> None:
        self.in_position = False
        self.exited_calls += 1

    def record_failed_setup(self) -> None:
        self.failed_setups += 1

    def snapshot(self) -> dict[str, Any]:
        return {"in_position": self.in_position}


# Use the v1.1 fast-path FirstPullback config: skip microstructure gates
# in tests by constructing the engine with strategy=_FakeStrategy() which
# is NOT an isinstance of FirstPullbackLong, so the gate is auto-bypassed.


# ----- engine harness ------------------------------------------------------


def _build_engine(
    *,
    autonomous: bool = True,
    portfolio_caps: PortfolioRiskCaps | None = None,
    risk_caps: RiskCaps | None = None,
) -> tuple[TradingEngine, _FakeJournal, _FakeExecutor, PortfolioRiskGate, _FakeStrategy]:
    """Construct a TradingEngine wired with stubs and a REAL
    PortfolioRiskGate / RiskGate. Returns (engine, journal, executor,
    portfolio_risk, strategy) for inspection."""
    cfg = EngineConfig(
        symbol="SKYQ",
        strategy_name="first_pullback_long",
        strategy_params={},
        quantity=10,
        autonomous=autonomous,
        risk_caps=risk_caps or RiskCaps(),
    )
    portfolio_risk = PortfolioRiskGate(portfolio_caps)

    engine = TradingEngine.__new__(TradingEngine)
    engine.config = cfg
    engine.ibkr = None
    engine.broker = None
    engine.settings = None
    engine.portfolio_risk = portfolio_risk

    journal = _FakeJournal()
    executor = _FakeExecutor()
    strategy = _FakeStrategy()
    risk_gate = RiskGate(_FakeSettings(), cfg.risk_caps)
    exits = ExitTriggerSet(ExitConfig())

    engine.spec = None
    engine.run_id = 1
    engine.journal = journal
    engine.strategy = strategy
    engine.feed = None
    engine.tf5 = None
    engine.executor = executor
    engine.risk = risk_gate
    engine.exits = exits

    engine.market_state = None
    engine._depth_ticker = None
    engine._tape_ticker = None
    engine._quote_ticker = None

    engine._entry_price = None
    engine._entry_ts = None
    engine._holds_portfolio_mutex = False

    engine._pending = None
    engine._pending_entry = None
    engine._stop_event = asyncio.Event()
    engine._running_task = None
    engine._depth_watchdog_task = None

    return engine, journal, executor, portfolio_risk, strategy


@dataclass
class _FakeSettings:
    paper_trading_only: bool = True
    live_trading_enabled: bool = False


def _signal_at(price: float = 100.0, ts: dt.datetime | None = None) -> Signal:
    return Signal(
        kind=SignalKind.ENTER_LONG,
        ts=ts or dt.datetime(2026, 6, 26, 14, 30, tzinfo=dt.UTC),
        price=price,
        reason="test_enter",
        extras={"stop_suggestion": price * 0.98},
    )


def _bar_at(close: float = 102.0, ts: dt.datetime | None = None) -> Bar:
    return Bar(
        ts=ts or dt.datetime(2026, 6, 26, 14, 35, tzinfo=dt.UTC),
        open=close - 0.5,
        high=close + 0.5,
        low=close - 1.0,
        close=close,
        volume=10_000.0,
    )


# ----- 1. Mutex acquired before submit ------------------------------------


@pytest.mark.asyncio
async def test_handle_enter_acquires_mutex_when_free() -> None:
    engine, journal, executor, prg, strategy = _build_engine()

    await engine._handle_enter(_bar_at(), _signal_at(price=100.0), snap=None)

    assert engine._holds_portfolio_mutex is True
    assert prg.holder() == "SKYQ"
    assert len(executor.calls) == 1
    assert executor.calls[0]["side"] == "BUY"
    assert strategy.entered_calls == 1
    assert "entry_blocked_by_portfolio_mutex" not in journal.kinds()


@pytest.mark.asyncio
async def test_handle_enter_denied_when_mutex_held_elsewhere() -> None:
    engine, journal, executor, prg, strategy = _build_engine()
    # Pre-acquire the mutex from a "different engine"
    await prg.try_acquire_for_entry("FRTT", 5)

    await engine._handle_enter(_bar_at(), _signal_at(price=100.0), snap=None)

    assert engine._holds_portfolio_mutex is False
    assert prg.holder() == "FRTT"  # FRTT still holds it
    assert len(executor.calls) == 0  # no order submitted

    blocks = journal.payloads_for("entry_blocked_by_portfolio_mutex")
    assert len(blocks) == 1
    assert blocks[0]["symbol"] == "SKYQ"
    assert blocks[0]["current_holder"] == "FRTT"
    assert blocks[0]["reason"] == "position_held_by:FRTT"
    assert blocks[0]["signal"]["price"] == 100.0

    # Strategy was unlatched so it can retry next bar
    assert strategy.exited_calls == 1


@pytest.mark.asyncio
async def test_handle_enter_denied_when_kill_switch_on() -> None:
    engine, journal, executor, prg, _ = _build_engine(
        portfolio_caps=PortfolioRiskCaps(max_daily_loss_usd=50.0)
    )
    # Trip the kill switch with a separate symbol
    await prg.try_acquire_for_entry("FRTT", 5)
    await prg.release("FRTT", realized_pnl_usd=-60.0)

    await engine._handle_enter(_bar_at(), _signal_at(), snap=None)

    assert engine._holds_portfolio_mutex is False
    assert len(executor.calls) == 0
    blocks = journal.payloads_for("entry_blocked_by_portfolio_mutex")
    assert len(blocks) == 1
    assert blocks[0]["reason"] == "kill_switch_on"


@pytest.mark.asyncio
async def test_handle_enter_does_not_consult_mutex_when_portfolio_risk_is_none() -> None:
    """Constructing a TradingEngine without a portfolio_risk (e.g. in
    a unit test that's exercising legacy v1.1 paths) must leave the
    mutex code path inert. No acquire, no journal block event."""
    engine, journal, executor, _, _ = _build_engine()
    engine.portfolio_risk = None

    await engine._handle_enter(_bar_at(), _signal_at(), snap=None)

    assert engine._holds_portfolio_mutex is False
    assert len(executor.calls) == 1
    assert "entry_blocked_by_portfolio_mutex" not in journal.kinds()


# ----- 2. Mutex released on submit failure --------------------------------


@pytest.mark.asyncio
async def test_handle_enter_releases_mutex_on_submit_failure() -> None:
    engine, _, executor, prg, _ = _build_engine()
    executor.return_none = True

    await engine._handle_enter(_bar_at(), _signal_at(price=100.0), snap=None)

    # Submit failed: mutex released, no holder
    assert engine._holds_portfolio_mutex is False
    assert prg.holder() is None
    # ... but the trade attempt still counts toward daily total
    assert prg.snapshot()["trades_count"] == 1


# ----- 3. Mutex released on full close (_handle_exit_decision) ------------


@pytest.mark.asyncio
async def test_handle_exit_decision_releases_mutex_on_full_close() -> None:
    engine, _, executor, prg, _ = _build_engine()
    # Enter first + confirm fill (v1.3 fill-gated entry; without the
    # simulated fill the engine would still be in pending-entry state
    # and have no position to exit).
    await engine._handle_enter(_bar_at(close=100.0), _signal_at(price=100.0), snap=None)
    await _simulate_full_fill(engine, price=100.0, qty=10)
    assert engine._holds_portfolio_mutex is True

    # Now drive a full-close exit decision
    exit_bar = _bar_at(close=105.0)
    decision = ExitDecision(
        kind=ExitTriggerKind.HARD_STOP,
        reason="test_stop",
        fraction=1.0,
        price_observed=105.0,
        extras={},
    )
    await engine._handle_exit_decision(exit_bar, decision)

    assert engine._holds_portfolio_mutex is False
    assert prg.holder() is None
    # P&L was added to portfolio aggregate: (105-100) * 10 = 50.0
    assert prg.snapshot()["realized_pnl_usd"] == pytest.approx(50.0)


@pytest.mark.asyncio
async def test_handle_exit_decision_partial_close_keeps_mutex() -> None:
    engine, _, executor, prg, _ = _build_engine()
    await engine._handle_enter(_bar_at(close=100.0), _signal_at(price=100.0), snap=None)
    await _simulate_full_fill(engine, price=100.0, qty=10)
    assert engine._holds_portfolio_mutex is True

    # 50% partial close — the engine code does `qty = max(int(held * 0.5), 0)`
    # which for qty=10 gives qty=5 < held=10, so this is a partial.
    exit_bar = _bar_at(close=105.0)
    decision = ExitDecision(
        kind=ExitTriggerKind.FIRST_TARGET,
        reason="test_target_50",
        fraction=0.5,
        price_observed=105.0,
        extras={},
    )
    await engine._handle_exit_decision(exit_bar, decision)

    # Partial close: still in position, mutex still held
    assert engine._holds_portfolio_mutex is True
    assert prg.holder() == "SKYQ"


# ----- 4. Mutex released on legacy exit signal path -----------------------


@pytest.mark.asyncio
async def test_handle_exit_signal_releases_mutex_on_full_close() -> None:
    engine, _, executor, prg, _ = _build_engine()
    await engine._handle_enter(_bar_at(close=100.0), _signal_at(price=100.0), snap=None)
    await _simulate_full_fill(engine, price=100.0, qty=10)
    assert engine._holds_portfolio_mutex is True

    exit_signal = Signal(
        kind=SignalKind.EXIT_LONG,
        ts=dt.datetime(2026, 6, 26, 14, 40, tzinfo=dt.UTC),
        price=98.0,
        reason="legacy_exit",
    )
    await engine._handle_exit_signal(exit_signal)

    assert engine._holds_portfolio_mutex is False
    assert prg.holder() is None
    # P&L: (98-100) * 10 = -20.0
    assert prg.snapshot()["realized_pnl_usd"] == pytest.approx(-20.0)


# ----- 5. Mutex released on engine.stop() while in position ---------------


@pytest.mark.asyncio
async def test_stop_releases_mutex_when_held() -> None:
    engine, journal, _, prg, _ = _build_engine()
    await engine._handle_enter(_bar_at(close=100.0), _signal_at(price=100.0), snap=None)
    await _simulate_full_fill(engine, price=100.0, qty=10)
    assert engine._holds_portfolio_mutex is True
    assert prg.holder() == "SKYQ"

    # Patch _set_run_status to no-op since we have no DB
    async def _noop(*_a: Any, **_kw: Any) -> None:
        return None

    engine._set_run_status = _noop  # type: ignore[method-assign]

    await engine.stop(reason="test_stop_while_in_position")

    assert engine._holds_portfolio_mutex is False
    assert prg.holder() is None

    # And we logged a warning because the position is still open at "IBKR"
    warnings = journal.payloads_for("warning")
    assert len(warnings) == 1
    assert warnings[0]["where"] == "stop"


@pytest.mark.asyncio
async def test_entry_cancelled_without_fill_does_not_open_position() -> None:
    """Regression: HKIT incident Fri 26 Jun PM.

    Before this fix, the engine opened the exit framework and marked
    `risk.state.open_position_qty = quantity` at order SUBMIT time. If
    the BUY then cancelled with zero fills (wide-spread micro-cap), the
    engine was left with a phantom position — next bar's hard_stop
    exit fired and submitted a SELL to close something that never
    existed.

    Now the engine waits for IBKR to confirm a fill before opening
    position state. If the BUY cancels with zero fills, everything
    rolls back cleanly.
    """
    engine, journal, executor, prg, strategy = _build_engine()

    await engine._handle_enter(_bar_at(close=100.0), _signal_at(price=100.0), snap=None)
    # Post-submit state: mutex acquired, strategy latched, but NO
    # position open yet, NO exits armed.
    assert engine._holds_portfolio_mutex is True
    assert prg.holder() == "SKYQ"
    assert engine._pending_entry is not None
    assert engine.risk.state.open_position_qty == 0
    assert engine._entry_price is None
    assert engine.exits.state is None  # exit framework not armed

    # Simulate IBKR cancelling the order with 0 fills (cancel-on-timeout).
    await _simulate_cancel_no_fill(engine)

    # All entry-side state rolled back; mutex released.
    assert engine._pending_entry is None
    assert engine._holds_portfolio_mutex is False
    assert prg.holder() is None
    assert engine.risk.state.open_position_qty == 0
    assert engine._entry_price is None
    assert engine.exits.state is None
    # Strategy unlatched so it can try again on the next signal
    assert strategy.in_position is False
    # Journaled for the audit trail. The event now uses the `error`
    # event_type (which the DB enum accepts) with `kind` in the payload
    # so we discriminate below. Previously we used an ad-hoc
    # `entry_cancelled_without_fill` event_type which the DB enum did
    # NOT accept — the INSERT silently failed in production while this
    # test (fake journal) passed. See journal.py `_TOPIC_MAP`.
    cancels = [
        p for p in journal.payloads_for("error")
        if p.get("kind") == "entry_cancelled_without_fill"
    ]
    assert len(cancels) == 1


@pytest.mark.asyncio
async def test_entry_first_fill_opens_exits_with_actual_fill_price() -> None:
    """Once the BUY confirms its first fill, the engine promotes
    pending-entry → in-position. The entry price recorded for the exit
    framework is the ACTUAL avg fill price from IBKR, not the signal
    price — important because the executor adds an offset to the signal
    price and the fill can vary further from the limit due to spread."""
    engine, _, _, _, _ = _build_engine()

    await engine._handle_enter(_bar_at(close=100.0), _signal_at(price=100.0), snap=None)
    # Pre-fill: no position, no exits
    assert engine.risk.state.open_position_qty == 0
    assert engine.exits.state is None

    # IBKR fills at 100.05 (slightly above signal price of 100.0)
    await _simulate_full_fill(engine, price=100.05, qty=10)

    # Position recorded at FILL price, not signal price
    assert engine._entry_price == 100.05
    assert engine.risk.state.open_position_qty == 10
    assert engine.exits.state is not None
    assert engine.exits.state.entry_price == 100.05


@pytest.mark.asyncio
async def test_status_filled_before_fill_event_does_not_orphan_position() -> None:
    """Regression: TC 2026-07-01 incident.

    ib_async fires `fillEvent` and `statusEvent(Filled)` on the SAME
    Trade around the same time. Both handlers schedule via
    `loop.create_task`, so the ordering between `_on_entry_fill` and
    `_on_entry_status(Filled)` is racy. In the incident, statusEvent
    ran first and cleared `_pending_entry`; the subsequent fill
    handler then bailed on `pe is None`, never calling `record_open`
    or `exits.open`. Result: 100 TC filled at IBKR but the engine's
    `open_position_qty=0` and no exit framework armed. The auto-arm
    staleness watcher then killed the engine 7s later, leaving an
    orphaned paper position.

    Fix: `record_open` runs BEFORE the pending_entry guard, and the
    "clear _pending_entry on Filled" branch moved out of
    `_on_entry_status` and into `_on_entry_fill` where it's atomic
    with the position promotion. This test exercises the exact race
    ordering that caused the incident.
    """
    engine, _, _, _, _ = _build_engine()
    await engine._handle_enter(_bar_at(close=100.0), _signal_at(price=100.0), snap=None)
    pe = engine._pending_entry
    assert pe is not None
    trade = pe.trade
    trade.orderStatus.status = "Filled"
    trade.orderStatus.filled = 10.0
    trade.orderStatus.avgFillPrice = 100.05

    # Race: statusEvent handler fires FIRST (used to clear
    # _pending_entry → break the fill handler).
    await engine._on_entry_status(trade)
    # Now fillEvent handler fires.
    await engine._on_entry_fill(trade)

    # Position must still be tracked, exits armed, entry price set —
    # regardless of the handler ordering.
    assert engine.risk.state.open_position_qty == 10, (
        "record_open was skipped due to the fillEvent/statusEvent race; "
        "the engine believes it has no position while IBKR shows a fill."
    )
    assert engine._entry_price == 100.05
    assert engine.exits.state is not None
    assert engine.exits.state.entry_price == 100.05
    # _pending_entry should be cleared by the fill handler (now the sole
    # owner of that lifecycle).
    assert engine._pending_entry is None


@pytest.mark.asyncio
async def test_stop_is_inert_when_no_mutex_held() -> None:
    engine, journal, _, prg, _ = _build_engine()
    # Engine never entered a position
    assert engine._holds_portfolio_mutex is False

    async def _noop(*_a: Any, **_kw: Any) -> None:
        return None

    engine._set_run_status = _noop  # type: ignore[method-assign]

    await engine.stop(reason="test_stop_idle")

    # No warning logged, no mutex change
    warnings = journal.payloads_for("warning")
    assert len(warnings) == 0
    assert prg.holder() is None


# ----- approximate P&L helper -------------------------------------------


@pytest.mark.asyncio
async def test_approx_realized_pnl_returns_zero_when_no_entry_tracked() -> None:
    engine, _, _, _, _ = _build_engine()
    engine._entry_price = None
    assert engine._approx_realized_pnl(exit_price=100.0, qty=10) == 0.0


@pytest.mark.asyncio
async def test_approx_realized_pnl_is_winning() -> None:
    engine, _, _, _, _ = _build_engine()
    engine._entry_price = 100.0
    assert engine._approx_realized_pnl(exit_price=105.0, qty=10) == pytest.approx(50.0)


@pytest.mark.asyncio
async def test_approx_realized_pnl_is_losing() -> None:
    engine, _, _, _, _ = _build_engine()
    engine._entry_price = 100.0
    assert engine._approx_realized_pnl(exit_price=97.5, qty=10) == pytest.approx(-25.0)
