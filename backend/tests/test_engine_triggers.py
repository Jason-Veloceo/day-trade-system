"""Unit tests for engine/triggers.py.

Covers the Ross-style micro/first-pullback breakout detector and the
MACD cross-up trigger. Each test builds an explicit sequence of bars
so the intent is obvious from the test name.
"""

from __future__ import annotations

import datetime as dt

import pytest

from day_trade.engine.strategies.base import Bar
from day_trade.engine.triggers import (
    PullbackBreakConfig,
    detect_macd_cross_up,
    detect_pullback_break,
)


# ---------- helpers ----------


def _bar(
    minute: int,
    *,
    open_: float,
    close: float,
    high: float | None = None,
    low: float | None = None,
    volume: float = 1000.0,
) -> Bar:
    """Build a 1m bar at 14:{minute} UTC. high/low default to max/min of open/close."""
    ts = dt.datetime(2026, 6, 15, 14, minute, tzinfo=dt.timezone.utc)
    h = high if high is not None else max(open_, close)
    l = low if low is not None else min(open_, close)
    return Bar(ts=ts, open=open_, high=h, low=l, close=close, volume=volume)


def _green(minute: int, open_: float, close: float, **kw) -> Bar:
    assert close > open_, "green bar requires close > open"
    return _bar(minute, open_=open_, close=close, **kw)


def _red(minute: int, open_: float, close: float, **kw) -> Bar:
    assert close < open_, "red bar requires close < open"
    return _bar(minute, open_=open_, close=close, **kw)


# ============================================================
# detect_pullback_break
# ============================================================


def test_classic_two_red_then_break_on_second_green_fires() -> None:
    """Mirrors the user's NVFY example:
      G G R R G G            (current = the second G)
    The second G's high exceeds the LAST red's high -> fires.
    """
    history = [
        _green(0, open_=3.40, close=3.55),  # impulse
        _green(1, open_=3.55, close=3.65),  # impulse
        _red(2, open_=3.65, close=3.58, high=3.66),  # bigger pullback red
        _red(3, open_=3.58, close=3.55, high=3.59),  # smaller (last) pullback red <- test high = 3.59
        _green(4, open_=3.55, close=3.58, high=3.585),  # first attempt - did NOT break 3.59
    ]
    current = _green(5, open_=3.58, close=3.62, high=3.73)  # second green, blows past 3.59

    result = detect_pullback_break(current_bar=current, history=history)
    assert result.fired is True, result.reason
    assert result.mode == "pullback_break"
    assert result.pullback_bar_count == 2
    assert result.impulse_bar_count == 2
    assert result.pullback_test_high == pytest.approx(3.59)
    # pullback low = min of pullback bar lows -> 3.55 (close of last red = low)
    assert result.pullback_low == pytest.approx(3.55)


def test_breakout_directly_after_pullback_no_intermediate_greens_fires() -> None:
    """G G R R G  (current G's high > last R's high)"""
    history = [
        _green(0, open_=2.00, close=2.10),
        _green(1, open_=2.10, close=2.20),
        _red(2, open_=2.20, close=2.15, high=2.22),
        _red(3, open_=2.15, close=2.12, high=2.16),  # test high
    ]
    current = _green(4, open_=2.12, close=2.18, high=2.19)
    result = detect_pullback_break(current_bar=current, history=history)
    assert result.fired is True, result.reason
    assert result.pullback_test_high == pytest.approx(2.16)
    assert result.pullback_bar_count == 2


def test_no_break_because_current_high_did_not_exceed_test_high() -> None:
    history = [
        _green(0, open_=2.00, close=2.10),
        _green(1, open_=2.10, close=2.20),
        _red(2, open_=2.20, close=2.18, high=2.21),
    ]
    # Green bar, but high = 2.20 which == test_high (no strict break).
    current = _green(3, open_=2.18, close=2.19, high=2.20)
    result = detect_pullback_break(current_bar=current, history=history)
    assert result.fired is False
    assert "did not exceed" in result.reason


def test_non_strict_break_allows_equality() -> None:
    history = [
        _green(0, open_=2.00, close=2.10),
        _red(1, open_=2.10, close=2.05, high=2.11),
    ]
    current = _green(2, open_=2.05, close=2.10, high=2.11)
    result = detect_pullback_break(
        current_bar=current,
        history=history,
        config=PullbackBreakConfig(strict_break=False),
    )
    assert result.fired is True, result.reason


def test_current_bar_red_does_not_fire() -> None:
    history = [
        _green(0, open_=2.00, close=2.10),
        _red(1, open_=2.10, close=2.05, high=2.11),
    ]
    current = _red(2, open_=2.05, close=2.02, high=2.06)  # red - cannot fire
    result = detect_pullback_break(current_bar=current, history=history)
    assert result.fired is False
    assert "current bar is not green" in result.reason


