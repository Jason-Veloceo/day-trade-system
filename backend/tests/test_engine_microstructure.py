"""Tests for FirstPullbackLong.evaluate_microstructure_gates.

Focus: the price-tiered spread threshold introduced 2026-07-01 and the
relaxed imbalance / tape-buy% defaults for Ross-style small-cap trading.
Prior to the tier work a flat 50 bps threshold blocked essentially every
small-cap alert (5c on $2.55 = 196 bps > 50 bps) even though those are
exactly the setups the system is meant to trade.
"""

from __future__ import annotations

import datetime as dt

import pytest

from day_trade.engine.features import FeatureSnapshot
from day_trade.engine.strategies.first_pullback_long import (
    FirstPullbackLong,
    TrendGateConfig,
    _max_spread_bps_for_price,
)


def _snap(
    *,
    mid: float | None = None,
    spread_bps: float | None = None,
    imbalance: float | None = None,
    tape_buy_pct: float | None = None,
    has_depth: bool = True,
    has_tape: bool = True,
    best_bid: float | None = None,
    best_ask: float | None = None,
) -> FeatureSnapshot:
    return FeatureSnapshot(
        ts=dt.datetime(2026, 7, 1, 15, 0, tzinfo=dt.UTC),
        best_bid=best_bid,
        best_ask=best_ask,
        spread=None,
        spread_bps=spread_bps,
        mid=mid,
        bid_size_top=None,
        ask_size_top=None,
        bid_ask_imbalance=imbalance,
        ask_wall_price=None,
        ask_wall_size=None,
        ask_wall_distance_bps=None,
        tape_count_60s=None,
        tape_buy_volume_60s=None,
        tape_sell_volume_60s=None,
        tape_buy_pct_60s=tape_buy_pct,
        tape_speed_30s=None,
        tape_speed_decay_pct=None,
        has_depth=has_depth,
        has_tape=has_tape,
    )


def _strategy() -> FirstPullbackLong:
    return FirstPullbackLong(trigger_mode="pullback_break")


# ---------------- _max_spread_bps_for_price tier helper ----------------


def test_max_spread_bps_tier_under_5() -> None:
    cfg = TrendGateConfig()
    assert _max_spread_bps_for_price(cfg, 2.55) == cfg.max_spread_bps_under_5


def test_max_spread_bps_tier_under_20() -> None:
    cfg = TrendGateConfig()
    assert _max_spread_bps_for_price(cfg, 12.0) == cfg.max_spread_bps_under_20


def test_max_spread_bps_tier_default_above_20() -> None:
    cfg = TrendGateConfig()
    assert _max_spread_bps_for_price(cfg, 42.0) == cfg.max_spread_bps


def test_max_spread_bps_boundary_exactly_5_uses_next_tier() -> None:
    """$5.00 is the boundary: <5 uses the under_5 tier, >=5 uses under_20."""
    cfg = TrendGateConfig()
    assert _max_spread_bps_for_price(cfg, 5.0) == cfg.max_spread_bps_under_20


def test_max_spread_bps_boundary_exactly_20_uses_default() -> None:
    cfg = TrendGateConfig()
    assert _max_spread_bps_for_price(cfg, 20.0) == cfg.max_spread_bps


def test_max_spread_bps_unknown_price_uses_tightest_tier() -> None:
    """Safety: if we don't know the price we assume the strictest
    threshold so we don't accidentally accept a wide spread."""
    cfg = TrendGateConfig()
    assert _max_spread_bps_for_price(cfg, None) == cfg.max_spread_bps
    assert _max_spread_bps_for_price(cfg, 0.0) == cfg.max_spread_bps
    assert _max_spread_bps_for_price(cfg, -1.0) == cfg.max_spread_bps


# ---------------- Real-world TC / CANF regression cases ----------------


def test_five_cent_spread_on_dollar_stock_now_passes() -> None:
    """The exact case the user hit: bid $2.52 / ask $2.57 → 196 bps.
    Under the old flat 50 bps rule this failed. Under the tiered rule
    (small-cap tier = 200 bps) it passes."""
    s = _snap(mid=2.545, spread_bps=196.0, imbalance=0.6, tape_buy_pct=0.6)
    passed, failures, notes = _strategy().evaluate_microstructure_gates(snapshot=s)
    assert passed, f"expected pass, got failures={[f.message for f in failures]}"
    assert notes["spread_bps_max"] == 200.0


