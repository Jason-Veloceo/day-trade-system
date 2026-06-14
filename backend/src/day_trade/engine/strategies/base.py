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

    @abc.abstractmethod
    def snapshot(self) -> dict[str, Any]:
        """Public state snapshot for journaling and the UI panel."""