def test_too_many_greens_between_pullback_and_current_returns_false() -> None:
    """Pullback is too far back: 4 green bars between it and current,
    default max_bars_since_pullback_end = 3."""
    history = [
        _green(0, open_=2.00, close=2.10),
        _red(1, open_=2.10, close=2.05, high=2.11),
        _green(2, open_=2.05, close=2.07),
        _green(3, open_=2.07, close=2.08),
        _green(4, open_=2.08, close=2.09),
        _green(5, open_=2.09, close=2.10),
    ]
    current = _green(6, open_=2.10, close=2.20, high=2.25)
    result = detect_pullback_break(current_bar=current, history=history)
    assert result.fired is False
    assert "no pullback within last" in result.reason


def test_pullback_too_long_returns_false() -> None:
    """max_pullback_bars default = 3 -> 4 red bars in a row is too long."""
    history = [
        _green(0, open_=2.00, close=2.20),
        _red(1, open_=2.20, close=2.15, high=2.21),
        _red(2, open_=2.15, close=2.10, high=2.16),
        _red(3, open_=2.10, close=2.05, high=2.11),
        _red(4, open_=2.05, close=2.00, high=2.06),
    ]
    current = _green(5, open_=2.00, close=2.10, high=2.15)
    result = detect_pullback_break(current_bar=current, history=history)
    assert result.fired is False
    assert "pullback too long" in result.reason


def test_no_impulse_before_pullback_returns_false_when_required() -> None:
    """Pullback is the FIRST bars in history -> no green impulse precedes it."""
    history = [
        _red(0, open_=2.20, close=2.15, high=2.21),
        _red(1, open_=2.15, close=2.10, high=2.16),
    ]
    current = _green(2, open_=2.10, close=2.18, high=2.22)
    result = detect_pullback_break(current_bar=current, history=history)
    assert result.fired is False
    assert "insufficient green impulse" in result.reason


def test_impulse_not_required_allows_no_prior_green() -> None:
    history = [
        _red(0, open_=2.20, close=2.15, high=2.21),
        _red(1, open_=2.15, close=2.10, high=2.16),
    ]
    current = _green(2, open_=2.10, close=2.18, high=2.22)
    result = detect_pullback_break(
        current_bar=current,
        history=history,
        config=PullbackBreakConfig(require_impulse=False),
    )
    assert result.fired is True, result.reason
    assert result.impulse_bar_count == 0


def test_no_red_in_history_returns_false() -> None:
    history = [
        _green(0, open_=2.00, close=2.05),
        _green(1, open_=2.05, close=2.10),
    ]
    current = _green(2, open_=2.10, close=2.20, high=2.25)
    result = detect_pullback_break(current_bar=current, history=history)
    assert result.fired is False
    assert "no pullback within last" in result.reason or "no red bars" in result.reason


def test_empty_history_returns_false() -> None:
    current = _green(0, open_=2.00, close=2.10, high=2.15)
    result = detect_pullback_break(current_bar=current, history=[])
    assert result.fired is False


def test_pullback_low_is_min_of_all_pullback_bar_lows() -> None:
    history = [
        _green(0, open_=2.00, close=2.20),
        _red(1, open_=2.20, close=2.10, high=2.21, low=2.08),  # low here = 2.08 (min)
        _red(2, open_=2.10, close=2.09, high=2.12, low=2.09),
    ]
    current = _green(3, open_=2.09, close=2.13, high=2.15)
    result = detect_pullback_break(current_bar=current, history=history)
    assert result.fired is True, result.reason
    assert result.pullback_low == pytest.approx(2.08)
    # test high comes from the LAST red (the smaller / more recent), not the max
    assert result.pullback_test_high == pytest.approx(2.12)


# ============================================================
# detect_macd_cross_up
# ============================================================


def test_macd_cross_up_fires_when_crossing_zero() -> None:
    result = detect_macd_cross_up(histogram=0.001, histogram_prev=-0.002)
    assert result.fired is True
    assert result.crossed_up is True
    assert result.mode == "macd_cross"


def test_macd_cross_up_fires_when_positive_and_rising() -> None:
    result = detect_macd_cross_up(histogram=0.005, histogram_prev=0.003)
    assert result.fired is True
    assert result.positive_and_rising is True


def test_macd_cross_up_does_not_fire_when_falling() -> None:
    result = detect_macd_cross_up(histogram=0.003, histogram_prev=0.005)
    assert result.fired is False


def test_macd_cross_up_does_not_fire_when_negative() -> None:
    result = detect_macd_cross_up(histogram=-0.001, histogram_prev=-0.002)
    assert result.fired is False


def test_macd_cross_up_returns_false_when_not_warmed_up() -> None:
    result = detect_macd_cross_up(histogram=None, histogram_prev=None)
    assert result.fired is False
    assert "not warmed up" in result.reason
