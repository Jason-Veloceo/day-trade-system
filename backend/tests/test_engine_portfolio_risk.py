"""Tests for the portfolio-level execution mutex + daily caps.

Covers:
  - acquire when free / denied when held
  - release frees the mutex
  - release with the wrong symbol is a no-op (defensive)
  - trade count increments on every acquire
  - max_total_trades_per_day denies further entries
  - max_daily_loss_usd trips the kill switch
  - kill switch denies further entries until manual reset
  - concurrent acquire attempts are serialised correctly
  - day-roll resets the daily aggregate
"""

from __future__ import annotations

import asyncio
import datetime as dt
from unittest.mock import patch

import pytest

from day_trade.engine.portfolio_risk import (
    PortfolioDailyState,
    PortfolioRiskCaps,
    PortfolioRiskGate,
)

# ----- basic mutex semantics -----------------------------------------------


@pytest.mark.asyncio
async def test_acquire_when_free_grants_mutex() -> None:
    gate = PortfolioRiskGate()
    result = await gate.try_acquire_for_entry("SKYQ", 10)
    assert result.granted is True
    assert result.reason == "granted"
    assert result.holder == "SKYQ"
    assert gate.is_holding() is True
    assert gate.holder() == "SKYQ"


@pytest.mark.asyncio
async def test_acquire_when_held_is_denied() -> None:
    gate = PortfolioRiskGate()
    await gate.try_acquire_for_entry("SKYQ", 10)

    result = await gate.try_acquire_for_entry("FRTT", 20)
    assert result.granted is False
    assert result.reason == "position_held_by:SKYQ"
    assert result.holder == "SKYQ"
    # FRTT did NOT become the holder
    assert gate.holder() == "SKYQ"


@pytest.mark.asyncio
async def test_release_frees_the_mutex() -> None:
    gate = PortfolioRiskGate()
    await gate.try_acquire_for_entry("SKYQ", 10)
    await gate.release("SKYQ", realized_pnl_usd=5.50)

    assert gate.is_holding() is False
    assert gate.holder() is None

    # Now FRTT can acquire
    result = await gate.try_acquire_for_entry("FRTT", 20)
    assert result.granted is True
    assert gate.holder() == "FRTT"


@pytest.mark.asyncio
async def test_release_with_wrong_symbol_is_noop() -> None:
    """A misbehaving engine releasing someone else's mutex must not
    succeed — the legitimate holder will release it later in its own
    lifecycle."""
    gate = PortfolioRiskGate()
    await gate.try_acquire_for_entry("SKYQ", 10)

    # FRTT (not the holder) tries to release
    await gate.release("FRTT", realized_pnl_usd=-50.0)

    # SKYQ is still the holder, daily P&L unchanged
    assert gate.holder() == "SKYQ"
    snap = gate.snapshot()
    assert snap["realized_pnl_usd"] == 0.0


@pytest.mark.asyncio
async def test_release_pnl_accumulates_into_daily_total() -> None:
    gate = PortfolioRiskGate()
    await gate.try_acquire_for_entry("SKYQ", 10)
    await gate.release("SKYQ", realized_pnl_usd=15.0)

    await gate.try_acquire_for_entry("FRTT", 20)
    await gate.release("FRTT", realized_pnl_usd=-10.0)

    snap = gate.snapshot()
    assert snap["realized_pnl_usd"] == 5.0
    assert snap["trades_count"] == 2


# ----- trade count cap ------------------------------------------------------


@pytest.mark.asyncio
async def test_trade_count_increments_on_acquire_even_with_zero_pnl() -> None:
    """Every successful acquire counts, even if the entry later cancels
    without filling (pnl=0). This throttles bots in bad markets."""
    gate = PortfolioRiskGate(PortfolioRiskCaps(max_total_trades_per_day=3))

    for _ in range(3):
        await gate.try_acquire_for_entry("SKYQ", 10)
        await gate.release("SKYQ", realized_pnl_usd=0.0)  # cancelled

    snap = gate.snapshot()
    assert snap["trades_count"] == 3


