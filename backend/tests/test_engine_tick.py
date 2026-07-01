"""Tests for the sub-bar (10s) evaluation tick.

The tick path is read-only on indicator/latch state — those mutations
belong exclusively to `on_bar`. The tick is allowed to fire entries
and exits, optimistically latch `_in_position`, and update the
informational `_last_*` snapshot fields.

The exit-side tick path covers only the price-driven + L2 triggers
(stop, targets, l2_distress). MACD flip / VWAP loss / tape flip /
time stop stay bar-close because their "N consecutive bars" semantics
would break under a 6x faster cadence.
"""

from __future__ import annotations

import datetime as dt

import pytest

from day_trade.engine.exits import (
    ExitConfig,
    ExitEvaluationInputs,
    ExitTriggerKind,
    ExitTriggerSet,
)
from day_trade.engine.features import FeatureSnapshot
from day_trade.engine.strategies.base import Bar
from day_trade.engine.strategies.first_pullback_long import (
    FirstPullbackLong,
    TrendGateConfig,
)
from day_trade.engine.triggers import PullbackBreakConfig


# ----------------------- shared fixtures -----------------------


def _bar(
    ts: dt.datetime,
    *,
    o: float,
    h: float,
    l: float,
    c: float,
    v: float = 1000.0,
) -> Bar:
    return Bar(ts=ts, open=o, high=h, low=l, close=c, volume=v)


_SESSION_OPEN = dt.datetime(2026, 6, 30, 13, 30, tzinfo=dt.timezone.utc)


def _t(minute: int) -> dt.datetime:
    """Return a UTC timestamp `minute` minutes past the US RTH open."""
    return _SESSION_OPEN + dt.timedelta(minutes=minute)


def _warmed_strategy() -> FirstPullbackLong:
    """Build a FirstPullbackLong whose MACD/VWAP are warmed by 60 calm
    1m bars and whose recent-bars buffer contains a clean Ross-style
    impulse + 2-red pullback. The very next bar (live or tick) sits
    in a state where ONLY the breakout-price condition is missing.

    Layout of the last few closed bars in the recent buffer:
      ... 8 green impulse bars climbing ...
      RED:  low=10.55  high=10.62   (pullback bar 1)
      RED:  low=10.50  high=10.58   (pullback bar 2, the "test high" = 10.58)
    """
    s = FirstPullbackLong(
        trend=TrendGateConfig(
            require_5m_histogram_positive=False,
            require_5m_histogram_not_falling=False,
            require_above_vwap=False,
        ),
        pullback=PullbackBreakConfig(
            min_pullback_bars=1,
            max_pullback_bars=3,
            max_bars_since_pullback_end=3,
            require_impulse=True,
            min_impulse_bars=1,
            strict_break=True,
        ),
    )
    # 50 warmup bars at flat price so MACD warms but stays small/positive.
    for i in range(50):
        s.on_bar(_bar(_t(i), o=10.0, h=10.05, l=9.95, c=10.02))
    # 8 green impulse bars climbing.
    base = 10.05
    for i in range(8):
        ts = _t(50 + i)
        o = base + i * 0.08
        c = o + 0.07
        s.on_bar(_bar(ts, o=o, h=c + 0.02, l=o - 0.02, c=c))
    # 2 red pullback bars (the second one — the "smaller" / more recent
    # red — is the test bar).
    s.on_bar(_bar(_t(58), o=10.78, h=10.79, l=10.55, c=10.62))   # red 1
    s.on_bar(_bar(_t(59), o=10.62, h=10.58, l=10.50, c=10.52))   # red 2 (test high = 10.58)
    return s


# ----------------------- strategy on_tick -----------------------


def test_on_tick_fires_mid_candle_when_partial_high_breaks_pullback() -> None:
    s = _warmed_strategy()
    # Partial bar opens green and the running high pokes above 10.58.
    partial = _bar(_t(60), o=10.55, h=10.62, l=10.54, c=10.61)
    sig = s.on_tick(partial)
    assert sig is not None, "expected mid-candle entry signal"
    assert sig.kind.value == "enter_long"
    assert sig.extras and sig.extras.get("is_mid_candle") is True
    # The trigger should report the actual test high from the most
    # recent red pullback bar.
    trig_meta = sig.extras["trigger"]
    assert trig_meta["pullback_test_high"] == pytest.approx(10.58)
    assert trig_meta["pullback_low"] == pytest.approx(10.50)
    # In-position latch should be set so a second tick on the same
    # candle doesn't fire again.
    assert s._in_position is True


