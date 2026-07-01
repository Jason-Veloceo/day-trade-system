"""Backside-of-move detection.

Ross's "don't trade the backside" rule encoded as a hybrid hard-veto + soft-
score gate. Designed to be called BEFORE the entry gates run, so we never
even evaluate the rest of the stack if we're clearly past the run.

Hard vetoes (any one = block entry, ignore the score):
  - 5-minute MACD histogram is negative AND falling
  - 1-minute MACD has already crossed-down at least once today after a
    previous cross-up (the "I already missed the move" tell)
  - Price has lost VWAP for `vwap_loss_bars` consecutive 1m bars
  - Active halt (TODO: wire from IBKR's contract status; placeholder for v1)
  - Post 10:30 ET with no fresh new-high-of-day in the last `late_day_grace_bars`

Soft signals (each adds to a 0..100 score; >= threshold = block):
  - Lower-highs count over the last K 1m bars
  - Tape buy% has fallen below threshold and falling
  - Tape speed decay (last 30s < last 60s by margin)
  - Failed-setup count today (we tried this setup, it failed) - tracked
    externally by the engine and passed in
  - Volume decay vs the first 30 min of session

The gate is configured by a `BacksideConfig` dataclass and every threshold
is exposed in the snapshot so the UI can show the live values vs limits.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class BacksideConfig:
    # hard vetoes
    veto_5m_macd_negative_falling: bool = True
    vwap_loss_bars_required: int = 3
    veto_post_1030_no_new_hod: bool = True
    late_day_grace_bars: int = 5            # bars since last new HOD before we late-day veto

    # soft scoring weights (sum to ~100 to make threshold intuitive)
    weight_lower_highs: float = 25.0        # max contribution from lower-highs count
    weight_tape_buy_pct: float = 25.0       # max contribution when buy% below floor
    weight_tape_speed_decay: float = 20.0   # max contribution when speed decay below floor
    weight_failed_setups: float = 20.0      # max contribution when N failed attempts today
    weight_volume_decay: float = 10.0       # max contribution when volume decay below floor

    # soft scoring thresholds
    lower_highs_lookback_bars: int = 6
    lower_highs_max_acceptable: int = 2     # 0..2 acceptable, 3+ adds full weight
    tape_buy_pct_floor: float = 0.55        # below this starts scoring
    tape_buy_pct_zero_at: float = 0.40      # at-or-below this gives full weight
    tape_speed_decay_floor: float = -0.20   # below this starts scoring (-20% slowdown)
    tape_speed_decay_zero_at: float = -0.50 # at-or-below this gives full weight
    failed_setups_zero_at: int = 3          # 3+ failed attempts today = full weight

    # threshold to actually block
    score_block_threshold: float = 60.0     # 0..100, default 60 = "leaning bag"


@dataclass(frozen=True, slots=True)
class BacksideDecision:
    """Result of one evaluation."""

    block: bool
    hard_vetoes: tuple[str, ...]
    score: float
    score_breakdown: dict[str, float]
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "block": self.block,
            "hard_vetoes": list(self.hard_vetoes),
            "score": self.score,
            "score_breakdown": dict(self.score_breakdown),
            "reasons": list(self.reasons),
        }


@dataclass(slots=True)
class BacksideInputs:
    """All the inputs the gate needs to evaluate one bar.

    Caller (the strategy) is responsible for assembling these from its own
    state. Keeping them in a single struct keeps the gate testable in
    isolation - feed it synthetic inputs in tests and assert the decision.
    """

    now: dt.datetime                                          # bar close ts (UTC)
    macd_5m_histogram: float | None                           # current 5m hist
    macd_5m_histogram_prev: float | None                      # previous 5m hist
    macd_1m_has_crossed_down_today: bool                      # latch
    bars_below_vwap_consecutive: int                          # 0 if currently above
    is_post_1030_et: bool                                     # caller derives in their TZ
    bars_since_last_new_hod: int | None                       # None = haven't seen a HOD yet today
    lower_highs_count: int                                    # last K 1m bars: how many were lower-high
    tape_buy_pct_60s: float | None                            # None when no tape subscription
    tape_speed_decay_pct: float | None                        # speed_30 - speed_60 normalised; None ok
    failed_setups_today: int                                  # engine-tracked
    volume_decay_pct: float | None                            # last-30-min vs first-30-min; None ok


@dataclass(slots=True)
class BacksideState:
    """Mutable per-run state the gate uses. Engine updates fields as it
    learns from the bar stream. Kept on the strategy.

    Reference levels (`pmhod` / `pdhod`) are carried separately from the
    intraday latches because they originate from the bootstrap replay and
    must survive `reset_for_new_session()` — they describe context the
    live session inherits, not state the live session generates.
    """

    macd_1m_has_crossed_up_today: bool = False
    macd_1m_has_crossed_down_today: bool = False
    bars_below_vwap_consecutive: int = 0
    last_new_hod_bar_idx: int | None = None
    bars_processed_today: int = 0
    highs_history: list[float] = field(default_factory=list)
    failed_setups_today: int = 0
    first_30m_volume: float = 0.0
    last_30m_volume: float = 0.0

    # Reference levels carried from the bootstrap replay. These are NOT
    # cleared by `reset_for_new_session()`; the engine seeds them once at
    # bootstrap and they're effectively read-only for the live session.
    #   pmhod: today's premarket high (04:00 - 09:30 ET).
    #   pdhod: previous-session RTH high (the most-recent prior calendar
    #          date in ET that has RTH bars in the bootstrap window).
    pmhod: float | None = None
    pdhod: float | None = None

    def reset_for_new_session(self) -> None:
        """Reset the intraday latches and counters that are populated by
        live bar processing. Reference levels (`pmhod`, `pdhod`) and any
        warm indicator history on the OWNING strategy are intentionally
        NOT cleared — those are inputs the live session inherits.
        """
        self.macd_1m_has_crossed_up_today = False
        self.macd_1m_has_crossed_down_today = False
        self.bars_below_vwap_consecutive = 0
        self.last_new_hod_bar_idx = None
        self.bars_processed_today = 0
        self.highs_history.clear()
        self.failed_setups_today = 0
        self.first_30m_volume = 0.0
        self.last_30m_volume = 0.0


class BacksideGate:
    """Evaluates `BacksideInputs` against the config and returns a decision.

    Stateless from the gate's perspective - all state is on the caller. This
    keeps the gate trivially testable: feed in inputs, assert the decision.
    """

    def __init__(self, config: BacksideConfig) -> None:
        self.config = config

    def evaluate(self, inp: BacksideInputs) -> BacksideDecision:
        hard_vetoes: list[str] = []
        reasons: list[str] = []

        # ---- hard vetoes ----
        if self.config.veto_5m_macd_negative_falling:
            h = inp.macd_5m_histogram
            h_prev = inp.macd_5m_histogram_prev
            if h is not None and h_prev is not None and h < 0 and h < h_prev:
                hard_vetoes.append(
                    f"5m MACD histogram is negative and falling ({h_prev:.6f} -> {h:.6f})"
                )

        if inp.macd_1m_has_crossed_down_today:
            hard_vetoes.append(
                "1m MACD has already crossed down today (potential backside risk)"
            )

        if inp.bars_below_vwap_consecutive >= self.config.vwap_loss_bars_required:
            hard_vetoes.append(
                f"price below VWAP for {inp.bars_below_vwap_consecutive} consecutive bars "
                f"(threshold {self.config.vwap_loss_bars_required})"
            )

        if self.config.veto_post_1030_no_new_hod and inp.is_post_1030_et:
            late = (
                inp.bars_since_last_new_hod is None
                or inp.bars_since_last_new_hod > self.config.late_day_grace_bars
            )
            if late:
                hard_vetoes.append(
                    "post 10:30 ET and no new HOD in last "
                    f"{self.config.late_day_grace_bars} bars"
                )

        # ---- soft scoring ----
        breakdown: dict[str, float] = {}
        cfg = self.config

        breakdown["lower_highs"] = self._score_linear(
            value=float(inp.lower_highs_count),
            zero_at=float(cfg.lower_highs_max_acceptable),
            full_at=float(cfg.lower_highs_lookback_bars),
            weight=cfg.weight_lower_highs,
        )

        breakdown["tape_buy_pct"] = self._score_linear_inverted(
            value=inp.tape_buy_pct_60s,
            zero_at=cfg.tape_buy_pct_floor,
            full_at=cfg.tape_buy_pct_zero_at,
            weight=cfg.weight_tape_buy_pct,
        )

        breakdown["tape_speed_decay"] = self._score_linear_inverted(
            value=inp.tape_speed_decay_pct,
            zero_at=cfg.tape_speed_decay_floor,
            full_at=cfg.tape_speed_decay_zero_at,
            weight=cfg.weight_tape_speed_decay,
        )

        breakdown["failed_setups"] = self._score_linear(
            value=float(inp.failed_setups_today),
            zero_at=0.0,
            full_at=float(cfg.failed_setups_zero_at),
            weight=cfg.weight_failed_setups,
        )

        breakdown["volume_decay"] = self._score_linear_inverted(
            value=inp.volume_decay_pct,
            zero_at=-0.20,    # 20% decay starts scoring
            full_at=-0.50,    # 50% decay = full weight
            weight=cfg.weight_volume_decay,
        )

        score = sum(breakdown.values())
        if score >= cfg.score_block_threshold:
            reasons.append(
                f"backside soft score {score:.1f} >= block threshold {cfg.score_block_threshold:.1f}"
            )

        block = bool(hard_vetoes or reasons)
        return BacksideDecision(
            block=block,
            hard_vetoes=tuple(hard_vetoes),
            score=score,
            score_breakdown=breakdown,
            reasons=tuple(hard_vetoes + reasons),
        )

    @staticmethod
    def _score_linear(
        *, value: float | None, zero_at: float, full_at: float, weight: float
    ) -> float:
        """0 below `zero_at`, full `weight` at-or-above `full_at`, linear in
        between. None inputs contribute 0."""
        if value is None or weight <= 0:
            return 0.0
        if full_at <= zero_at:
            return weight if value >= full_at else 0.0
        if value <= zero_at:
            return 0.0
        if value >= full_at:
            return weight
        return weight * (value - zero_at) / (full_at - zero_at)

    @staticmethod
    def _score_linear_inverted(
        *, value: float | None, zero_at: float, full_at: float, weight: float
    ) -> float:
        """0 above `zero_at`, full `weight` at-or-below `full_at`, linear in
        between. Used for metrics where LOWER is worse (e.g. tape buy %)."""
        if value is None or weight <= 0:
            return 0.0
        if full_at >= zero_at:
            return weight if value <= full_at else 0.0
        if value >= zero_at:
            return 0.0
        if value <= full_at:
            return weight
        return weight * (zero_at - value) / (zero_at - full_at)
