"""Derived L2 + T&S features used by the strategy gate stack.

All features return Python floats / None. The strategy decides whether
None means "skip gate" (no subscription) or "fail gate" (subscription
exists but no data yet).

Naming convention: every feature is named `compute_<feature>` and takes a
MarketState plus any feature-specific arguments. None of these functions
mutate the state.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .orderbook import MarketState


@dataclass(frozen=True, slots=True)
class FeatureSnapshot:
    """Snapshot of all the derived features at a moment in time. Used for
    the live feature panel on the UI and persisted to engine_events."""

    ts: dt.datetime

    # --- depth ---
    best_bid: float | None
    best_ask: float | None
    spread: float | None
    spread_bps: float | None
    mid: float | None
    bid_size_top: float | None
    ask_size_top: float | None
    bid_ask_imbalance: float | None       # bid_size / (bid_size + ask_size), 0..1
    ask_wall_price: float | None          # price of the largest ask within risk band
    ask_wall_size: float | None           # size at that price
    ask_wall_distance_bps: float | None   # how far above mid (basis points)

    # --- tape ---
    tape_count_60s: int | None
    tape_buy_volume_60s: float | None
    tape_sell_volume_60s: float | None
    tape_buy_pct_60s: float | None        # 0..1
    tape_speed_30s: float | None          # prints/sec over last 30s
    tape_speed_decay_pct: float | None    # (speed_30s - speed_60s) / speed_60s

    # --- meta ---
    has_depth: bool
    has_tape: bool

    def to_dict(self) -> dict[str, float | int | bool | None]:
        return {
            "ts": self.ts.isoformat(),
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "spread": self.spread,
            "spread_bps": self.spread_bps,
            "mid": self.mid,
            "bid_size_top": self.bid_size_top,
            "ask_size_top": self.ask_size_top,
            "bid_ask_imbalance": self.bid_ask_imbalance,
            "ask_wall_price": self.ask_wall_price,
            "ask_wall_size": self.ask_wall_size,
            "ask_wall_distance_bps": self.ask_wall_distance_bps,
            "tape_count_60s": self.tape_count_60s,
            "tape_buy_volume_60s": self.tape_buy_volume_60s,
            "tape_sell_volume_60s": self.tape_sell_volume_60s,
            "tape_buy_pct_60s": self.tape_buy_pct_60s,
            "tape_speed_30s": self.tape_speed_30s,
            "tape_speed_decay_pct": self.tape_speed_decay_pct,
            "has_depth": self.has_depth,
            "has_tape": self.has_tape,
        }


def compute_bid_ask_imbalance(state: MarketState, levels: int = 1) -> float | None:
    """Sum the top N bid sizes vs top N ask sizes; report bid_share.

    Returns None when there's no subscription. Returns 0.5 when book exists
    but has no size on either side (neutral)."""
    if not state.has_depth_subscription:
        return None
    bid_size = sum(b.size for b in state.depth.bids[:levels])
    ask_size = sum(a.size for a in state.depth.asks[:levels])
    if bid_size + ask_size <= 0:
        return 0.5
    return bid_size / (bid_size + ask_size)


def compute_spread_bps(state: MarketState) -> float | None:
    spread = state.depth.spread
    mid = state.depth.mid
    if spread is None or mid is None or mid <= 0:
        return None
    return (spread / mid) * 10_000.0


def compute_ask_wall(
    state: MarketState, *, risk_band_bps: float = 50.0
) -> tuple[float | None, float | None, float | None]:
    """Find the largest ask within `risk_band_bps` of mid. Returns
    (price, size, distance_bps). Returns (None, None, None) if no
    subscription, no asks, or no mid.

    Default 50bps band ≈ 0.5%, a reasonable "first ceiling" range for a
    small-cap momentum trade.
    """
    if not state.has_depth_subscription or state.depth.mid is None:
        return None, None, None
    mid = state.depth.mid
    upper = mid * (1.0 + risk_band_bps / 10_000.0)
    candidates = [a for a in state.depth.asks if a.price <= upper]
    if not candidates:
        return None, None, None
    biggest = max(candidates, key=lambda a: a.size)
    distance_bps = ((biggest.price - mid) / mid) * 10_000.0 if mid > 0 else 0.0
    return biggest.price, biggest.size, distance_bps


def compute_tape_buy_pct(state: MarketState, window_seconds: float = 60.0) -> float | None:
    """Fraction of last-window-seconds volume that hit the ask.

    Returns None when no tape subscription. Returns None (not 0.5) when no
    prints in window - distinguishes "no data" from "balanced flow"."""
    if not state.has_tape_subscription:
        return None
    if window_seconds <= 0:
        return None
    if not state.tape.ticks:
        return None
    cutoff = state.tape.ticks[-1].ts - dt.timedelta(seconds=window_seconds)
    buy_vol = 0.0
    sell_vol = 0.0
    for t in state.tape.ticks:
        if t.ts < cutoff:
            continue
        if t.side == "buy":
            buy_vol += t.size
        elif t.side == "sell":
            sell_vol += t.size
    total = buy_vol + sell_vol
    if total <= 0:
        return None
    return buy_vol / total


def compute_tape_speed(state: MarketState, window_seconds: float = 30.0) -> float | None:
    """Prints per second in the last `window_seconds`."""
    if not state.has_tape_subscription:
        return None
    if not state.tape.ticks:
        return None
    cutoff = state.tape.ticks[-1].ts - dt.timedelta(seconds=window_seconds)
    count = sum(1 for t in state.tape.ticks if t.ts >= cutoff)
    return count / window_seconds


def compute_snapshot(state: MarketState, *, now: dt.datetime | None = None) -> FeatureSnapshot:
    """Materialise every feature at this instant. Cheap (single pass over the
    window buffer)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    bb = state.depth.best_bid
    ba = state.depth.best_ask
    spread = state.depth.spread
    spread_bps = compute_spread_bps(state)
    mid = state.depth.mid
    imbalance = compute_bid_ask_imbalance(state, levels=1)
    wall_price, wall_size, wall_bps = compute_ask_wall(state)
    buy_pct_60 = compute_tape_buy_pct(state, window_seconds=60.0)
    speed_30 = compute_tape_speed(state, window_seconds=30.0)
    speed_60 = compute_tape_speed(state, window_seconds=60.0)
    decay = None
    if speed_30 is not None and speed_60 is not None and speed_60 > 0:
        decay = (speed_30 - speed_60) / speed_60

    # Build the tape volumes for the snapshot in one pass.
    tape_count_60 = None
    tape_buy_vol_60 = None
    tape_sell_vol_60 = None
    if state.has_tape_subscription and state.tape.ticks:
        cutoff = state.tape.ticks[-1].ts - dt.timedelta(seconds=60.0)
        tape_count_60 = 0
        tape_buy_vol_60 = 0.0
        tape_sell_vol_60 = 0.0
        for t in state.tape.ticks:
            if t.ts < cutoff:
                continue
            tape_count_60 += 1
            if t.side == "buy":
                tape_buy_vol_60 += t.size
            elif t.side == "sell":
                tape_sell_vol_60 += t.size

    return FeatureSnapshot(
        ts=now,
        best_bid=bb.price if bb else None,
        best_ask=ba.price if ba else None,
        spread=spread,
        spread_bps=spread_bps,
        mid=mid,
        bid_size_top=bb.size if bb else None,
        ask_size_top=ba.size if ba else None,
        bid_ask_imbalance=imbalance,
        ask_wall_price=wall_price,
        ask_wall_size=wall_size,
        ask_wall_distance_bps=wall_bps,
        tape_count_60s=tape_count_60,
        tape_buy_volume_60s=tape_buy_vol_60,
        tape_sell_volume_60s=tape_sell_vol_60,
        tape_buy_pct_60s=buy_pct_60,
        tape_speed_30s=speed_30,
        tape_speed_decay_pct=decay,
        has_depth=state.has_depth_subscription,
        has_tape=state.has_tape_subscription,
    )
