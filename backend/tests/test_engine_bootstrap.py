"""Tests for the bootstrap-replay → live-session handover.

The engine replays ~2 trading days of 1m bars into the strategy before the
live feed starts. That keeps MACDs warm but also pollutes intraday latches
(`macd_1m_has_crossed_down_today`, optimistic `_in_position`, etc.).
`Strategy.finalize_bootstrap` is the clean-handover point — these tests
pin its semantics.

The `_compute_session_levels` helper that derives PMHOD/PDHOD from the
bootstrap bar list is tested here too because it's coupled to the
handover sequence.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from day_trade.engine.engine import _compute_session_levels
from day_trade.engine.strategies.base import Bar
from day_trade.engine.strategies.first_pullback_long import (
    FirstPullbackLong,
    GateFailureCategory,
)

ET = ZoneInfo("America/New_York")


def _bar_et(date: dt.date, hour: int, minute: int, *, high: float, low: float | None = None) -> Bar:
    """Build a closed 1m Bar whose close timestamp is `hour:minute` ET on
    `date`. The close timestamp gets converted to UTC since that's what
    the engine carries internally."""
    ts_et = dt.datetime(date.year, date.month, date.day, hour, minute, tzinfo=ET)
    ts_utc = ts_et.astimezone(dt.timezone.utc)
    return Bar(
        ts=ts_utc,
        open=high - 0.05,
        high=high,
        low=low if low is not None else high - 0.10,
        close=high - 0.02,
        volume=1000.0,
    )


# ----------------------- PMHOD / PDHOD computation -----------------------


def test_compute_session_levels_empty() -> None:
    pm, pd = _compute_session_levels([])
    assert pm is None and pd is None


def test_compute_session_levels_premarket_only() -> None:
    today = dt.date(2026, 6, 30)
    bars = [
        _bar_et(today, 5, 30, high=3.10),
        _bar_et(today, 6, 15, high=3.40),   # PMHOD
        _bar_et(today, 7, 0, high=3.20),
        _bar_et(today, 9, 30, high=3.35),   # last premarket bar (close == open)
    ]
    pm, pd = _compute_session_levels(bars)
    assert pm == 3.40
    assert pd is None


def test_compute_session_levels_prior_session_rth_only() -> None:
    today = dt.date(2026, 6, 30)
    prior = dt.date(2026, 6, 29)
    bars = [
        _bar_et(prior, 10, 0, high=2.80),    # RTH
        _bar_et(prior, 13, 45, high=3.55),   # PDHOD (prior RTH high)
        _bar_et(prior, 16, 0, high=3.30),    # last RTH bar
        _bar_et(today, 5, 0, high=3.10),     # today premarket
    ]
    pm, pd = _compute_session_levels(bars)
    assert pm == 3.10
    assert pd == 3.55


def test_compute_session_levels_picks_most_recent_prior_session() -> None:
    today = dt.date(2026, 6, 30)
    two_days_ago = dt.date(2026, 6, 26)
    one_day_ago = dt.date(2026, 6, 29)
    bars = [
        _bar_et(two_days_ago, 13, 0, high=9.99),   # NOT PDHOD - older session
        _bar_et(one_day_ago, 13, 0, high=4.20),    # PDHOD
        _bar_et(today, 6, 0, high=3.10),
    ]
    pm, pd = _compute_session_levels(bars)
    assert pm == 3.10
    assert pd == 4.20


def test_compute_session_levels_excludes_premarket_from_pdhod() -> None:
    today = dt.date(2026, 6, 30)
    prior = dt.date(2026, 6, 29)
    bars = [
        _bar_et(prior, 7, 0, high=99.99),    # premarket on prior day - NOT PDHOD
        _bar_et(prior, 10, 0, high=4.10),    # RTH on prior day - this IS PDHOD
        _bar_et(today, 6, 0, high=3.10),
    ]
    pm, pd = _compute_session_levels(bars)
    assert pm == 3.10
    assert pd == 4.10


def test_compute_session_levels_boundary_bar_at_open() -> None:
    """A bar with close ts at exactly 09:30 ET covers [09:29, 09:30) which
    is premarket. The next bar (close 09:31) is the first RTH bar.
    """
    today = dt.date(2026, 6, 30)
    bars = [
        _bar_et(today, 9, 30, high=3.50),   # premarket boundary
        _bar_et(today, 9, 31, high=3.60),   # first RTH bar (NOT premarket)
    ]
    pm, _ = _compute_session_levels(bars)
    assert pm == 3.50


# --------------------- FirstPullbackLong.finalize_bootstrap ---------------------


def test_finalize_bootstrap_clears_macd_cross_down_latch() -> None:
    """After a replay sequence that produces a +→- 1m MACD crossdown, the
    latch should be reset by finalize_bootstrap so the live session
    starts clean."""
    s = FirstPullbackLong()
    s.backside_state.macd_1m_has_crossed_down_today = True
    s.backside_state.macd_1m_has_crossed_up_today = True
    s.backside_state.bars_below_vwap_consecutive = 4
    s.backside_state.failed_setups_today = 2
    s.backside_state.last_new_hod_bar_idx = 99
    s.backside_state.bars_processed_today = 1074
    s.backside_state.highs_history = [1.0, 2.0, 3.0]
    s._high_of_day = 5.55
    s._in_position = True
    s._last_pullback_low = 3.20
    s._last_pullback_test_high = 3.80

    s.finalize_bootstrap(pmhod=4.10, pdhod=3.85)

    assert s.backside_state.macd_1m_has_crossed_down_today is False
    assert s.backside_state.macd_1m_has_crossed_up_today is False
    assert s.backside_state.bars_below_vwap_consecutive == 0
    assert s.backside_state.failed_setups_today == 0
    assert s.backside_state.last_new_hod_bar_idx is None
    assert s.backside_state.bars_processed_today == 0
    assert s.backside_state.highs_history == []
    assert s._high_of_day is None
    assert s._in_position is False
    assert s._last_pullback_low is None
    assert s._last_pullback_test_high is None


def test_finalize_bootstrap_seeds_reference_levels() -> None:
    s = FirstPullbackLong()
    s.finalize_bootstrap(pmhod=4.10, pdhod=3.85)
    assert s.backside_state.pmhod == 4.10
    assert s.backside_state.pdhod == 3.85
    snap = s.snapshot()
    assert snap["pmhod"] == 4.10
    assert snap["pdhod"] == 3.85


def test_finalize_bootstrap_preserves_warm_indicators() -> None:
    """The MACD EMA state on `_fast`/`_slow`/`_signal` and the prev-hist
    trackers must survive the handover so cross-detection works on the
    first live bar."""
    s = FirstPullbackLong()
    # Drive 35 bars through to fully warm the 1m MACD.
    base = dt.datetime(2026, 6, 30, 11, 0, tzinfo=dt.timezone.utc)
    for i in range(60):
        bar = Bar(
            ts=base + dt.timedelta(minutes=i),
            open=10.0 + i * 0.01,
            high=10.05 + i * 0.01,
            low=9.95 + i * 0.01,
            close=10.02 + i * 0.01,
            volume=1000.0,
        )
        s.on_bar(bar)
    pre_macd_hist = s._macd_1m_last.histogram if s._macd_1m_last else None
    pre_prev_hist = s._macd_1m_prev_hist

    s.finalize_bootstrap(pmhod=None, pdhod=None)

    assert s._macd_1m_last is not None
    assert s._macd_1m_last.histogram == pre_macd_hist  # untouched
    assert s._macd_1m_prev_hist == pre_prev_hist


def test_finalize_bootstrap_clears_in_position_latch_from_replay() -> None:
    """A signal emitted during the replay optimistically latches
    `_in_position`. The engine discards the signal but the latch is the
    strategy's; without an explicit clear it stays True forever and
    blocks every live entry. This is the bug `finalize_bootstrap`
    primarily exists to fix."""
    s = FirstPullbackLong()
    s._in_position = True  # simulated optimistic latch from a replayed ENTER
    s.finalize_bootstrap(pmhod=None, pdhod=None)
    assert s._in_position is False


# --------------------- tagged-failure shape ---------------------


def test_snapshot_failures_are_tagged_dicts() -> None:
    """A gate failure must surface as {category, message} so the UI can
    group by category — not as a raw string."""
    s = FirstPullbackLong()
    # Push one bar so on_bar runs once and produces a gate result.
    bar = Bar(
        ts=dt.datetime(2026, 6, 30, 13, 0, tzinfo=dt.timezone.utc),
        open=2.0, high=2.05, low=1.99, close=2.01, volume=100.0,
    )
    s.on_bar(bar)
    snap = s.snapshot()
    failures = snap["last_entry_gate"]["failures"]
    assert isinstance(failures, list) and failures, "expected at least one failure on the first bar"
    for f in failures:
        assert isinstance(f, dict)
        assert set(f.keys()) >= {"category", "message"}
        assert f["category"] in {c.value for c in GateFailureCategory}
