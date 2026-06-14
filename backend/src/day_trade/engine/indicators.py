"""Incremental MACD computation.

Standard MACD: EMA(fast) - EMA(slow), then EMA(diff, signal). Histogram is
the diff between MACD and signal. We compute it incrementally so each new bar
costs O(1) and we never have to keep the full price history in memory.

EMA bootstrap: until we've seen `period` samples, we use an SMA seed; after
that we apply the standard EMA recurrence. This matches the most common
charting library behaviour (TradingView, IBKR's TWS) and avoids the cold-start
divergence you get if you start the EMA from zero or the first value.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MACDValue:
    macd: float
    signal: float
    histogram: float


class _EMA:
    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError("EMA period must be >= 1")
        self.period = period
        self._alpha = 2.0 / (period + 1)
        self._seed_sum = 0.0
        self._count = 0
        self._value: float | None = None

    def update(self, sample: float) -> float | None:
        if self._value is None:
            self._seed_sum += sample
            self._count += 1
            if self._count >= self.period:
                self._value = self._seed_sum / self.period
                return self._value
            return None
        self._value = (sample - self._value) * self._alpha + self._value
        return self._value


class MACD:
    """Incremental MACD. Call `update(close)` per bar; returns a MACDValue or
    None until both EMAs and the signal line have warmed up."""

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9) -> None:
        if fast >= slow:
            raise ValueError("MACD fast period must be < slow period")
        self.fast_period = fast
        self.slow_period = slow
        self.signal_period = signal
        self._fast = _EMA(fast)
        self._slow = _EMA(slow)
        self._signal = _EMA(signal)
        self._last: MACDValue | None = None

    @property
    def last(self) -> MACDValue | None:
        return self._last

    def update(self, close: float) -> MACDValue | None:
        fast = self._fast.update(close)
        slow = self._slow.update(close)
        if fast is None or slow is None:
            return None
        macd = fast - slow
        signal = self._signal.update(macd)
        if signal is None:
            return None
        self._last = MACDValue(macd=macd, signal=signal, histogram=macd - signal)
        return self._last
