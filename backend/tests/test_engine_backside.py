"""Tests for the hybrid backside gate."""

from __future__ import annotations

import datetime as dt

import pytest

from day_trade.engine.backside import BacksideConfig, BacksideGate, BacksideInputs


@pytest.fixture
def t0() -> dt.datetime:
    return dt.datetime(2026, 6, 15, 14, 35, tzinfo=dt.timezone.utc)


def _inputs(
    t0: dt.datetime,
    *,
    macd_5m: float | None = 0.01,
    macd_5m_prev: float | None = 0.005,
    crossed_down: bool = False,
    bars_below_vwap: int = 0,
    post_1030: bool = False,
    bars_since_hod: int | None = 0,
    lower_highs: int = 0,
    tape_buy_pct: float | None = None,
    speed_decay: float | None = None,
    failed_setups: int = 0,
) -> BacksideInputs:
    return BacksideInputs(
        now=t0,
        macd_5m_histogram=macd_5m,
        macd_5m_histogram_prev=macd_5m_prev,
        macd_1m_has_crossed_down_today=crossed_down,
        bars_below_vwap_consecutive=bars_below_vwap,
        is_post_1030_et=post_1030,
        bars_since_last_new_hod=bars_since_hod,
        lower_highs_count=lower_highs,
        tape_buy_pct_60s=tape_buy_pct,
        tape_speed_decay_pct=speed_decay,
        failed_setups_today=failed_setups,
        volume_decay_pct=None,
    )


def test_passes_when_everything_is_clean(t0: dt.datetime) -> None:
    gate = BacksideGate(BacksideConfig())
    decision = gate.evaluate(_inputs(t0))
    assert not decision.block
    assert not decision.hard_vetoes


def test_hard_veto_5m_macd_negative_falling(t0: dt.datetime) -> None:
    gate = BacksideGate(BacksideConfig())
    decision = gate.evaluate(_inputs(t0, macd_5m=-0.02, macd_5m_prev=-0.01))
    assert decision.block
    assert any("5m MACD" in v for v in decision.hard_vetoes)


def test_hard_veto_1m_already_crossed_down(t0: dt.datetime) -> None:
    gate = BacksideGate(BacksideConfig())
    decision = gate.evaluate(_inputs(t0, crossed_down=True))
    assert decision.block


def test_hard_veto_vwap_loss(t0: dt.datetime) -> None:
    gate = BacksideGate(BacksideConfig(vwap_loss_bars_required=3))
    decision = gate.evaluate(_inputs(t0, bars_below_vwap=3))
    assert decision.block


def test_hard_veto_post_1030_no_new_hod(t0: dt.datetime) -> None:
    cfg = BacksideConfig(late_day_grace_bars=5)
    gate = BacksideGate(cfg)
    decision = gate.evaluate(_inputs(t0, post_1030=True, bars_since_hod=10))
    assert decision.block


def test_soft_score_blocks_when_threshold_exceeded(t0: dt.datetime) -> None:
    # Threshold 60: trigger via tape buy% well below floor + many failed setups.
    cfg = BacksideConfig(score_block_threshold=40.0)
    gate = BacksideGate(cfg)
    decision = gate.evaluate(
        _inputs(t0, tape_buy_pct=0.35, speed_decay=-0.6, failed_setups=5, lower_highs=6)
    )
    assert decision.block
    assert decision.score >= cfg.score_block_threshold


def test_soft_score_below_threshold_passes(t0: dt.datetime) -> None:
    cfg = BacksideConfig(score_block_threshold=60.0)
    gate = BacksideGate(cfg)
    decision = gate.evaluate(_inputs(t0, lower_highs=1, failed_setups=0))
    assert not decision.block


def test_breakdown_has_all_components(t0: dt.datetime) -> None:
    gate = BacksideGate(BacksideConfig())
    decision = gate.evaluate(_inputs(t0))
    assert set(decision.score_breakdown.keys()) == {
        "lower_highs",
        "tape_buy_pct",
        "tape_speed_decay",
        "failed_setups",
        "volume_decay",
    }