def test_wide_spread_on_dollar_stock_still_fails() -> None:
    """250 bps on a $2.55 stock is roughly 6.4c — still too wide even
    for a small-cap. Confirms the tier isn't a free pass."""
    s = _snap(mid=2.55, spread_bps=250.0, imbalance=0.6, tape_buy_pct=0.6)
    passed, failures, _ = _strategy().evaluate_microstructure_gates(snapshot=s)
    assert not passed
    assert any("spread" in f.message for f in failures)


def test_five_cent_spread_on_100_stock_fails() -> None:
    """5 bps of a $100 stock is 5c — normal. But 5 bps < 50 threshold,
    passes. Let's flip: 60 bps on $100 = 60c → should fail."""
    s = _snap(mid=100.0, spread_bps=60.0, imbalance=0.6, tape_buy_pct=0.6)
    passed, failures, _ = _strategy().evaluate_microstructure_gates(snapshot=s)
    assert not passed
    assert any("spread" in f.message for f in failures)


# ---------------- mid vs best_bid/best_ask fallback ----------------


def test_ref_price_falls_back_to_best_bid_ask_midpoint() -> None:
    """When mid is None but best_bid/ask are available, we derive
    ref_price from them so the tier still works."""
    s = _snap(
        mid=None, best_bid=2.50, best_ask=2.60, spread_bps=200.0,
        imbalance=0.6, tape_buy_pct=0.6,
    )
    passed, failures, notes = _strategy().evaluate_microstructure_gates(snapshot=s)
    assert passed, f"failures={[f.message for f in failures]}"
    assert notes["spread_bps_max"] == 200.0  # under-5 tier


def test_no_price_available_uses_tightest_tier() -> None:
    """Without mid AND without best_bid/ask, we can't pick a tier —
    default to the tightest (50 bps) as a safety net. 100 bps then
    fails."""
    s = _snap(mid=None, spread_bps=100.0, imbalance=0.6, tape_buy_pct=0.6)
    passed, failures, notes = _strategy().evaluate_microstructure_gates(snapshot=s)
    assert not passed
    assert notes["spread_bps_max"] == 50.0
    assert any("spread" in f.message for f in failures)


# ---------------- Relaxed imbalance + tape defaults (0.45) ----------------


def test_imbalance_047_now_passes() -> None:
    """Before the 0.55 → 0.45 relaxation this failed. Ross-style
    aggressive mode accepts modestly bid-heavy books."""
    s = _snap(mid=2.55, spread_bps=100.0, imbalance=0.47, tape_buy_pct=0.6)
    passed, failures, _ = _strategy().evaluate_microstructure_gates(snapshot=s)
    assert passed, f"failures={[f.message for f in failures]}"


def test_imbalance_040_still_fails() -> None:
    """Clearly sell-heavy books are still rejected."""
    s = _snap(mid=2.55, spread_bps=100.0, imbalance=0.40, tape_buy_pct=0.6)
    passed, failures, _ = _strategy().evaluate_microstructure_gates(snapshot=s)
    assert not passed
    assert any("imbalance" in f.message for f in failures)


def test_tape_buy_pct_048_now_passes() -> None:
    s = _snap(mid=2.55, spread_bps=100.0, imbalance=0.6, tape_buy_pct=0.48)
    passed, failures, _ = _strategy().evaluate_microstructure_gates(snapshot=s)
    assert passed, f"failures={[f.message for f in failures]}"


def test_tape_buy_pct_040_still_fails() -> None:
    s = _snap(mid=2.55, spread_bps=100.0, imbalance=0.6, tape_buy_pct=0.40)
    passed, failures, _ = _strategy().evaluate_microstructure_gates(snapshot=s)
    assert not passed
    assert any("tape buy" in f.message for f in failures)


# ---------------- Missing-data edge cases (unchanged behaviour) ----------------


def test_no_snapshot_passes_trivially() -> None:
    passed, failures, notes = _strategy().evaluate_microstructure_gates(snapshot=None)
    assert passed and not failures
    assert notes.get("l2_ts") == "na"


def test_has_depth_but_no_spread_still_fails() -> None:
    """`has_depth=True` but `spread_bps=None` → book empty → fail (unchanged)."""
    s = _snap(mid=2.55, spread_bps=None, imbalance=0.6, tape_buy_pct=0.6)
    passed, failures, _ = _strategy().evaluate_microstructure_gates(snapshot=s)
    assert not passed
    assert any("book empty" in f.message for f in failures)
