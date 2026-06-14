"""Tests for the exit trigger framework."""

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


@pytest.fixture
def t0() -> dt.datetime:
    return dt.datetime(2026, 6, 15, 14, 30, tzinfo=dt.timezone.utc)


def _set(t0: dt.datetime, entry: float = 10.0, stop: float = 9.50) -> ExitTriggerSet:
    s = ExitTriggerSet(ExitConfig())
    s.open(entry_price=entry, stop_price=stop, entry_ts=t0, quantity=100)
    return s


def _inp(
    t0: dt.datetime,
    *,
    close: float,
    low: float | None = None,
    high: float | None = None,
    macd_prev: float | None = None,
    macd_curr: float | None = None,
    above_vwap: bool | None = True,
    snapshot: FeatureSnapshot | None = None,
) -> ExitEvaluationInputs:
    return ExitEvaluationInputs(
        ts=t0,
        close=close,
        low=low if low is not None else close,
        high=high if high is not None else close,
        macd_1m_histogram_prev=macd_prev,
        macd_1m_histogram=macd_curr,
        above_vwap=above_vwap,
        feature_snapshot=snapshot,
    )


def test_hard_stop_fires_first(t0: dt.datetime) -> None:
    s = _set(t0, entry=10.0, stop=9.50)
    # Bar low penetrates stop AND macd has crossed down - hard stop must win.
    decision = s.on_bar(
        _inp(t0, close=9.40, low=9.40, macd_prev=0.01, macd_curr=-0.01)
    )
    assert decision is not None
    assert decision.kind == ExitTriggerKind.HARD_STOP
    assert decision.fraction == 1.0


def test_second_target_full_exit(t0: dt.datetime) -> None:
    s = _set(t0, entry=10.0, stop=9.50)  # risk = 0.50, 2R target = 11.0
    decision = s.on_bar(_inp(t0, close=11.10, high=11.10))
    assert decision is not None
    assert decision.kind == ExitTriggerKind.SECOND_TARGET
    assert decision.fraction == 1.0


def test_first_target_partial_then_second(t0: dt.datetime) -> None:
    s = _set(t0, entry=10.0, stop=9.50)  # 1R = 10.50, 2R = 11.0
    d1 = s.on_bar(_inp(t0, close=10.55, high=10.55))
    assert d1 is not None
    assert d1.kind == ExitTriggerKind.FIRST_TARGET
    assert d1.fraction == 0.5
    # Second target on next bar.
    d2 = s.on_bar(_inp(t0 + dt.timedelta(minutes=1), close=11.10, high=11.10))
    assert d2 is not None
    assert d2.kind == ExitTriggerKind.SECOND_TARGET


def test_macd_flip(t0: dt.datetime) -> None:
    s = _set(t0)
    decision = s.on_bar(_inp(t0, close=10.20, macd_prev=0.05, macd_curr=-0.01))
    assert decision is not None
    assert decision.kind == ExitTriggerKind.MACD_FLIP


def test_vwap_loss_requires_consecutive_bars(t0: dt.datetime) -> None:
    cfg = ExitConfig(vwap_loss_bars_after_entry=2)
    s = ExitTriggerSet(cfg)
    s.open(entry_price=10.0, stop_price=9.5, entry_ts=t0, quantity=100)

    # First below-vwap bar should NOT trigger.
    d1 = s.on_bar(_inp(t0, close=9.90, above_vwap=False))
    assert d1 is None
    d2 = s.on_bar(_inp(t0 + dt.timedelta(minutes=1), close=9.89, above_vwap=False))
    assert d2 is not None
    assert d2.kind == ExitTriggerKind.VWAP_LOSS


