"""MACD crossover long-only strategy (POC scope).

Rules:
  - Compute MACD(fast, slow, signal) on the close of each 1m bar.
  - When the histogram crosses from negative to positive AND we are flat,
    emit ENTER_LONG.
  - When the histogram crosses from positive to negative AND we are long,
    emit EXIT_LONG.
  - No shorts. No pyramiding. One open position at a time.

This is deliberately the simplest strategy that exercises every part of the
engine plumbing. It is NOT meant to make money - the Ross strategy described
in strategy_sources/ross_notes.md is the eventual replacement, swapped in via
the same `Strategy` interface.
"""

from __future__ import annotations

from typing import Any

from ..indicators import MACD, MACDValue
from .base import Bar, Signal, SignalKind, Strategy


class MACDCrossoverLong(Strategy):
    name = "macd_crossover_long"

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9) -> None:
        self.macd = MACD(fast=fast, slow=slow, signal=signal)
        self._prev_hist: float | None = None
        self._in_position: bool = False
        self._last_value: MACDValue | None = None
        self._params = {"fast": fast, "slow": slow, "signal": signal}

    def on_bar(self, bar: Bar) -> Signal | None:
        value = self.macd.update(bar.close)
        if value is None:
            # Still warming up; no signal possible.
            return None
        self._last_value = value

        prev = self._prev_hist
        curr = value.histogram
        # Update the previous-hist tracker BEFORE emitting, so on consecutive
        # warm-up bars we don't false-fire from a missing prev.
        self._prev_hist = curr

        if prev is None:
            return None

        crossed_up = prev <= 0.0 < curr
        crossed_down = prev >= 0.0 > curr

        if crossed_up and not self._in_position:
            self._in_position = True
            return Signal(
                kind=SignalKind.ENTER_LONG,
                ts=bar.ts,
                price=bar.close,
                reason=(
                    f"MACD histogram crossed up (prev={prev:.6f} -> curr={curr:.6f}; "
                    f"macd={value.macd:.6f} signal={value.signal:.6f})"
                ),
                extras={
                    "macd": value.macd,
                    "signal": value.signal,
                    "histogram": value.histogram,
                    "prev_histogram": prev,
                },
            )

        if crossed_down and self._in_position:
            self._in_position = False
            return Signal(
                kind=SignalKind.EXIT_LONG,
                ts=bar.ts,
                price=bar.close,
                reason=(
                    f"MACD histogram crossed down (prev={prev:.6f} -> curr={curr:.6f}; "
                    f"macd={value.macd:.6f} signal={value.signal:.6f})"
                ),
                extras={
                    "macd": value.macd,
                    "signal": value.signal,
                    "histogram": value.histogram,
                    "prev_histogram": prev,
                },
            )

        return None

    def snapshot(self) -> dict[str, Any]:
        v = self._last_value
        return {
            "name": self.name,
            "params": self._params,
            "in_position": self._in_position,
            "prev_histogram": self._prev_hist,
            "macd_line": None if v is None else v.macd,
            "macd_signal": None if v is None else v.signal,
            "macd_histogram": None if v is None else v.histogram,
        }
