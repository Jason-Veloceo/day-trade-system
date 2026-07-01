"""Exit trigger framework.

When the engine is in an open long position, multiple INDEPENDENT triggers
watch for reasons to exit. The first one that fires wins; the engine then
journals the trigger reason and submits an exit order.

Triggers implemented (default set, each can be enabled/disabled by config):
  - hard_stop:        price <= stop_price (set from setup low at entry)
  - first_target:     price >= first_target (scale-out point - half exit)
  - second_target:    price >= second_target (full exit)
  - macd_flip:        1m MACD histogram crossed down
  - vwap_loss:        price below VWAP for N consecutive bars after entry
  - l2_distress:      bid:ask imbalance < threshold OR ask wall appears within risk band
  - tape_flip:        tape buy% < threshold AND tape speed decay below threshold
  - time_stop:        no progress (price within +/- X cents of entry) after N bars

The strategy hosts the framework but does NOT implement exit logic inside
its on_bar method - cleaner separation, easier to test triggers in isolation.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .backside import BacksideInputs
from .features import FeatureSnapshot


class ExitTriggerKind(StrEnum):
    HARD_STOP = "hard_stop"
    FIRST_TARGET = "first_target"
    SECOND_TARGET = "second_target"
    MACD_FLIP = "macd_flip"
    VWAP_LOSS = "vwap_loss"
    L2_DISTRESS = "l2_distress"
    TAPE_FLIP = "tape_flip"
    TIME_STOP = "time_stop"
    USER_DISARM = "user_disarm"


@dataclass(frozen=True, slots=True)
class ExitConfig:
    """Per-strategy exit configuration. All values are visible to the UI."""

    enable_hard_stop: bool = True
    enable_first_target: bool = True
    enable_second_target: bool = True
    enable_macd_flip: bool = True
    enable_vwap_loss: bool = True
    enable_l2_distress: bool = True
    enable_tape_flip: bool = True
    enable_time_stop: bool = True

    first_target_rr: float = 1.0            # 1R
    second_target_rr: float = 2.0           # 2R
    first_target_partial_fraction: float = 0.5

    vwap_loss_bars_after_entry: int = 2
    l2_distress_imbalance_floor: float = 0.30   # bid_share < 0.30 = sellers dominant
    l2_distress_wall_size_multiple: float = 5.0  # ask wall N x median ask size = distress
    l2_distress_wall_distance_bps: float = 20.0  # within 20bps of mid

    tape_flip_buy_pct_ceiling: float = 0.40
    tape_flip_speed_decay_floor: float = -0.30  # speed dropped 30% in 30s vs 60s
    tape_flip_bars_required: int = 2            # must persist for N consecutive bars

    time_stop_bars_max: int = 8                 # no real progress in 8 bars -> bail
    time_stop_progress_cents: float = 5.0       # +/- 5c counts as no progress


@dataclass(slots=True)
class ExitState:
    """Mutable state for one open position. Created on entry, cleared on exit."""

    entry_price: float
    stop_price: float
    entry_ts: dt.datetime
    quantity: int
    first_target: float
    second_target: float
    first_target_hit: bool = False

    bars_since_entry: int = 0
    bars_below_vwap_since_entry: int = 0
    consecutive_tape_flip_bars: int = 0


@dataclass(frozen=True, slots=True)
class ExitDecision:
    """One concrete exit instruction. The engine routes it to the executor."""

    kind: ExitTriggerKind
    reason: str
    fraction: float                  # 1.0 = full exit, 0.5 = half scale
    price_observed: float
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExitEvaluationInputs:
    """All the per-bar inputs the framework needs.

    Bar-level: ts, close, low, high (for stop check), volume.
    Indicator: macd_1m_histogram (current and previous).
    State: VWAP value + state.
    Features: FeatureSnapshot (optional - None when no L2/T&S).
    """

    ts: dt.datetime
    close: float
    low: float
    high: float
    macd_1m_histogram_prev: float | None
    macd_1m_histogram: float | None
    above_vwap: bool | None              # None = N/A (forex)
    feature_snapshot: FeatureSnapshot | None


class ExitTriggerSet:
    """Evaluates all enabled exit triggers against the current bar.

    Returns the first ExitDecision that fires, or None. Order matters:
    hard_stop is checked first (price-based, deterministic) so we never
    exit on a discretionary trigger before honouring the literal stop loss.
    """

    def __init__(self, config: ExitConfig) -> None:
        self.config = config
        self.state: ExitState | None = None

    def open(self, *, entry_price: float, stop_price: float, entry_ts: dt.datetime, quantity: int) -> None:
        if entry_price <= 0 or stop_price <= 0 or quantity <= 0:
            raise ValueError("invalid open params")
        if stop_price >= entry_price:
            raise ValueError(
                f"long entry stop must be below entry; got entry={entry_price} stop={stop_price}"
            )
        risk = entry_price - stop_price
        self.state = ExitState(
            entry_price=entry_price,
            stop_price=stop_price,
            entry_ts=entry_ts,
            quantity=quantity,
            first_target=entry_price + risk * self.config.first_target_rr,
            second_target=entry_price + risk * self.config.second_target_rr,
        )

    def close(self) -> None:
        self.state = None

    def on_tick(self, inp: ExitEvaluationInputs) -> ExitDecision | None:
        """Sub-bar exit evaluation against the in-progress 1m bar.

        Called by the engine on the 10s tick path. Checks the
        PRICE-DRIVEN triggers and the L2 distress trigger, which are
        the only ones whose decision can become "fire now" between bar
        boundaries:

          - hard_stop:      partial.low <= stop
          - second_target:  partial.high >= 2R
          - first_target:   partial.high >= 1R (partial scale)
          - l2_distress:    bid:ask imbalance below floor OR ask wall
                            within risk band (snapshot is real-time)

        Deliberately SKIPPED on the tick path:
          - macd_flip:     MACD uses last-closed-bar value (stale by
                           design per spec), no edge information at
                           10s cadence.
          - vwap_loss:     requires consecutive-bar count, which only
                           makes sense on closed-bar cadence.
          - tape_flip:     requires consecutive-bar count.
          - time_stop:     bar-count-based.

        IMPORTANT: this method does NOT touch ExitState counters
        (`bars_since_entry`, `bars_below_vwap_since_entry`,
        `consecutive_tape_flip_bars`). Those are advanced ONLY by
        `on_bar` so the "N consecutive bars" semantics stay correct.
        """
        s = self.state
        if s is None:
            return None

        if self.config.enable_hard_stop and inp.low <= s.stop_price:
            return ExitDecision(
                kind=ExitTriggerKind.HARD_STOP,
                reason=f"low={inp.low:.4f} <= stop={s.stop_price:.4f} (tick)",
                fraction=1.0,
                price_observed=s.stop_price,
            )

        if self.config.enable_second_target and inp.high >= s.second_target:
            return ExitDecision(
                kind=ExitTriggerKind.SECOND_TARGET,
                reason=(
                    f"high={inp.high:.4f} >= 2R target={s.second_target:.4f} (tick)"
                ),
                fraction=1.0,
                price_observed=s.second_target,
            )

        if (
            self.config.enable_first_target
            and not s.first_target_hit
            and inp.high >= s.first_target
        ):
            s.first_target_hit = True
            return ExitDecision(
                kind=ExitTriggerKind.FIRST_TARGET,
                reason=(
                    f"high={inp.high:.4f} >= 1R target={s.first_target:.4f} (tick)"
                ),
                fraction=self.config.first_target_partial_fraction,
                price_observed=s.first_target,
            )

        if self.config.enable_l2_distress and inp.feature_snapshot is not None:
            f = inp.feature_snapshot
            if f.has_depth:
                imbalance = f.bid_ask_imbalance
                wall_size = f.ask_wall_size
                wall_bps = f.ask_wall_distance_bps
                if imbalance is not None and imbalance < self.config.l2_distress_imbalance_floor:
                    return ExitDecision(
                        kind=ExitTriggerKind.L2_DISTRESS,
                        reason=(
                            f"bid:ask imbalance {imbalance:.2f} below floor "
                            f"{self.config.l2_distress_imbalance_floor:.2f} (tick)"
                        ),
                        fraction=1.0,
                        price_observed=inp.close,
                        extras={"imbalance": imbalance},
                    )
                if (
                    wall_size is not None
                    and wall_bps is not None
                    and f.ask_size_top is not None
                    and f.ask_size_top > 0
                    and wall_size >= f.ask_size_top * self.config.l2_distress_wall_size_multiple
                    and wall_bps <= self.config.l2_distress_wall_distance_bps
                ):
                    return ExitDecision(
                        kind=ExitTriggerKind.L2_DISTRESS,
                        reason=(
                            f"ask wall {wall_size:.0f}@{f.ask_wall_price:.4f} "
                            f"({wall_bps:.1f}bps above mid) >> top-of-book ask "
                            f"{f.ask_size_top:.0f} (tick)"
                        ),
                        fraction=1.0,
                        price_observed=inp.close,
                        extras={
                            "wall_price": f.ask_wall_price,
                            "wall_size": wall_size,
                            "wall_distance_bps": wall_bps,
                        },
                    )

        return None

    def on_bar(self, inp: ExitEvaluationInputs) -> ExitDecision | None:
        s = self.state
        if s is None:
            return None
        s.bars_since_entry += 1

        # --- 1. hard stop (price-driven, always first) ---
        if self.config.enable_hard_stop and inp.low <= s.stop_price:
            return ExitDecision(
                kind=ExitTriggerKind.HARD_STOP,
                reason=f"low={inp.low:.4f} <= stop={s.stop_price:.4f}",
                fraction=1.0,
                price_observed=s.stop_price,
            )

        # --- 2. second target (full exit) ---
        if self.config.enable_second_target and inp.high >= s.second_target:
            return ExitDecision(
                kind=ExitTriggerKind.SECOND_TARGET,
                reason=f"high={inp.high:.4f} >= 2R target={s.second_target:.4f}",
                fraction=1.0,
                price_observed=s.second_target,
            )

        # --- 3. first target (partial scale) ---
        if (
            self.config.enable_first_target
            and not s.first_target_hit
            and inp.high >= s.first_target
        ):
            s.first_target_hit = True
            return ExitDecision(
                kind=ExitTriggerKind.FIRST_TARGET,
                reason=f"high={inp.high:.4f} >= 1R target={s.first_target:.4f}",
                fraction=self.config.first_target_partial_fraction,
                price_observed=s.first_target,
            )

        # --- 4. MACD flip ---
        if (
            self.config.enable_macd_flip
            and inp.macd_1m_histogram is not None
            and inp.macd_1m_histogram_prev is not None
            and inp.macd_1m_histogram_prev > 0
            and inp.macd_1m_histogram <= 0
        ):
            return ExitDecision(
                kind=ExitTriggerKind.MACD_FLIP,
                reason=(
                    f"1m MACD histogram flipped negative "
                    f"({inp.macd_1m_histogram_prev:.6f} -> {inp.macd_1m_histogram:.6f})"
                ),
                fraction=1.0,
                price_observed=inp.close,
            )

        # --- 5. VWAP loss ---
        if self.config.enable_vwap_loss and inp.above_vwap is False:
            s.bars_below_vwap_since_entry += 1
            if s.bars_below_vwap_since_entry >= self.config.vwap_loss_bars_after_entry:
                return ExitDecision(
                    kind=ExitTriggerKind.VWAP_LOSS,
                    reason=(
                        f"below VWAP for {s.bars_below_vwap_since_entry} bars "
                        f"(threshold {self.config.vwap_loss_bars_after_entry})"
                    ),
                    fraction=1.0,
                    price_observed=inp.close,
                )
        elif inp.above_vwap is True:
            s.bars_below_vwap_since_entry = 0

        # --- 6. L2 distress ---
        if self.config.enable_l2_distress and inp.feature_snapshot is not None:
            f = inp.feature_snapshot
            if f.has_depth:
                imbalance = f.bid_ask_imbalance
                wall_size = f.ask_wall_size
                wall_bps = f.ask_wall_distance_bps
                # Imbalance check
                if imbalance is not None and imbalance < self.config.l2_distress_imbalance_floor:
                    return ExitDecision(
                        kind=ExitTriggerKind.L2_DISTRESS,
                        reason=(
                            f"bid:ask imbalance {imbalance:.2f} below floor "
                            f"{self.config.l2_distress_imbalance_floor:.2f}"
                        ),
                        fraction=1.0,
                        price_observed=inp.close,
                        extras={"imbalance": imbalance},
                    )
                # Ask wall check
                if (
                    wall_size is not None
                    and wall_bps is not None
                    and f.ask_size_top is not None
                    and f.ask_size_top > 0
                    and wall_size >= f.ask_size_top * self.config.l2_distress_wall_size_multiple
                    and wall_bps <= self.config.l2_distress_wall_distance_bps
                ):
                    return ExitDecision(
                        kind=ExitTriggerKind.L2_DISTRESS,
                        reason=(
                            f"ask wall {wall_size:.0f}@{f.ask_wall_price:.4f} "
                            f"({wall_bps:.1f}bps above mid) >> top-of-book ask "
                            f"{f.ask_size_top:.0f}"
                        ),
                        fraction=1.0,
                        price_observed=inp.close,
                        extras={
                            "wall_price": f.ask_wall_price,
                            "wall_size": wall_size,
                            "wall_distance_bps": wall_bps,
                        },
                    )

        # --- 7. Tape flip (requires consecutive bars of bad flow) ---
        if self.config.enable_tape_flip and inp.feature_snapshot is not None:
            f = inp.feature_snapshot
            if f.has_tape and f.tape_buy_pct_60s is not None and f.tape_speed_decay_pct is not None:
                if (
                    f.tape_buy_pct_60s <= self.config.tape_flip_buy_pct_ceiling
                    and f.tape_speed_decay_pct <= self.config.tape_flip_speed_decay_floor
                ):
                    s.consecutive_tape_flip_bars += 1
                else:
                    s.consecutive_tape_flip_bars = 0
                if s.consecutive_tape_flip_bars >= self.config.tape_flip_bars_required:
                    return ExitDecision(
                        kind=ExitTriggerKind.TAPE_FLIP,
                        reason=(
                            f"tape flipped for {s.consecutive_tape_flip_bars} bars "
                            f"(buy%={f.tape_buy_pct_60s:.2f} <= "
                            f"{self.config.tape_flip_buy_pct_ceiling:.2f}, "
                            f"speed_decay={f.tape_speed_decay_pct:.2f} <= "
                            f"{self.config.tape_flip_speed_decay_floor:.2f})"
                        ),
                        fraction=1.0,
                        price_observed=inp.close,
                    )

        # --- 8. Time stop ---
        if (
            self.config.enable_time_stop
            and s.bars_since_entry >= self.config.time_stop_bars_max
        ):
            move = abs(inp.close - s.entry_price) * 100.0  # cents
            if move <= self.config.time_stop_progress_cents:
                return ExitDecision(
                    kind=ExitTriggerKind.TIME_STOP,
                    reason=(
                        f"no progress for {s.bars_since_entry} bars "
                        f"(move {move:.2f}c <= threshold "
                        f"{self.config.time_stop_progress_cents:.2f}c)"
                    ),
                    fraction=1.0,
                    price_observed=inp.close,
                )

        return None
