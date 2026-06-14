"""Lightweight L2 + T&S state for the engine.

This module is consumed by the L2/T&S features module (`features.py`). It
keeps in-memory snapshots and a sliding window of recent ticks so feature
computation is O(W) where W is the window size, not O(history).

What we store
-------------
DepthBook  -- the latest snapshot of the top-N bid/ask levels
TapeWindow -- a sliding window (default last 60 seconds) of recent prints
              and quote ticks, used for tape-speed and buy% features

The fields stay aligned with ib_async's DOMLevel / TickByTickAllLast /
TickByTickBidAsk shapes so the IBKR client can update them by mutation
without any translation.

Graceful degradation: if no L2/T&S subscription is active (e.g. forex on
IDEALPRO), the relevant features all return None and the strategy treats
their gates as "not applicable" rather than "failed".
"""

from __future__ import annotations

import datetime as dt
from collections import deque
from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True, slots=True)
class DepthLevel:
    side: Literal["bid", "ask"]
    price: float
    size: float
    market_maker: str | None = None


@dataclass(slots=True)
class DepthBook:
    """Current top-of-book + N levels of depth.

    `bids` is sorted descending by price (best bid at index 0).
    `asks` is sorted ascending by price (best ask at index 0).
    """

    bids: list[DepthLevel] = field(default_factory=list)
    asks: list[DepthLevel] = field(default_factory=list)
    updated_at: dt.datetime | None = None

    @property
    def best_bid(self) -> DepthLevel | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> DepthLevel | None:
        return self.asks[0] if self.asks else None

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask.price - self.best_bid.price

    @property
    def mid(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid.price + self.best_ask.price) / 2.0


@dataclass(frozen=True, slots=True)
class TapeTick:
    """A single print or quote update."""

    ts: dt.datetime
    price: float
    size: float
    side: Literal["buy", "sell", "unknown"]
    raw_type: str  # 'AllLast' | 'BidAsk' | etc.


class TapeWindow:
    """Sliding window of recent ticks. Pops anything older than `window_seconds`.

    Designed for engine-side feature computation, not for persistence. We rely
    on the BarFeed/Journal to persist a coarser snapshot per bar.
    """

    def __init__(self, window_seconds: float = 60.0) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self._window = dt.timedelta(seconds=window_seconds)
        self._ticks: deque[TapeTick] = deque()

    @property
    def ticks(self) -> list[TapeTick]:
        return list(self._ticks)

    @property
    def count(self) -> int:
        return len(self._ticks)

    def push(self, tick: TapeTick) -> None:
        self._ticks.append(tick)
        self._evict(now=tick.ts)

    def _evict(self, *, now: dt.datetime) -> None:
        cutoff = now - self._window
        while self._ticks and self._ticks[0].ts < cutoff:
            self._ticks.popleft()

    def reset(self) -> None:
        self._ticks.clear()


@dataclass(slots=True)
class MarketState:
    """Combined L2 + T&S state for one symbol. Lives on the IBKR client and
    is mutated as depth and tick events arrive."""

    depth: DepthBook = field(default_factory=DepthBook)
    tape: TapeWindow = field(default_factory=lambda: TapeWindow(window_seconds=60.0))
    has_depth_subscription: bool = False
    has_tape_subscription: bool = False
    last_error: str | None = None
