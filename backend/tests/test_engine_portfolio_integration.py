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


class _FakeExecutor:
    """Stub executor with configurable return value. By default returns
    a sentinel non-None value so the engine treats the submit as
    successful. Set `return_none=True` to simulate a submit failure."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.return_none = False

    async def execute(self, **kwargs: Any) -> object | None:
        self.calls.append(kwargs)
        if self.return_none:
            return None
        return object()


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
    engine._stop_event = asyncio.Event()
    engine._running_task = None

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
    # Enter first
    await engine._handle_enter(_bar_at(close=100.0), _signal_at(price=100.0), snap=None)
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