def test_on_tick_does_not_fire_when_partial_high_below_pullback() -> None:
    s = _warmed_strategy()
    partial = _bar(_t(60), o=10.55, h=10.57, l=10.54, c=10.56)
    sig = s.on_tick(partial)
    assert sig is None
    assert s._in_position is False


def test_on_tick_no_double_fire_after_in_position() -> None:
    s = _warmed_strategy()
    breakout = _bar(_t(60), o=10.55, h=10.62, l=10.54, c=10.61)
    first = s.on_tick(breakout)
    assert first is not None
    # Second tick within the same partial bar — should be silent.
    again = s.on_tick(_bar(_t(60), o=10.55, h=10.70, l=10.54, c=10.69))
    assert again is None


def test_on_tick_is_readonly_on_indicator_state() -> None:
    """Strategy state that is supposed to advance only on closed bars
    must NOT be touched by on_tick. This is the contract that keeps the
    bar-close cadence semantically valid."""
    s = _warmed_strategy()
    pre_macd_1m_hist = s._macd_1m_last.histogram if s._macd_1m_last else None
    pre_recent_count = len(s._recent_1m_bars)
    pre_bars_processed = s.backside_state.bars_processed_today
    pre_highs_history = list(s.backside_state.highs_history)
    pre_crossed_down = s.backside_state.macd_1m_has_crossed_down_today

    breakout = _bar(_t(60), o=10.55, h=10.62, l=10.54, c=10.61)
    s.on_tick(breakout)

    assert s._macd_1m_last is not None
    assert s._macd_1m_last.histogram == pre_macd_1m_hist
    assert len(s._recent_1m_bars) == pre_recent_count
    assert s.backside_state.bars_processed_today == pre_bars_processed
    assert s.backside_state.highs_history == pre_highs_history
    assert s.backside_state.macd_1m_has_crossed_down_today == pre_crossed_down


# ----------------------- exits on_tick -----------------------


@pytest.fixture
def t0() -> dt.datetime:
    return dt.datetime(2026, 6, 30, 13, 30, tzinfo=dt.timezone.utc)


def _exitset(t0: dt.datetime, *, entry: float = 10.0, stop: float = 9.50) -> ExitTriggerSet:
    s = ExitTriggerSet(ExitConfig())
    s.open(entry_price=entry, stop_price=stop, entry_ts=t0, quantity=100)
    return s


def _inp_tick(
    t0: dt.datetime,
    *,
    close: float,
    low: float | None = None,
    high: float | None = None,
    snapshot: FeatureSnapshot | None = None,
) -> ExitEvaluationInputs:
    return ExitEvaluationInputs(
        ts=t0,
        close=close,
        low=low if low is not None else close,
        high=high if high is not None else close,
        macd_1m_histogram_prev=None,
        macd_1m_histogram=None,
        above_vwap=True,
        feature_snapshot=snapshot,
    )


def test_on_tick_hard_stop_fires_on_partial_low(t0: dt.datetime) -> None:
    s = _exitset(t0, entry=10.0, stop=9.50)
    d = s.on_tick(_inp_tick(t0, close=9.60, low=9.49))
    assert d is not None
    assert d.kind == ExitTriggerKind.HARD_STOP
    assert d.fraction == 1.0


def test_on_tick_first_target_partial_scale(t0: dt.datetime) -> None:
    s = _exitset(t0, entry=10.0, stop=9.50)  # risk = 0.50, 1R = 10.50
    d = s.on_tick(_inp_tick(t0, close=10.45, high=10.51))
    assert d is not None
    assert d.kind == ExitTriggerKind.FIRST_TARGET
    assert d.fraction == 0.5
    # And the latch is set so it doesn't fire again on the next tick.
    again = s.on_tick(_inp_tick(t0, close=10.55, high=10.55))
    assert again is None or again.kind != ExitTriggerKind.FIRST_TARGET