@pytest.mark.asyncio
async def test_max_total_trades_per_day_denies_further_entries() -> None:
    gate = PortfolioRiskGate(PortfolioRiskCaps(max_total_trades_per_day=2))

    r1 = await gate.try_acquire_for_entry("SKYQ", 10)
    assert r1.granted is True
    await gate.release("SKYQ", realized_pnl_usd=5.0)

    r2 = await gate.try_acquire_for_entry("FRTT", 10)
    assert r2.granted is True
    await gate.release("FRTT", realized_pnl_usd=5.0)

    # Third attempt should be denied
    r3 = await gate.try_acquire_for_entry("LHSW", 10)
    assert r3.granted is False
    assert r3.reason.startswith("max_total_trades_per_day:")


# ----- kill switch ----------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_switch_trips_when_cumulative_pnl_hits_cap() -> None:
    gate = PortfolioRiskGate(PortfolioRiskCaps(max_daily_loss_usd=100.0))

    await gate.try_acquire_for_entry("SKYQ", 10)
    await gate.release("SKYQ", realized_pnl_usd=-60.0)
    assert gate.snapshot()["kill_switch_on"] is False

    await gate.try_acquire_for_entry("FRTT", 10)
    await gate.release("FRTT", realized_pnl_usd=-50.0)  # cumulative -110
    assert gate.snapshot()["kill_switch_on"] is True


@pytest.mark.asyncio
async def test_kill_switch_denies_further_entries() -> None:
    gate = PortfolioRiskGate(PortfolioRiskCaps(max_daily_loss_usd=50.0))
    await gate.try_acquire_for_entry("SKYQ", 10)
    await gate.release("SKYQ", realized_pnl_usd=-60.0)  # trips kill

    result = await gate.try_acquire_for_entry("FRTT", 10)
    assert result.granted is False
    assert result.reason == "kill_switch_on"


@pytest.mark.asyncio
async def test_kill_switch_exact_boundary_trips() -> None:
    """Cap is -100.0; cumulative -100.0 should also trip (<=)."""
    gate = PortfolioRiskGate(PortfolioRiskCaps(max_daily_loss_usd=100.0))
    await gate.try_acquire_for_entry("SKYQ", 10)
    await gate.release("SKYQ", realized_pnl_usd=-100.0)
    assert gate.snapshot()["kill_switch_on"] is True


@pytest.mark.asyncio
async def test_kill_switch_manual_reset() -> None:
    gate = PortfolioRiskGate(PortfolioRiskCaps(max_daily_loss_usd=50.0))
    await gate.try_acquire_for_entry("SKYQ", 10)
    await gate.release("SKYQ", realized_pnl_usd=-60.0)
    assert gate.snapshot()["kill_switch_on"] is True

    await gate.reset_kill_switch()
    assert gate.snapshot()["kill_switch_on"] is False
    # Realized P&L preserved
    assert gate.snapshot()["realized_pnl_usd"] == -60.0

    # Entries can flow again
    result = await gate.try_acquire_for_entry("FRTT", 10)
    assert result.granted is True


# ----- failure precedence ---------------------------------------------------


@pytest.mark.asyncio
async def test_kill_switch_takes_precedence_over_other_denials() -> None:
    """If both the kill switch is on AND a position is held, the denial
    reason should be `kill_switch_on` (most fatal first)."""
    gate = PortfolioRiskGate(PortfolioRiskCaps(max_daily_loss_usd=50.0))
    await gate.try_acquire_for_entry("SKYQ", 10)
    await gate.release("SKYQ", realized_pnl_usd=-60.0)  # trips kill
    await gate.try_acquire_for_entry("FRTT", 10)  # wait, won't grant — kill is on

    # Confirm FRTT didn't acquire
    assert gate.holder() is None

    # Manually inject a holder state (using internal state — for test only)
    # to verify precedence ordering
    gate._holder = "FRTT"

    result = await gate.try_acquire_for_entry("LHSW", 10)
    assert result.granted is False
    assert result.reason == "kill_switch_on"


