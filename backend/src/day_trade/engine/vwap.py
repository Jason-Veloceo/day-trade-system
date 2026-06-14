"""Session-anchored VWAP.

VWAP = sum(typical_price * volume) / sum(volume), where typical_price is
(high + low + close) / 3 and the sums are anchored at the session start (US
RTH open by default, or the first bar the engine sees, whichever is later).

Returned values:
  - `value`: current VWAP price
  - `cum_volume`: rolling sum used as the divisor
  - `state`: which side of VWAP the close sits ('above' | 'below' | 'at' | 'na')

Forex MIDPOINT bars carry no volume, so VWAP is meaningless for them - we
detect that case and report state='na' so the strategy can skip the VWAP gate
gracefully.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VwapValue:
    value: float
    cum_volume: float
    state: str  # 'above' | 'below' | 'at' | 'na'


class SessionVwap:
    """Anchored VWAP that resets when the session boundary is crossed.

    The session boundary is defined as 13:30 UTC for US RTH (9:30am ET in
    EST/EDT-naive form). The engine optionally feeds bars with `is_rth=True`,
    but for robustness we also reset when the trading_day changes between
    consecutive bars.
    """

    SESSION_OPEN_UTC = dt.time(13, 30)  # 9:30 ET, summer or winter (we don't care)

    def __init__(self) -> None:
        self._anchor_day: dt.date | None = None
        self._cum_pv: float = 0.0
        self._cum_v: float = 0.0
        self._last_value: float | None = None
        self._last_close: float | None = None

    @property
    def last(self) -> VwapValue | None:
        if self._last_value is None or self._last_close is None:
            return None
        if self._cum_v <= 0:
            return VwapValue(value=self._last_value, cum_volume=0.0, state="na")
        eps = max(1e-6, self._last_value * 1e-6)
        state = (
            "above"
            if self._last_close > self._last_value + eps
            else "below"
            if self._last_close < self._last_value - eps
            else "at"
        )
        return VwapValue(value=self._last_value, cum_volume=self._cum_v, state=state)

    def update(self, ts: dt.datetime, high: float, low: float, close: float, volume: float) -> VwapValue | None:
        """Ingest a closed bar. Returns the current VWAP value, or None if
        nothing meaningful can be reported yet (no volume seen)."""
        self._maybe_reset(ts)
        tp = (high + low + close) / 3.0
        # Volume can be 0 or negative for forex MIDPOINT bars - skip those
        # contributions but still update the last_close for state reporting.
        v = max(volume, 0.0)
        self._cum_pv += tp * v
        self._cum_v += v
        self._last_close = close
        self._last_value = (self._cum_pv / self._cum_v) if self._cum_v > 0 else close
        return self.last

    def _maybe_reset(self, ts: dt.datetime) -> None:
        if ts.tzinfo is None:
            raise ValueError("VWAP requires timezone-aware timestamps")
        ts_utc = ts.astimezone(dt.timezone.utc)
        # The anchor date is the UTC date of the most recent session open.
        # A bar at 14:00 UTC on 2026-06-15 anchors to 2026-06-15 (open was 13:30).
        # A bar at 12:00 UTC on 2026-06-15 anchors to 2026-06-14 (today's open
        # hasn't happened yet, so the relevant session opened yesterday).
        anchor_day = ts_utc.date()
        if ts_utc.time() < self.SESSION_OPEN_UTC:
            anchor_day = anchor_day - dt.timedelta(days=1)

        if self._anchor_day is None:
            self._anchor_day = anchor_day
            return
        if anchor_day != self._anchor_day:
            self._cum_pv = 0.0
            self._cum_v = 0.0
            self._last_value = None
            self._anchor_day = anchor_day

    def reset(self) -> None:
        self._anchor_day = None
        self._cum_pv = 0.0
        self._cum_v = 0.0
        self._last_value = None
        self._last_close = None
