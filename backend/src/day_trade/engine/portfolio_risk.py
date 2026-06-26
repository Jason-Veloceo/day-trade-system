"""Portfolio-level risk gate shared across all engines in the registry.

Enforces the hard invariant that **at most ONE open position exists across
the entire registry at any time** (the "execution mutex"), and tracks
portfolio-wide daily aggregates: realised P&L, trade count, and the daily
kill switch.

The mutex is acquired atomically before any engine submits an entry order
and released when the position goes flat (or the entry is cancelled
without filling). All operations are serialised via a single
`asyncio.Lock` so concurrent acquire attempts from different engines on
the same event loop cannot race.

State is held in memory for Phase 1. Phase 2 may reconstruct from the
`engine_events` table on backend boot; Phase 3 may persist the daily
aggregate directly.

See `docs/multi_engine_design.md` for the locked-in decisions this module
implements.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PortfolioRiskCaps:
    """Portfolio-wide hard caps. Defaults are conservative starting points
    for the autonomous paper-trading calibration phase; the UI surfaces
    them in a 'Portfolio settings' panel."""

    # Cumulative realised P&L across all engines today. Once daily P&L
    # falls below `-abs(max_daily_loss_usd)` the kill switch trips:
    # all engines stop, no new entries until manual reset.
    max_daily_loss_usd: float = 200.0

    # Hard cap on how many engines the registry will run concurrently.
    # Beyond this, `EngineRegistry.start` raises `EngineSlotFullError`.
    max_concurrent_engines: int = 4

    # Hard cap on entry attempts across all engines for the UTC day.
    # Each successful `try_acquire_for_entry` increments the counter.
    # Once reached, new entries are denied; existing positions are
    # allowed to exit naturally.
    max_total_trades_per_day: int = 10


@dataclass(slots=True)
class PortfolioDailyState:
    """In-memory daily aggregates. Resets at UTC midnight via
    `_maybe_roll_day`."""

    realized_pnl_usd: float = 0.0
    trades_count: int = 0
    kill_switch_on: bool = False
    day_utc: dt.date | None = None


@dataclass(frozen=True, slots=True)
class AcquireResult:
    """Result of `try_acquire_for_entry`. When `granted=False`, `reason`
    is a short machine-friendly tag (`position_held_by:FRTT`,
    `kill_switch_on`, `max_total_trades_per_day`) suitable for journal
    events. The engine should include the full reason in its
    `entry_blocked_by_portfolio_mutex` audit record."""

    granted: bool
    reason: str
    holder: str | None = None  # who holds the mutex right now (informational)


class PortfolioRiskGate:
    """Process-wide execution mutex + portfolio-cap enforcer.

    The gate is owned by the `EngineRegistry` (one per backend process).
    Every engine calls `try_acquire_for_entry` before submitting an entry
    order; the gate either grants the mutex (and marks the symbol as
    holder) or denies with a structured reason.

    Lifecycle of the mutex from any one engine's perspective:

      1. Entry signal fires after gates pass.
      2. Engine calls `try_acquire_for_entry(symbol, intended_qty)`.
      3a. Denied: engine journals `entry_blocked_by_portfolio_mutex`
          with the full gate state and continues monitoring.
      3b. Granted: engine submits the entry order to IBKR.
      4a. Entry cancelled without filling: engine calls `release(symbol)`
          with `realized_pnl_usd=0.0`. Mutex freed, daily P&L unchanged
          (trade attempt still counts toward `max_total_trades_per_day`).
      4b. Entry fills, exit triggers fire later, exit fills, position-flat:
          engine calls `release(symbol, realized_pnl_usd=<actual>)`.
          Daily P&L updated, kill switch evaluated.

    Trade-count increment policy: every successful acquire counts (even
    if the entry later cancels without filling). This is deliberate — a
    bot that tries 10 entries in a session and gets nothing filled is
    in a bad market, and the trade-count cap is meant to throttle that.
    """

    def __init__(self, caps: PortfolioRiskCaps | None = None) -> None:
        self._caps = caps or PortfolioRiskCaps()
        self._lock = asyncio.Lock()
        self._holder: str | None = None
        self._daily = PortfolioDailyState()

    @property
    def caps(self) -> PortfolioRiskCaps:
        return self._caps

    def is_holding(self) -> bool:
        """Non-async, lock-free read intended for cheap status snapshots.
        For correctness-critical decisions (entry attempts), use the
        locked path via `try_acquire_for_entry`."""
        return self._holder is not None

    def holder(self) -> str | None:
        return self._holder

    async def try_acquire_for_entry(
        self,
        symbol: str,
        intended_qty: int,
    ) -> AcquireResult:
        """Atomically check all portfolio gates and acquire the mutex if
        every gate passes. Returns an `AcquireResult` describing the
        outcome.

        Failure precedence (most-fatal first):
          - kill switch on (daily loss cap previously breached)
          - max_total_trades_per_day reached
          - position currently held by another symbol

        On success, the daily trade count is incremented and the holder
        is set to `symbol`. The caller MUST call `release(symbol, pnl)`
        eventually — even if the entry order cancels — to free the mutex.
        """
        async with self._lock:
            self._maybe_roll_day()

            if self._daily.kill_switch_on:
                return AcquireResult(
                    granted=False,
                    reason="kill_switch_on",
                    holder=self._holder,
                )

            if self._daily.trades_count >= self._caps.max_total_trades_per_day:
                return AcquireResult(
                    granted=False,
                    reason=f"max_total_trades_per_day:{self._daily.trades_count}",
                    holder=self._holder,
                )

            if self._holder is not None:
                return AcquireResult(
                    granted=False,
                    reason=f"position_held_by:{self._holder}",
                    holder=self._holder,
                )

            self._holder = symbol
            self._daily.trades_count += 1
            logger.info(
                "portfolio mutex acquired by %s (intended_qty=%d, "
                "trades_today=%d/%d)",
                symbol,
                intended_qty,
                self._daily.trades_count,
                self._caps.max_total_trades_per_day,
            )
            return AcquireResult(
                granted=True,
                reason="granted",
                holder=symbol,
            )

    async def release(
        self,
        symbol: str,
        realized_pnl_usd: float = 0.0,
    ) -> None:
        """Release the mutex held by `symbol` and update daily P&L.

        Called by the holding engine on either:
          - entry order cancellation (`realized_pnl_usd=0.0`)
          - position-flat after entry-then-exit (actual P&L)

        If `symbol` doesn't match the current holder, logs a warning and
        no-ops — this protects against a misbehaving engine releasing
        someone else's mutex. The legitimate holder will release it
        later when its own lifecycle reaches the release point.

        After the P&L update, evaluates the daily kill switch: if
        cumulative realised P&L is at or below `-abs(max_daily_loss_usd)`,
        flips `kill_switch_on=True`. The kill switch is sticky for the
        rest of the UTC day (until next `_maybe_roll_day`).
        """
        async with self._lock:
            self._maybe_roll_day()

            if self._holder != symbol:
                logger.warning(
                    "portfolio mutex release called by %s but holder is %s; "
                    "no-op",
                    symbol,
                    self._holder,
                )
                return

            self._holder = None
            self._daily.realized_pnl_usd += realized_pnl_usd

            cap = abs(self._caps.max_daily_loss_usd)
            if self._daily.realized_pnl_usd <= -cap:
                if not self._daily.kill_switch_on:
                    logger.warning(
                        "portfolio daily-loss cap breached "
                        "(realized_pnl_usd=%.2f, cap=-%.2f); kill switch ON",
                        self._daily.realized_pnl_usd,
                        cap,
                    )
                self._daily.kill_switch_on = True

            logger.info(
                "portfolio mutex released by %s (realized_pnl=%.2f, "
                "cum_pnl_today=%.2f, trades_today=%d, kill_switch=%s)",
                symbol,
                realized_pnl_usd,
                self._daily.realized_pnl_usd,
                self._daily.trades_count,
                self._daily.kill_switch_on,
            )

    async def reset_kill_switch(self) -> None:
        """Manual reset of the daily kill switch. Used by the operator
        when they've reviewed the day's trades and want to re-arm the
        bot mid-day (rare; usually you let the day roll). Does NOT reset
        the realized P&L or trade counter."""
        async with self._lock:
            if self._daily.kill_switch_on:
                logger.warning(
                    "portfolio kill switch manually reset "
                    "(cum_pnl_today=%.2f, trades_today=%d)",
                    self._daily.realized_pnl_usd,
                    self._daily.trades_count,
                )
            self._daily.kill_switch_on = False

    def snapshot(self) -> dict[str, Any]:
        """Lock-free read of the current portfolio state for status
        endpoints. May be momentarily inconsistent if read mid-acquire;
        that's acceptable for UI display."""
        return {
            "caps": asdict(self._caps),
            "holder": self._holder,
            "is_holding": self._holder is not None,
            "realized_pnl_usd": self._daily.realized_pnl_usd,
            "trades_count": self._daily.trades_count,
            "kill_switch_on": self._daily.kill_switch_on,
            "day_utc": self._daily.day_utc.isoformat()
            if self._daily.day_utc
            else None,
        }

    def _maybe_roll_day(self) -> None:
        """If we've crossed UTC midnight since the last call, reset the
        daily aggregate. Caller must hold `_lock`."""
        today = dt.datetime.now(dt.UTC).date()
        if self._daily.day_utc != today:
            if self._daily.day_utc is not None:
                logger.info(
                    "portfolio day rolled: %s -> %s "
                    "(prev day realized=%.2f, trades=%d, kill_switch=%s)",
                    self._daily.day_utc,
                    today,
                    self._daily.realized_pnl_usd,
                    self._daily.trades_count,
                    self._daily.kill_switch_on,
                )
            self._daily = PortfolioDailyState(day_utc=today)
