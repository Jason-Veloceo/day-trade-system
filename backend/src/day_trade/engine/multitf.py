"""5-minute bar aggregator that sits on top of the 1-minute BarFeed.

The existing BarFeed produces closed 1m Bars from IBKR 5s bars. The strategy
needs BOTH 1m and 5m context (1m for the trigger, 5m for the trend gate). To
avoid forking the BarFeed we run this aggregator alongside: it consumes the
1m bars the BarFeed already emits and produces 5m bars whenever the 5-minute
bucket closes.

Bucket alignment: a 5m bar with close time 13:35:00 covers [13:30:00, 13:35:00).
We use the 1m bar's close-time minute boundary to assign it to a bucket.

The aggregator can also be primed with a backfilled list of historical 1m
bars at engine start, so the 5m MACD doesn't need ~130 minutes of live data
to warm up.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Awaitable, Callable

from .strategies.base import Bar

logger = logging.getLogger(__name__)


def _bucket_close(bar_close: dt.datetime, window_minutes: int = 5) -> dt.datetime:
    """Return the close time of the N-minute bucket that contains `bar_close`.

    A 1m bar with close time 13:31:00 contributes to the bucket closing at
    13:35:00 (because 31 is in [30, 35)).
    """
    epoch = bar_close.replace(second=0, microsecond=0)
    minute = epoch.minute
    bucket_minute = (minute // window_minutes) * window_minutes
    bucket_start = epoch.replace(minute=bucket_minute)
    return bucket_start + dt.timedelta(minutes=window_minutes)


class HigherTimeframeAggregator:
    """Generic aggregator from N x 1m bars to one N-minute bar.

    The class is timeframe-agnostic. Used here as a 5m aggregator on top of
    the existing 1m BarFeed; could also be used to derive 15m / 30m bars
    later without changing the BarFeed.
    """

    def __init__(
        self,
        *,
        window_minutes: int,
        on_close: Callable[[Bar], Awaitable[None]],
    ) -> None:
        if window_minutes < 2:
            raise ValueError("window_minutes must be >= 2 (use the 1m feed directly otherwise)")
        self._window = window_minutes
        self._on_close = on_close

        self._cur_close: dt.datetime | None = None
        self._cur_open: float | None = None
        self._cur_high: float = float("-inf")
        self._cur_low: float = float("inf")
        self._cur_last: float = 0.0
        self._cur_volume: float = 0.0

    async def push(self, m1: Bar) -> None:
        """Push a closed 1m bar. Emits the higher-timeframe bar to `on_close`
        as soon as the bucket completes."""
        bucket_end = _bucket_close(m1.ts, self._window)
        if self._cur_close is None:
            self._reset(bucket_end, m1)
            return
        if bucket_end != self._cur_close:
            await self._emit_current()
            self._reset(bucket_end, m1)
            return
        self._cur_high = max(self._cur_high, m1.high)
        self._cur_low = min(self._cur_low, m1.low)
        self._cur_last = m1.close
        self._cur_volume += max(m1.volume, 0.0)

    def prime_with_history(self, m1_bars: list[Bar]) -> list[Bar]:
        """Seed the aggregator with historical 1m bars. Returns the list of
        higher-timeframe bars that closed during the backfill - the caller
        should feed those into any indicator warm-up. We do NOT invoke the
        on_close callback for primed bars; that's the caller's choice.
        """
        emitted: list[Bar] = []
        for m1 in m1_bars:
            bucket_end = _bucket_close(m1.ts, self._window)
            if self._cur_close is None:
                self._reset(bucket_end, m1)
                continue
            if bucket_end != self._cur_close:
                emitted.append(self._snapshot())
                self._reset(bucket_end, m1)
                continue
            self._cur_high = max(self._cur_high, m1.high)
            self._cur_low = min(self._cur_low, m1.low)
            self._cur_last = m1.close
            self._cur_volume += max(m1.volume, 0.0)
        return emitted

    def _reset(self, close: dt.datetime, m1: Bar) -> None:
        self._cur_close = close
        self._cur_open = m1.open
        self._cur_high = m1.high
        self._cur_low = m1.low
        self._cur_last = m1.close
        self._cur_volume = max(m1.volume, 0.0)

    def _snapshot(self) -> Bar:
        assert self._cur_close is not None and self._cur_open is not None
        return Bar(
            ts=self._cur_close,
            open=self._cur_open,
            high=self._cur_high,
            low=self._cur_low,
            close=self._cur_last,
            volume=self._cur_volume,
        )

    async def _emit_current(self) -> None:
        bar = self._snapshot()
        try:
            await self._on_close(bar)
        except Exception:
            logger.exception("higher-timeframe aggregator callback raised")