def test_l2_distress_imbalance(t0: dt.datetime) -> None:
    s = _set(t0)
    snap = FeatureSnapshot(
        ts=t0,
        best_bid=10.0, best_ask=10.05, spread=0.05, spread_bps=50.0, mid=10.025,
        bid_size_top=100.0, ask_size_top=100.0,
        bid_ask_imbalance=0.20,  # sellers dominant
        ask_wall_price=None, ask_wall_size=None, ask_wall_distance_bps=None,
        tape_count_60s=None, tape_buy_volume_60s=None, tape_sell_volume_60s=None,
        tape_buy_pct_60s=None, tape_speed_30s=None, tape_speed_decay_pct=None,
        has_depth=True, has_tape=False,
    )
    d = s.on_bar(_inp(t0, close=10.0, snapshot=snap))
    assert d is not None
    assert d.kind == ExitTriggerKind.L2_DISTRESS


def test_l2_distress_ask_wall(t0: dt.datetime) -> None:
    s = _set(t0)
    snap = FeatureSnapshot(
        ts=t0,
        best_bid=10.0, best_ask=10.01, spread=0.01, spread_bps=10.0, mid=10.005,
        bid_size_top=100.0, ask_size_top=100.0,
        bid_ask_imbalance=0.60,
        ask_wall_price=10.02, ask_wall_size=1000.0,  # 10x top-of-book
        ask_wall_distance_bps=15.0,  # within 20bps band
        tape_count_60s=None, tape_buy_volume_60s=None, tape_sell_volume_60s=None,
        tape_buy_pct_60s=None, tape_speed_30s=None, tape_speed_decay_pct=None,
        has_depth=True, has_tape=False,
    )
    d = s.on_bar(_inp(t0, close=10.0, snapshot=snap))
    assert d is not None
    assert d.kind == ExitTriggerKind.L2_DISTRESS


def test_tape_flip_needs_consecutive(t0: dt.datetime) -> None:
    cfg = ExitConfig(tape_flip_bars_required=2)
    s = ExitTriggerSet(cfg)
    s.open(entry_price=10.0, stop_price=9.5, entry_ts=t0, quantity=100)
    bad = FeatureSnapshot(
        ts=t0, best_bid=10.0, best_ask=10.01, spread=0.01, spread_bps=10.0, mid=10.005,
        bid_size_top=None, ask_size_top=None,
        bid_ask_imbalance=None,
        ask_wall_price=None, ask_wall_size=None, ask_wall_distance_bps=None,
        tape_count_60s=10, tape_buy_volume_60s=10.0, tape_sell_volume_60s=30.0,
        tape_buy_pct_60s=0.30,
        tape_speed_30s=1.0, tape_speed_decay_pct=-0.40,
        has_depth=False, has_tape=True,
    )
    d1 = s.on_bar(_inp(t0, close=10.0, snapshot=bad))
    assert d1 is None
    d2 = s.on_bar(_inp(t0 + dt.timedelta(minutes=1), close=10.0, snapshot=bad))
    assert d2 is not None
    assert d2.kind == ExitTriggerKind.TAPE_FLIP


def test_time_stop(t0: dt.datetime) -> None:
    cfg = ExitConfig(time_stop_bars_max=3, time_stop_progress_cents=5.0)
    s = ExitTriggerSet(cfg)
    s.open(entry_price=10.0, stop_price=9.5, entry_ts=t0, quantity=100)
    for i in range(2):
        d = s.on_bar(_inp(t0 + dt.timedelta(minutes=i), close=10.01))
        assert d is None
    d = s.on_bar(_inp(t0 + dt.timedelta(minutes=2), close=10.02))
    assert d is not None
    assert d.kind == ExitTriggerKind.TIME_STOP


def test_open_rejects_invalid(t0: dt.datetime) -> None:
    s = ExitTriggerSet(ExitConfig())
    with pytest.raises(ValueError):
        s.open(entry_price=10.0, stop_price=10.0, entry_ts=t0, quantity=100)
    with pytest.raises(ValueError):
        s.open(entry_price=10.0, stop_price=10.50, entry_ts=t0, quantity=100)
