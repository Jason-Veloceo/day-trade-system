"""Strategy ABC + value objects.

The engine knows nothing about MACD specifically. It feeds closed bars to a
Strategy implementation and acts on whatever Signal it returns. To swap in the
real Ross strategy later, implement the same interface.
"""

from __future__ import annotations

import abc
import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


@dataclass(frozen=True, slots=True)
class Bar:
    """A single closed OHLCV bar at the strategy's working timeframe."""

    ts: dt.datetime          # bar close time, timezone-aware (UTC)
    open: float
    high: float
    low: float
    close: float
    volume: float            # 0 for forex MIDPOINT bars; that's expected


class SignalKind(StrEnum):
    ENTER_LONG = "enter_long"
    EXIT_LONG = "exit_long"
    ENTER_SHORT = "enter_short"
    EXIT_SHORT = "exit_short"


@dataclass(frozen=True, slots=True)
class Signal:
    """A trade intention. The engine decides whether to act on it (risk gate,
    autonomous flag), not the strategy."""

    kind: SignalKind
    ts: dt.datetime          # bar close time (the bar that triggered the signal)
    price: float             # the closing price used to decide -> the "signal price"
    reason: str              # human-readable why (logged on every event)
    extras: dict[str, Any] | None = None


class Strategy(abc.ABC):
    """Pluggable strategy interface.

    Conventions:
      - Strategy is stateful (it tracks position).
      - `on_bar` is called once per CLOSED 1-minute bar in chronological order.
      - `on_bar` returns at most one Signal. Multiple actions on the same bar
        are not allowed in this POC.
      - `on_5m_bar` is called once per CLOSED 5-minute bar. Default no-op for
        single-timeframe strategies. Must NOT return signals - it exists only
        to update internal trend-gate state.
      - The strategy must be safe to call from a single asyncio loop; no
        threading concerns.
    """

    name: str = "base"

    @abc.abstractmethod
    def on_bar(self, bar: Bar) -> Signal | None:
        ...

    def on_5m_bar(self, bar: Bar) -> None:
        """Update higher-timeframe state. Default no-op."""
        return None

    def on_tick(self, partial: Bar) -> Signal | None:
        """Evaluate entry conditions against the IN-PROGRESS 1m bar.

        Called by the engine on a sub-bar cadence (default 10 seconds)
        in addition to the closed-bar `on_bar` path. `partial` is a
        synthetic Bar with `ts` set to the current 1m bar's close time,
        and `open/high/low/close/volume` reflecting the running OHLC
        snapshot at the moment of the tick.

        Implementations MUST be read-only with respect to indicator
        state (MACD EMA, VWAP cumulative sums, recent-bars buffer,
        backside latches). Side effects allowed: optimistic
        `_in_position` latch to prevent intra-tick double-fires, and
        snapshot-only fields like `_last_entry_gate` / cached pullback
        info. The next `on_bar` at the actual 1m close is the source
        of truth for state mutations.

        Default: no-op. Strategies that should never fire mid-candle
        (e.g. single-timeframe POC strategies) can ignore this.
        """
        return None

    def mark_entered(self) -> None:
        """Called after an entry fill is confirmed. Default no-op."""
        return None

    def mark_exited(self) -> None:
        """Called after a full exit fill is confirmed. Lets the strategy
        auto re-arm. Default no-op."""
        return None

    def record_failed_setup(self) -> None:
        """Called when a trade closed at or below entry. Default no-op."""
        return None

    def finalize_bootstrap(
        self, *, pmhod: float | None, pdhod: float | None
    ) -> None:
        """Called once by the engine after the historical-bar replay used
        to warm indicators is complete, immediately before the live
        BarFeed starts.

        Strategies that maintain intraday latches whose lifetime should
        be "today's live session only" (not "the replay window") should
        override this to clear them. Reference levels `pmhod`
        (today's premarket high so far) and `pdhod` (most recent prior
        session's RTH high) are computed by the engine from the same
        historical bars and handed in for the strategy to retain.

        Default: no-op. Strategies that don't care about session-scoped
        latches (e.g. single-timeframe POC strategies) can ignore this.
        """
        return None

    @abc.abstractmethod
    def snapshot(self) -> dict[str, Any]:
        """Public state snapshot for journaling and the UI panel."""