def test_on_tick_second_target_full_exit(t0: dt.datetime) -> None:
    s = _exitset(t0, entry=10.0, stop=9.50)  # 2R = 11.00
    d = s.on_tick(_inp_tick(t0, close=10.95, high=11.01))
    assert d is not None
    assert d.kind == ExitTriggerKind.SECOND_TARGET
    assert d.fraction == 1.0


def test_on_tick_l2_distress_imbalance(t0: dt.datetime) -> None:
    s = _exitset(t0, entry=10.0, stop=9.50)
    snap = FeatureSnapshot(
        ts=t0,
        best_bid=10.20, best_ask=10.22, spread=0.02, spread_bps=20.0,
        mid=10.21, bid_size_top=100.0, ask_size_top=2000.0,
        bid_ask_imbalance=0.05,  # heavily seller-dominant
        ask_wall_price=None, ask_wall_size=None, ask_wall_distance_bps=None,
        tape_count_60s=None, tape_buy_volume_60s=None, tape_sell_volume_60s=None,
        tape_buy_pct_60s=None, tape_speed_30s=None, tape_speed_decay_pct=None,
        has_depth=True, has_tape=False,
    )
    d = s.on_tick(_inp_tick(t0, close=10.20, snapshot=snap))
    assert d is not None
    assert d.kind == ExitTriggerKind.L2_DISTRESS


def test_on_tick_does_not_bump_consecutive_counters(t0: dt.datetime) -> None:
    """Critical invariant: the tick path must not advance the
    `bars_since_entry` / `bars_below_vwap_since_entry` /
    `consecutive_tape_flip_bars` counters that drive the
    "N consecutive bars" semantics of vwap_loss / tape_flip / time_stop.
    If it did, those triggers would fire 6x too soon when 10s tick is
    enabled."""
    s = _exitset(t0, entry=10.0, stop=9.50)
    assert s.state is not None
    pre = (
        s.state.bars_since_entry,
        s.state.bars_below_vwap_since_entry,
        s.state.consecutive_tape_flip_bars,
    )
    # Several ticks in a row — none should fire targets or stops, and
    # importantly none should bump the counters.
    for _ in range(10):
        s.on_tick(_inp_tick(t0, close=10.10, high=10.10, low=10.00))
    post = (
        s.state.bars_since_entry,
        s.state.bars_below_vwap_since_entry,
        s.state.consecutive_tape_flip_bars,
    )
    assert pre == post


def test_on_tick_skips_macd_vwap_tape_time(t0: dt.datetime) -> None:
    """Even when the inputs would fire macd_flip / vwap_loss /
    tape_flip / time_stop on the BAR-CLOSE path, the tick path must
    stay silent on them."""
    s = _exitset(t0, entry=10.0, stop=9.50)
    snap = FeatureSnapshot(
        ts=t0,
        best_bid=10.00, best_ask=10.02, spread=0.02, spread_bps=20.0,
        mid=10.01, bid_size_top=500.0, ask_size_top=500.0,
        bid_ask_imbalance=0.50,  # neutral - no L2 distress
        ask_wall_price=None, ask_wall_size=None, ask_wall_distance_bps=None,
        tape_count_60s=20, tape_buy_volume_60s=100.0, tape_sell_volume_60s=400.0,
        tape_buy_pct_60s=0.20,           # very seller-heavy tape
        tape_speed_30s=None, tape_speed_decay_pct=-0.80,  # massive speed decay
        has_depth=True, has_tape=True,
    )
    inp = ExitEvaluationInputs(
        ts=t0,
        close=10.00,
        low=9.99,
        high=10.05,
        macd_1m_histogram_prev=0.05,
        macd_1m_histogram=-0.05,        # massive MACD flip
        above_vwap=False,               # below VWAP
        feature_snapshot=snap,
    )
    # No exit because hard_stop / targets aren't met and L2 imbalance
    # is neutral. tape_flip + macd_flip + vwap_loss are NOT checked on
    # the tick path.
    assert s.on_tick(inp) is None
