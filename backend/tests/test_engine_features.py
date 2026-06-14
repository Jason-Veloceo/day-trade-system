"""Tests for the derived L2/T&S features."""

from __future__ import annotations

import datetime as dt

import pytest

from day_trade.engine.features import (
    compute_ask_wall,
    compute_bid_ask_imbalance,
    compute_snapshot,
    compute_spread_bps,
    compute_tape_buy_pct,
    compute_tape_speed,
)
from day_trade.engine.orderbook import DepthBook, DepthLevel, MarketState, TapeTick, TapeWindow


@pytest.fixture
def t0() -> dt.datetime:
    return dt.datetime(2026, 6, 15, 14, 30, tzinfo=dt.timezone.utc)


def _state_with_book(bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> MarketState:
    state = MarketState()
    state.has_depth_subscription = True
    state.depth = DepthBook(
        bids=[DepthLevel(side="bid", price=p, size=s) for p, s in bids],
        asks=[DepthLevel(side="ask", price=p, size=s) for p, s in asks],
        updated_at=dt.datetime.now(dt.timezone.utc),
    )
    return state


def test_imbalance_neutral_when_empty() -> None:
    s = MarketState()
    s.has_depth_subscription = True
    assert compute_bid_ask_imbalance(s) == 0.5


def test_imbalance_basic() -> None:
    s = _state_with_book([(10.0, 800)], [(10.05, 200)])
    assert compute_bid_ask_imbalance(s) == pytest.approx(0.8)


def test_imbalance_none_when_no_subscription() -> None:
    s = MarketState()
    assert compute_bid_ask_imbalance(s) is None


def test_spread_bps() -> None:
    s = _state_with_book([(10.0, 100)], [(10.10, 100)])
    bps = compute_spread_bps(s)
    assert bps is not None
    # spread 0.10 / mid 10.05 = ~99.5 bps
    assert 95 < bps < 105


def test_ask_wall_finds_largest_within_band() -> None:
    s = _state_with_book(
        [(10.0, 100)],
        [
            (10.01, 100),
            (10.02, 5000),
            (10.05, 200),
        ],
    )
    price, size, dist_bps = compute_ask_wall(s, risk_band_bps=50.0)
    assert price == 10.02
    assert size == 5000
    assert dist_bps is not None and dist_bps > 0


def test_ask_wall_returns_none_outside_band() -> None:
    s = _state_with_book(
        [(10.0, 100)],
        [
            (10.01, 100),
            (10.50, 5000),  # 500bps above mid - outside default band
        ],
    )
    price, size, dist = compute_ask_wall(s, risk_band_bps=50.0)
    # Top ask 10.01 is within band; it should be returned as the only candidate.
    assert price == 10.01
    assert size == 100


def test_tape_buy_pct(t0: dt.datetime) -> None:
    s = MarketState()
    s.has_tape_subscription = True
    s.tape = TapeWindow(window_seconds=120.0)
    s.tape.push(TapeTick(ts=t0, price=10.0, size=100, side="buy", raw_type="AllLast"))
    s.tape.push(TapeTick(ts=t0 + dt.timedelta(seconds=10), price=10.0, size=300, side="sell", raw_type="AllLast"))
    s.tape.push(TapeTick(ts=t0 + dt.timedelta(seconds=20), price=10.0, size=100, side="buy", raw_type="AllLast"))
    pct = compute_tape_buy_pct(s, window_seconds=60.0)
    assert pct is not None
    # buy_vol = 200, sell_vol = 300, total = 500, buy/total = 0.4
    assert pct == pytest.approx(0.4)


def test_tape_speed(t0: dt.datetime) -> None:
    s = MarketState()
    s.has_tape_subscription = True
    s.tape = TapeWindow(window_seconds=120.0)
    for i in range(6):
        s.tape.push(TapeTick(ts=t0 + dt.timedelta(seconds=i * 5), price=10.0, size=100, side="buy", raw_type="AllLast"))
    sp30 = compute_tape_speed(s, window_seconds=30.0)
    assert sp30 is not None
    assert sp30 > 0


def test_snapshot_works_without_subscriptions(t0: dt.datetime) -> None:
    s = MarketState()
    snap = compute_snapshot(s, now=t0)
    assert snap.has_depth is False
    assert snap.has_tape is False
    assert snap.bid_ask_imbalance is None
    assert snap.tape_buy_pct_60s is None
