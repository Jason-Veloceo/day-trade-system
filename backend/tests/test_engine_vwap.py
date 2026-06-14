"""Tests for SessionVwap."""

from __future__ import annotations

import datetime as dt

import pytest

from day_trade.engine.vwap import SessionVwap


@pytest.fixture
def t0() -> dt.datetime:
    # 14:30 UTC = post US RTH open
    return dt.datetime(2026, 6, 15, 14, 30, tzinfo=dt.timezone.utc)


def test_no_volume_reports_na(t0: dt.datetime) -> None:
    """A bar with zero volume should still update last_close but VWAP is N/A."""
    v = SessionVwap()
    out = v.update(t0, high=1.1, low=1.0, close=1.05, volume=0.0)
    assert out is not None
    assert out.state == "na"


def test_above_below_state(t0: dt.datetime) -> None:
    v = SessionVwap()
    v.update(t0, 10.0, 9.0, 9.5, volume=1000.0)
    out2 = v.update(t0 + dt.timedelta(minutes=1), 11.0, 10.0, 10.5, volume=2000.0)
    assert out2 is not None
    # Cumulative VWAP after two bars; second bar close 10.5 is above.
    assert out2.state == "above"

    # A bar that takes price below the running VWAP should report 'below'.
    out3 = v.update(t0 + dt.timedelta(minutes=2), 10.0, 8.0, 8.5, volume=2000.0)
    assert out3 is not None
    assert out3.state in ("below", "at")
    assert out3.cum_volume == 5000.0


def test_session_reset_on_next_day(t0: dt.datetime) -> None:
    v = SessionVwap()
    v.update(t0, 10.0, 9.0, 9.5, volume=1000.0)
    # 24h later -> new anchor day -> cum resets.
    t1 = t0 + dt.timedelta(days=1)
    out = v.update(t1, 20.0, 19.0, 19.5, volume=1000.0)
    assert out is not None
    # After reset, cum_volume == this single bar's volume only.
    assert out.cum_volume == 1000.0


def test_requires_tz_aware() -> None:
    v = SessionVwap()
    with pytest.raises(ValueError):
        v.update(dt.datetime(2026, 6, 15, 14, 30), 1.0, 1.0, 1.0, volume=0.0)
