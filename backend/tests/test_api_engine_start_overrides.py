"""Unit tests for the per-engine microstructure override merge helper.

`day_trade.api.engine._apply_microstructure_overrides` is the seam
where the JSON-shaped `MicrostructureIn` block (from the Arm form) is
materialised into a `TrendGateConfig` instance and stuffed into
`strategy_params` before the engine is built. These tests pin down the
compose rules with the `require_5m_macd` toggle and the auto-arm code
path (which never sends overrides).
"""
from __future__ import annotations

import pytest

from day_trade.api.engine import (
    MicrostructureIn,
    _apply_microstructure_overrides,
)
from day_trade.engine.strategies.first_pullback_long import TrendGateConfig


def _base_params() -> dict:
    return {
        "macd_fast": 12,
        "macd_slow": 26,
        "macd_signal": 9,
        "trigger_mode": "pullback_break",
    }


def test_no_overrides_leaves_params_untouched_require_5m_true() -> None:
    """Auto-armed engines never send a microstructure block. When
    `require_5m_macd=True` (default), strategy_params should flow
    through unchanged so the strategy uses its built-in defaults."""
    params = _base_params()
    result = _apply_microstructure_overrides(
        strategy_name="first_pullback_long",
        strategy_params=params,
        require_5m_macd=True,
        overrides=None,
    )
    assert "trend" not in result
    # Original dict must not be mutated.
    assert "trend" not in params


def test_no_overrides_leaves_params_untouched_require_5m_false() -> None:
    """Even with `require_5m_macd=False`, no overrides means the API
    layer stays out of the way — the engine's own __init__ handles the
    fallback (creating a `trend` with the histogram flags disabled)."""
    params = _base_params()
    result = _apply_microstructure_overrides(
        strategy_name="first_pullback_long",
        strategy_params=params,
        require_5m_macd=False,
        overrides=None,
    )
    assert "trend" not in result


def test_other_strategy_ignores_overrides() -> None:
    """Non-first_pullback strategies do not accept a `trend` kwarg —
    the helper must be a no-op for them regardless of what was sent."""
    params = {"fast": 12, "slow": 26, "signal": 9}
    result = _apply_microstructure_overrides(
        strategy_name="macd_crossover_long",
        strategy_params=params,
        require_5m_macd=True,
        overrides=MicrostructureIn(max_spread_bps=25.0),
    )
    assert "trend" not in result


def test_spread_tier_overrides_produce_trendgateconfig() -> None:
    """A user tightening the sub-$5 spread cap from the Arm form
    should surface on the resulting `TrendGateConfig` while all other
    fields keep their strategy-default values."""
    result = _apply_microstructure_overrides(
        strategy_name="first_pullback_long",
        strategy_params=_base_params(),
        require_5m_macd=True,
        overrides=MicrostructureIn(max_spread_bps_under_5=150.0),
    )
    trend = result["trend"]
    assert isinstance(trend, TrendGateConfig)
    assert trend.max_spread_bps_under_5 == 150.0
    # Defaults preserved for everything else.
    assert trend.max_spread_bps == TrendGateConfig().max_spread_bps
    assert trend.max_spread_bps_under_20 == TrendGateConfig().max_spread_bps_under_20
    assert trend.min_bid_ask_imbalance == TrendGateConfig().min_bid_ask_imbalance
    assert trend.min_tape_buy_pct == TrendGateConfig().min_tape_buy_pct


def test_multiple_overrides_compose() -> None:
    result = _apply_microstructure_overrides(
        strategy_name="first_pullback_long",
        strategy_params=_base_params(),
        require_5m_macd=True,
        overrides=MicrostructureIn(
            max_spread_bps=30.0,
            max_spread_bps_under_5=250.0,
            min_bid_ask_imbalance=0.55,
            min_tape_buy_pct=0.60,
        ),
    )
    trend = result["trend"]
    assert isinstance(trend, TrendGateConfig)
    assert trend.max_spread_bps == 30.0
    assert trend.max_spread_bps_under_5 == 250.0
    assert trend.min_bid_ask_imbalance == 0.55
    assert trend.min_tape_buy_pct == 0.60
    # Untouched:
    assert trend.max_spread_bps_under_20 == TrendGateConfig().max_spread_bps_under_20


def test_overrides_fold_in_require_5m_macd_true() -> None:
    """When the user provides microstructure overrides AND leaves
    `require_5m_macd=True`, the composed TrendGateConfig must still
    enforce the broader-trend filter (histogram positive + not falling)."""
    result = _apply_microstructure_overrides(
        strategy_name="first_pullback_long",
        strategy_params=_base_params(),
        require_5m_macd=True,
        overrides=MicrostructureIn(max_spread_bps=25.0),
    )
    trend = result["trend"]
    assert trend.require_5m_histogram_positive is True
    assert trend.require_5m_histogram_not_falling is True


def test_overrides_fold_in_require_5m_macd_false() -> None:
    """When the user provides microstructure overrides AND turns off
    the 5m-MACD gate (fast-pivot mode), the composed TrendGateConfig
    must reflect BOTH the overrides and the disabled histogram flags —
    this is the same shape the engine's fallback would have produced
    on its own if we hadn't populated `trend` here."""
    result = _apply_microstructure_overrides(
        strategy_name="first_pullback_long",
        strategy_params=_base_params(),
        require_5m_macd=False,
        overrides=MicrostructureIn(min_bid_ask_imbalance=0.40),
    )
    trend = result["trend"]
    assert trend.require_5m_histogram_positive is False
    assert trend.require_5m_histogram_not_falling is False
    assert trend.min_bid_ask_imbalance == 0.40


def test_helper_does_not_mutate_input_dict() -> None:
    params = _base_params()
    _apply_microstructure_overrides(
        strategy_name="first_pullback_long",
        strategy_params=params,
        require_5m_macd=True,
        overrides=MicrostructureIn(max_spread_bps=25.0),
    )
    assert "trend" not in params


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_spread_bps", 0.5),          # below ge=1.0
        ("max_spread_bps", 1500.0),       # above le=1000.0
        ("min_bid_ask_imbalance", -0.1),  # below ge=0.0
        ("min_bid_ask_imbalance", 1.5),   # above le=1.0
        ("min_tape_buy_pct", -0.5),
        ("min_tape_buy_pct", 2.0),
    ],
)
def test_pydantic_rejects_out_of_range_values(field: str, value: float) -> None:
    """Range validation lives on the pydantic model, so we don't have
    to defend against nonsense values inside the helper."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        MicrostructureIn(**{field: value})
