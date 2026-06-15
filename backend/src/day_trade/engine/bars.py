"""5-second -> 1-minute bar aggregator.

IBKR's lowest real-time bar resolution is 5 seconds. We aggregate twelve of
those into a closed 1-minute bar and emit it onto an asyncio queue. The
strategy consumes from that queue.

Why not just request 1-minute bars from IBKR? Because reqRealTimeBars only
supports 5-second size. Anything larger has to be aggregated client-side.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Awaitable, Callable

from ib_async import Contract, RealTimeBar, RealTimeBarList

from .ibkr_client import IBKRClient
from .strategies.base import Bar

logger = logging.getLogger(__name__)


def _bar_minute_close(rt_time: dt.datetime) -> dt.datetime:
    """Return the close time of the 1-minute bucket that contains `rt_time`.

    A 1-minute bar with close time 09:31:00 covers the half-open interval
    [09:30:00, 09:31:00). IBKR 5s bar times are bar START times, so a 5s bar
    at 09:30:55 contributes to the bar closing at 09:31:00.
    """
    # zero out seconds, then add 60s.
    bucket_start = rt_time.replace(second=0, microsecond=0)
    return bucket_start + dt.timedelta(minutes=1)


class BarFeed:
    """Subscribes to IBKR 5s real-time bars and emits closed 1m Bars.

    The provided `on_minute_close` callback is invoked exactly once per
    closed minute bar, on the same asyncio loop the feed was started on.
    Callback exceptions are logged but do not stop the feed.
    """

    def __init__(
        self,
        ibkr: IBKRClient,
        contract: Contract,
        what_to_show: str,
        on_minute_close: Callable[[Bar], Awaitable[None]],
    ) -> None:
        self._ibkr = ibkr
        self._contract = contract
        self._what_to_show = what_to_show
        self._on_minute_close = on_minute_close

        self._loop: asyncio.AbstractEventLoop | None = None
        self._rt_handle: RealTimeBarList | None = None

        # Accumulator for the currently-forming minute bar.
        self._cur_close: dt.datetime | None = None
        self._cur_open: float | None = None
        self._cur_high: float = float("-inf")
        self._cur_low: float = float("inf")
        self._cur_last: float = 0.0
        self._cur_volume: float = 0.0

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._rt_handle = self._ibkr.subscribe_realtime_bars(
            self._contract, self._what_to_show, self._on_rt_bar
        )
        logger.info(
            "BarFeed started: %s what_to_show=%s",
            self._contract.localSymbol or self._contract.symbol,
            self._what_to_show,
        )

    def stop(self) -> None:
        if self._rt_handle is not None:
            self._ibkr.cancel_realtime_bars(self._rt_handle)
            self._rt_handle = None
        logger.info("BarFeed stopped")

    # --- ib_async callback (runs on the same loop) ---

    def _on_rt_bar(self, bars: RealTimeBarList, has_new_bar: bool) -> None:
        if not has_new_bar or not bars:
            return
        last_bar: RealTimeBar = bars[-1]
        self._ingest(last_bar)

    def _ingest(self, b: RealTimeBar) -> None:
        # b.time is a tz-aware datetime (UTC). It is the bar START time.
        bar_close = _bar_minute_close(b.time)

        # First bar ever -> initialise the current minute.
        if self._cur_close is None:
            self._reset_minute(bar_close, b)
            return

        # If this 5s bar belongs to a NEW minute, emit the previous one.
        if bar_close != self._cur_close:
            self._emit_current()
            self._reset_minute(bar_close, b)
            return

        # Same minute -> merge.
        self._cur_high = max(self._cur_high, float(b.high))
        self._cur_low = min(self._cur_low, float(b.low))
        self._cur_last = float(b.close)
        self._cur_volume += float(b.volume) if b.volume is not None else 0.0

    def _reset_minute(self, close: dt.datetime, b: RealTimeBar) -> None:
        self._cur_close = close
        self._cur_open = float(b.open_)
        self._cur_high = float(b.high)
        self._cur_low = float(b.low)
        self._cur_last = float(b.close)
        # Forex 5s bars report volume = -1 for "no data"; clamp to 0.
        v = float(b.volume) if b.volume is not None else 0.0
        self._cur_volume = max(v, 0.0)

    def _emit_current(self) -> None:
        if self._cur_close is None or self._cur_open is None:
            return
        bar = Bar(
            ts=self._cur_close,
            open=self._cur_open,
            high=self._cur_high,
            low=self._cur_low,
            close=self._cur_last,
            volume=self._cur_volume,
        )
        loop = self._loop
        if loop is None or loop.is_closed():
            logger.warning("BarFeed cannot emit - loop missing/closed")
            return
        # ib_async callbacks run inside the asyncio loop (driven by util.run),
        # so we can directly schedule the awaitable as a task here.
        task = loop.create_task(self._safe_call(bar))
        # detach: we don't await; exceptions are handled inside _safe_call.
        task.add_done_callback(lambda _t: None)

    async def _safe_call(self, bar: Bar) -> None:
        try:
            await self._on_minute_close(bar)
        except Exception:
            logger.exception("BarFeed callback raised")