@pytest.mark.asyncio
async def test_trade_count_cap_takes_precedence_over_holder() -> None:
    """If both the trade cap is reached AND a position is held, the
    denial reason should be the trade-count cap."""
    gate = PortfolioRiskGate(PortfolioRiskCaps(max_total_trades_per_day=2))
    await gate.try_acquire_for_entry("SKYQ", 10)
    await gate.release("SKYQ", realized_pnl_usd=0.0)
    await gate.try_acquire_for_entry("FRTT", 10)
    # FRTT is now the holder, trade count == 2

    result = await gate.try_acquire_for_entry("LHSW", 10)
    assert result.granted is False
    assert result.reason.startswith("max_total_trades_per_day:")


# ----- concurrency ---------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_acquires_are_serialised() -> None:
    """Three engines try to acquire the mutex simultaneously. Exactly
    one wins; the other two see `position_held_by:<winner>`."""
    gate = PortfolioRiskGate()

    async def attempt(symbol: str) -> tuple[str, bool, str]:
        result = await gate.try_acquire_for_entry(symbol, 10)
        return symbol, result.granted, result.reason

    results = await asyncio.gather(
        attempt("SKYQ"),
        attempt("FRTT"),
        attempt("LHSW"),
    )

    granted = [r for r in results if r[1] is True]
    denied = [r for r in results if r[1] is False]

    assert len(granted) == 1
    assert len(denied) == 2
    winner = granted[0][0]
    for _sym, _g, reason in denied:
        assert reason == f"position_held_by:{winner}"


# ----- day roll ------------------------------------------------------------


@pytest.mark.asyncio
async def test_day_roll_resets_daily_aggregate() -> None:
    """Crossing UTC midnight resets P&L, trade count, kill switch."""
    gate = PortfolioRiskGate(PortfolioRiskCaps(max_daily_loss_usd=100.0))

    # Force "today" to be a fixed past date by injecting state
    yesterday = dt.date(2026, 6, 25)
    gate._daily = PortfolioDailyState(
        realized_pnl_usd=-150.0,
        trades_count=8,
        kill_switch_on=True,
        day_utc=yesterday,
    )

    today = dt.date(2026, 6, 26)
    fake_now = dt.datetime(2026, 6, 26, 0, 5, tzinfo=dt.UTC)

    with patch("day_trade.engine.portfolio_risk.dt") as mock_dt:
        mock_dt.datetime.now.return_value = fake_now
        mock_dt.timezone = dt.timezone
        mock_dt.date = dt.date

        # An acquire forces _maybe_roll_day
        result = await gate.try_acquire_for_entry("SKYQ", 10)

    assert result.granted is True  # fresh day, no kill switch
    snap = gate.snapshot()
    assert snap["realized_pnl_usd"] == 0.0
    assert snap["trades_count"] == 1  # this one just-acquired
    assert snap["kill_switch_on"] is False
    assert snap["day_utc"] == today.isoformat()


# ----- snapshot ------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_includes_caps_and_state() -> None:
    caps = PortfolioRiskCaps(
        max_daily_loss_usd=250.0,
        max_concurrent_engines=3,
        max_total_trades_per_day=7,
    )
    gate = PortfolioRiskGate(caps)
    await gate.try_acquire_for_entry("SKYQ", 10)

    snap = gate.snapshot()
    assert snap["caps"]["max_daily_loss_usd"] == 250.0
    assert snap["caps"]["max_concurrent_engines"] == 3
    assert snap["caps"]["max_total_trades_per_day"] == 7
    assert snap["holder"] == "SKYQ"
    assert snap["is_holding"] is True
    assert snap["realized_pnl_usd"] == 0.0
    assert snap["trades_count"] == 1
    assert snap["kill_switch_on"] is False
    assert snap["day_utc"] is not None
