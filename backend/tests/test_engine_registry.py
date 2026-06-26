"""Tests for the multi-engine registry.

Uses a `FakeEngine` injected via the registry's `engine_factory` so we
can test the registry in isolation from `TradingEngine`'s IBKR / DB /
broker dependencies.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from day_trade.engine.engine import EngineConfig
from day_trade.engine.portfolio_risk import PortfolioRiskCaps
from day_trade.engine.registry import (
    EngineAlreadyRunningError,
    EngineRegistry,
    EngineSlotFullError,
)
from day_trade.engine.risk import RiskCaps

# ----- fake engine ----------------------------------------------------------


_NEXT_RUN_ID = 0


def _next_run_id() -> int:
    global _NEXT_RUN_ID
    _NEXT_RUN_ID += 1
    return _NEXT_RUN_ID


class _FakeIbkr:
    account = "DUTEST00"


class _FakeRiskState:
    trades_count = 0
    open_position_qty = 0
    realized_pnl_usd = 0.0
    kill_switch_on = False


class _FakeRisk:
    state = _FakeRiskState()


class _FakeStrategy:
    def snapshot(self) -> dict[str, Any]:
        return {"name": "fake", "macd_1m_hist": 0.0}


class FakeEngine:
    """Lightweight engine stand-in for registry tests. Implements the
    minimum surface the registry touches: status, run_id, config, ibkr,
    risk, strategy, market_state, _pending, start, stop, approve_pending,
    reject_pending."""

    def __init__(self, config: EngineConfig) -> None:
        self.config = config
        self.run_id: int | None = None
        self._status = "created"
        self.ibkr = _FakeIbkr()
        self.risk = _FakeRisk()
        self.strategy = _FakeStrategy()
        self.market_state = None
        self._pending = None
        self._pending_approved = False
        self._pending_rejected = False
        self._stop_reason: str | None = None

    @property
    def status(self) -> str:
        return self._status

    async def start(self) -> int:
        self.run_id = _next_run_id()
        self._status = "running"
        return self.run_id

    async def stop(self, reason: str = "user_stop") -> None:
        self._status = "stopped"
        self._stop_reason = reason

    def approve_pending(self) -> bool:
        self._pending_approved = True
        return True

    def reject_pending(self) -> bool:
        self._pending_rejected = True
        return True


def _build_registry(
    max_concurrent: int = 4, max_daily_loss: float = 200.0
) -> EngineRegistry:
    caps = PortfolioRiskCaps(
        max_concurrent_engines=max_concurrent,
        max_daily_loss_usd=max_daily_loss,
    )
    return EngineRegistry(
        engine_factory=lambda cfg, _risk: FakeEngine(cfg),
        portfolio_caps=caps,
    )


def _start_kwargs(symbol: str, **overrides: Any) -> dict[str, Any]:
    base = {
        "symbol": symbol,
        "strategy_name": "first_pullback_long",
        "strategy_params": {"trigger_mode": "pullback_break"},
        "quantity": 10,
        "autonomous": False,
        "risk_caps": RiskCaps(),
    }
    base.update(overrides)
    return base


# ----- start / stop --------------------------------------------------------


@pytest.mark.asyncio
async def test_start_engine_tracks_it_in_registry() -> None:
    reg = _build_registry()
    run_id = await reg.start(**_start_kwargs("SKYQ"))

    assert run_id > 0
    assert reg.engine_for_symbol("SKYQ") is not None
    assert reg.engine_for_run_id(run_id) is not None
    assert len(reg.active()) == 1


@pytest.mark.asyncio
async def test_start_multiple_engines_on_different_symbols() -> None:
    reg = _build_registry()
    rid1 = await reg.start(**_start_kwargs("SKYQ"))
    rid2 = await reg.start(**_start_kwargs("FRTT"))
    rid3 = await reg.start(**_start_kwargs("LHSW"))

    assert len({rid1, rid2, rid3}) == 3
    assert len(reg.active()) == 3
    assert reg.engine_for_symbol("SKYQ").run_id == rid1
    assert reg.engine_for_symbol("FRTT").run_id == rid2
    assert reg.engine_for_symbol("LHSW").run_id == rid3


@pytest.mark.asyncio
async def test_start_same_symbol_twice_raises_already_running() -> None:
    reg = _build_registry()
    await reg.start(**_start_kwargs("SKYQ"))

    with pytest.raises(EngineAlreadyRunningError, match="SKYQ"):
        await reg.start(**_start_kwargs("SKYQ"))


@pytest.mark.asyncio
async def test_start_beyond_max_concurrent_raises_slot_full() -> None:
    reg = _build_registry(max_concurrent=2)
    await reg.start(**_start_kwargs("SKYQ"))
    await reg.start(**_start_kwargs("FRTT"))

    with pytest.raises(EngineSlotFullError, match="2/2"):
        await reg.start(**_start_kwargs("LHSW"))


@pytest.mark.asyncio
async def test_stop_removes_engine_from_registry() -> None:
    reg = _build_registry()
    await reg.start(**_start_kwargs("SKYQ"))

    stopped = await reg.stop("SKYQ")
    assert stopped is True
    assert reg.engine_for_symbol("SKYQ") is None
    assert len(reg.active()) == 0


@pytest.mark.asyncio
async def test_stop_unknown_symbol_returns_false() -> None:
    reg = _build_registry()
    assert await reg.stop("DOESNOTEXIST") is False


@pytest.mark.asyncio
async def test_restart_same_symbol_after_stop_succeeds() -> None:
    """After stopping SKYQ, we should be able to re-arm SKYQ in a
    fresh engine slot. This validates the GC of stopped entries."""
    reg = _build_registry()
    rid1 = await reg.start(**_start_kwargs("SKYQ"))
    await reg.stop("SKYQ")

    rid2 = await reg.start(**_start_kwargs("SKYQ"))
    assert rid2 != rid1
    assert reg.engine_for_symbol("SKYQ").run_id == rid2


@pytest.mark.asyncio
async def test_stop_all_stops_every_engine() -> None:
    reg = _build_registry()
    await reg.start(**_start_kwargs("SKYQ"))
    await reg.start(**_start_kwargs("FRTT"))
    await reg.start(**_start_kwargs("LHSW"))

    stopped_count = await reg.stop_all()
    assert stopped_count == 3
    assert len(reg.active()) == 0


@pytest.mark.asyncio
async def test_stop_all_when_empty_returns_zero() -> None:
    reg = _build_registry()
    assert await reg.stop_all() == 0


# ----- approve / reject ---------------------------------------------------


@pytest.mark.asyncio
async def test_approve_routes_to_correct_engine_by_run_id() -> None:
    reg = _build_registry()
    rid1 = await reg.start(**_start_kwargs("SKYQ"))
    await reg.start(**_start_kwargs("FRTT"))

    assert reg.approve(rid1) is True
    engine_skyq = reg.engine_for_symbol("SKYQ")
    engine_frtt = reg.engine_for_symbol("FRTT")
    assert engine_skyq._pending_approved is True
    assert engine_frtt._pending_approved is False


@pytest.mark.asyncio
async def test_reject_routes_to_correct_engine_by_run_id() -> None:
    reg = _build_registry()
    await reg.start(**_start_kwargs("SKYQ"))
    rid2 = await reg.start(**_start_kwargs("FRTT"))

    assert reg.reject(rid2) is True
    engine_skyq = reg.engine_for_symbol("SKYQ")
    engine_frtt = reg.engine_for_symbol("FRTT")
    assert engine_frtt._pending_rejected is True
    assert engine_skyq._pending_rejected is False


@pytest.mark.asyncio
async def test_approve_unknown_run_id_returns_false() -> None:
    reg = _build_registry()
    await reg.start(**_start_kwargs("SKYQ"))
    assert reg.approve(run_id=99999) is False


# ----- status ------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_returns_engines_and_portfolio() -> None:
    reg = _build_registry(max_concurrent=4)
    await reg.start(**_start_kwargs("SKYQ"))
    await reg.start(**_start_kwargs("FRTT"))

    status = reg.status()
    assert "engines" in status
    assert "portfolio" in status
    assert "slots" in status

    assert len(status["engines"]) == 2
    symbols = {e["symbol"] for e in status["engines"]}
    assert symbols == {"SKYQ", "FRTT"}

    assert status["slots"] == {"active": 2, "max": 4}
    assert status["portfolio"]["is_holding"] is False
    assert status["portfolio"]["caps"]["max_concurrent_engines"] == 4


@pytest.mark.asyncio
async def test_status_for_symbol_returns_engine_dict_or_none() -> None:
    reg = _build_registry()
    await reg.start(**_start_kwargs("SKYQ"))

    skyq_status = reg.status_for_symbol("SKYQ")
    assert skyq_status is not None
    assert skyq_status["symbol"] == "SKYQ"
    assert skyq_status["status"] == "running"

    assert reg.status_for_symbol("DOESNOTEXIST") is None


# ----- portfolio risk integration -----------------------------------------


@pytest.mark.asyncio
async def test_registry_exposes_portfolio_risk_gate() -> None:
    reg = _build_registry(max_concurrent=4, max_daily_loss=300.0)
    assert reg.portfolio_risk is not None
    snap = reg.portfolio_risk.snapshot()
    assert snap["caps"]["max_concurrent_engines"] == 4
    assert snap["caps"]["max_daily_loss_usd"] == 300.0


@pytest.mark.asyncio
async def test_portfolio_mutex_is_independent_of_engine_lifecycle() -> None:
    """Acquiring the mutex doesn't auto-start an engine, and stopping an
    engine doesn't auto-release the mutex. The engine layer (Day 2)
    will wire the two together."""
    reg = _build_registry()
    await reg.start(**_start_kwargs("SKYQ"))

    result = await reg.portfolio_risk.try_acquire_for_entry("SKYQ", 10)
    assert result.granted is True

    # Stopping the SKYQ engine does NOT auto-release the mutex (Day 2
    # responsibility). The mutex still says SKYQ is the holder.
    await reg.stop("SKYQ")
    assert reg.portfolio_risk.holder() == "SKYQ"

    # The engine layer's release call (simulated here) is what frees it.
    await reg.portfolio_risk.release("SKYQ", realized_pnl_usd=0.0)
    assert reg.portfolio_risk.holder() is None


# ----- concurrent start serialisation ------------------------------------


@pytest.mark.asyncio
async def test_concurrent_starts_for_same_symbol_serialise_correctly() -> None:
    """Two parallel start requests for the same symbol: one should
    succeed, the other should raise EngineAlreadyRunningError."""
    reg = _build_registry()

    async def attempt(symbol: str) -> tuple[bool, str]:
        try:
            await reg.start(**_start_kwargs(symbol))
            return True, "ok"
        except EngineAlreadyRunningError as e:
            return False, str(e)

    results = await asyncio.gather(
        attempt("SKYQ"),
        attempt("SKYQ"),
    )
    succeeded = [r for r in results if r[0]]
    failed = [r for r in results if not r[0]]

    assert len(succeeded) == 1
    assert len(failed) == 1
    assert "already running" in failed[0][1]


@pytest.mark.asyncio
async def test_concurrent_starts_at_slot_cap_serialise_correctly() -> None:
    """Three parallel starts on different symbols with a cap of 2: the
    third should raise EngineSlotFullError."""
    reg = _build_registry(max_concurrent=2)

    async def attempt(symbol: str) -> tuple[str, bool, str]:
        try:
            await reg.start(**_start_kwargs(symbol))
            return symbol, True, "ok"
        except EngineSlotFullError as e:
            return symbol, False, str(e)

    results = await asyncio.gather(
        attempt("SKYQ"),
        attempt("FRTT"),
        attempt("LHSW"),
    )
    succeeded = [r for r in results if r[1]]
    failed = [r for r in results if not r[1]]

    assert len(succeeded) == 2
    assert len(failed) == 1
    assert "2/2" in failed[0][2]
