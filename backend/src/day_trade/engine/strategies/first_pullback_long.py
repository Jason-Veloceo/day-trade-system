"""First-pullback / micro-pullback long-only strategy.

Multi-timeframe MACD trend gate + backside gate + L2/T&S gates + a
configurable trigger. The default trigger is the Ross-style structural
micro-pullback breakout: "first 1m candle to make a new high after the
most recent red-candle pullback". An alternative MACD cross-up trigger is
also available for indicator-only runs.

This is the v1 implementation of the Ross-inspired semi-automated workflow.
Detailed thresholds live in `engine/backside.py::BacksideConfig`, `engine/
exits.py::ExitConfig`, `engine/triggers.py::PullbackBreakConfig`, and this
class's params; the UI surfaces all of them.

Auto re-arm: the strategy exposes `in_position` and signals exits via the
exit trigger framework, but it stays "armed" for the next entry indefinitely.
The engine treats the strategy as the source of entry signals and the
ExitTriggerSet as the source of exit signals.

L2/T&S degradation: when no subscription is active, the L2/T&S gates and
features return None and the strategy treats them as N/A (gate passes). On
forex, this is the expected mode; on US small caps, the gates contribute
both to entry quality and to exit triggers once a subscription is active.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..backside import BacksideConfig, BacksideGate, BacksideInputs, BacksideState
from ..exits import ExitConfig
from ..features import FeatureSnapshot
from ..indicators import MACD, MACDValue
from ..triggers import (
    PullbackBreakConfig,
    TriggerResult,
    detect_macd_cross_up,
    detect_pullback_break,
)
from ..vwap import SessionVwap
from .base import Bar, Signal, SignalKind, Strategy

logger = logging.getLogger(__name__)


class GateFailureCategory(StrEnum):
    """Why a gate said no. Drives UI grouping so the user can tell a
    real veto from "still warming up" or "trigger pattern not yet
    complete" at a glance.
    """

    WARMUP = "warmup"               # indicator has not produced its first value
    INDICATOR = "indicator"         # indicator is warm but value fails the threshold
    VWAP = "vwap"                   # VWAP-relative condition
    BACKSIDE = "backside"           # backside gate hard veto or soft score
    TRIGGER = "trigger"             # entry trigger pattern didn't fire this bar
    MICROSTRUCTURE = "microstructure"  # L2 / tape / spread last-look (entry-time)


@dataclass(frozen=True, slots=True)
class GateFailure:
    """One reason a gate said no. `category` lets the UI group failures
    so "5m MACD warming up" doesn't look the same as "1m MACD currently
    negative" — which from the trader's POV are very different states."""

    category: GateFailureCategory
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"category": str(self.category), "message": self.message}


@dataclass(slots=True)
class EntryGateResult:
    """Per-bar decision returned by the entry gate stack."""

    passed: bool
    failures: list[GateFailure] = field(default_factory=list)
    notes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TrendGateConfig:
    require_5m_histogram_positive: bool = True
    require_5m_histogram_not_falling: bool = True
    # Two flavours of 1m MACD requirement:
    #   require_1m_positive: histogram > 0 (used as a CONTEXT gate when the
    #     entry trigger is structural, e.g. pullback_break)
    #   require_1m_trigger:  histogram crossed up OR positive-and-rising,
    #     i.e. it IS the trigger (used with trigger_mode='macd_cross')
    # When trigger_mode='pullback_break' the strategy enforces _positive_;
    # when trigger_mode='macd_cross' the strategy enforces _trigger_.
    require_1m_positive: bool = True
    require_1m_trigger: bool = True

    # Microstructure (entry-time only)
    max_spread_bps: float = 50.0
    require_above_vwap: bool = True
    min_bid_ask_imbalance: float = 0.55  # bid_share, when L2 available
    min_tape_buy_pct: float = 0.55       # buy share, when tape available


@dataclass(frozen=True, slots=True)
class StopConfig:
    """How far below entry the stop is placed at entry time.

    Three modes:
      - 'pullback_low': stop = pullback low captured by the trigger - buffer
        (only valid when trigger fired and produced a pullback_low; falls back
        to 'recent_low' otherwise)
      - 'recent_low': stop = lowest low of last `recent_low_lookback` bars - buffer
      - 'fixed_cents': stop = entry - fixed_cents_below_entry
    """

    mode: str = "pullback_low"
    recent_low_lookback: int = 3
    stop_buffer_cents: float = 2.0
    fixed_cents_below_entry: float = 10.0


class FirstPullbackLong(Strategy):
    """First-pullback long entry strategy with multi-TF and microstructure gates.

    Strategy params (all overrideable via the engine API):
      macd_fast / macd_slow / macd_signal:  MACD periods on both 1m and 5m
      trend / backside / stop:              subconfig dataclass overrides
    """

    name = "first_pullback_long"

    # Valid trigger modes
    TRIGGER_MODE_PULLBACK_BREAK = "pullback_break"
    TRIGGER_MODE_MACD_CROSS = "macd_cross"
    VALID_TRIGGER_MODES = (TRIGGER_MODE_PULLBACK_BREAK, TRIGGER_MODE_MACD_CROSS)

    def __init__(
        self,
        *,
        macd_fast: int = 12,
        macd_slow: int = 26,
        macd_signal: int = 9,
        trigger_mode: str = TRIGGER_MODE_PULLBACK_BREAK,
        trend: TrendGateConfig | None = None,
        backside: BacksideConfig | None = None,
        stop: StopConfig | None = None,
        pullback: PullbackBreakConfig | None = None,
    ) -> None:
        if trigger_mode not in self.VALID_TRIGGER_MODES:
            raise ValueError(
                f"trigger_mode must be one of {self.VALID_TRIGGER_MODES}, got {trigger_mode!r}"
            )
        self._params = {
            "macd_fast": macd_fast,
            "macd_slow": macd_slow,
            "macd_signal": macd_signal,
            "trigger_mode": trigger_mode,
        }
        self.trigger_mode = trigger_mode
        self.macd_1m = MACD(fast=macd_fast, slow=macd_slow, signal=macd_signal)
        self.macd_5m = MACD(fast=macd_fast, slow=macd_slow, signal=macd_signal)

        self.trend_cfg = trend or TrendGateConfig()
        self.backside_cfg = backside or BacksideConfig()
        self.stop_cfg = stop or StopConfig()
        self.pullback_cfg = pullback or PullbackBreakConfig()
        self.backside_gate = BacksideGate(self.backside_cfg)
        self.exit_cfg = ExitConfig()

        self.vwap = SessionVwap()
        self.backside_state = BacksideState()

        # Indicator state
        self._macd_1m_last: MACDValue | None = None
        self._macd_5m_last: MACDValue | None = None
        self._macd_1m_prev_hist: float | None = None
        self._macd_5m_prev_hist: float | None = None

        # Lookback buffers - must be large enough to cover impulse + pullback
        # + gap_to_trigger for the pullback_break trigger.
        min_history = (
            self.pullback_cfg.max_pullback_bars
            + self.pullback_cfg.max_bars_since_pullback_end
            + max(self.pullback_cfg.min_impulse_bars, 2)
            + 2
        )
        self._recent_1m_bars: list[Bar] = []
        self._recent_1m_max: int = max(
            10, self.stop_cfg.recent_low_lookback + 2, min_history
        )

        # Position / arm state
        self._in_position: bool = False
        self._high_of_day: float | None = None

        # Cached pullback info (set when a pullback_break trigger fires).
        # Used by suggest_stop_price.
        self._last_pullback_low: float | None = None
        self._last_pullback_test_high: float | None = None

        # Last gate eval - exposed in snapshot for the UI
        self._last_entry_gate: EntryGateResult | None = None
        self._last_backside_decision = None
        self._last_trigger_result: TriggerResult | None = None

    # ---- Strategy API ----

    def on_bar(self, bar: Bar) -> Signal | None:
        """Process a closed 1m bar. Returns an ENTER_LONG signal if all gates pass."""
        # Update VWAP regardless of position.
        self.vwap.update(bar.ts, bar.high, bar.low, bar.close, bar.volume)

        # Update 1m MACD.
        macd_val = self.macd_1m.update(bar.close)
        if macd_val is not None:
            self._macd_1m_prev_hist = self._macd_1m_last.histogram if self._macd_1m_last else None
            self._macd_1m_last = macd_val

        # Update 1m MACD cross-down latch for backside gate.
        prev = self._macd_1m_prev_hist
        curr_hist = macd_val.histogram if macd_val else None
        if prev is not None and curr_hist is not None:
            if prev > 0 and curr_hist <= 0:
                self.backside_state.macd_1m_has_crossed_down_today = True
            if prev <= 0 and curr_hist > 0:
                self.backside_state.macd_1m_has_crossed_up_today = True

        # Track recent bars + high-of-day.
        self._recent_1m_bars.append(bar)
        if len(self._recent_1m_bars) > self._recent_1m_max:
            self._recent_1m_bars.pop(0)
        is_new_hod = self._high_of_day is None or bar.high > self._high_of_day
        if is_new_hod:
            self._high_of_day = bar.high
            self.backside_state.last_new_hod_bar_idx = self.backside_state.bars_processed_today
        self.backside_state.bars_processed_today += 1

        # Lower-highs lookback (for backside score).
        self.backside_state.highs_history.append(bar.high)
        if len(self.backside_state.highs_history) > 50:
            self.backside_state.highs_history.pop(0)

        # VWAP-loss latch.
        vw = self.vwap.last
        if vw is not None and vw.state == "below":
            self.backside_state.bars_below_vwap_consecutive += 1
        elif vw is not None and vw.state == "above":
            self.backside_state.bars_below_vwap_consecutive = 0

        # While in position we don't produce ENTER signals; exits are routed
        # by the engine through the ExitTriggerSet.
        if self._in_position:
            return None

        # ---- Evaluate the entry gate stack ----
        result = self._evaluate_entry_gates(bar)
        self._last_entry_gate = result
        if not result.passed:
            return None

        # All gates passed -> emit an ENTER_LONG signal at the bar close.
        self._in_position = True  # optimistic latch; engine will roll back on rejection
        trig = self._last_trigger_result
        return Signal(
            kind=SignalKind.ENTER_LONG,
            ts=bar.ts,
            price=bar.close,
            reason=(
                f"first_pullback_long ({self.trigger_mode}): all entry gates passed"
            ),
            extras={
                "gate_notes": dict(result.notes),
                "macd_1m_hist": curr_hist,
                "macd_5m_hist": self._macd_5m_last.histogram if self._macd_5m_last else None,
                "vwap": vw.value if vw else None,
                "vwap_state": vw.state if vw else "na",
                "trigger": {
                    "mode": trig.mode if trig else self.trigger_mode,
                    "reason": trig.reason if trig else None,
                    "pullback_test_high": trig.pullback_test_high if trig else None,
                    "pullback_low": trig.pullback_low if trig else None,
                    "pullback_bar_count": trig.pullback_bar_count if trig else 0,
                    "impulse_bar_count": trig.impulse_bar_count if trig else 0,
                },
                "stop_suggestion": self.suggest_stop_price(bar),
            },
        )

    def on_5m_bar(self, bar: Bar) -> None:
        """Process a closed 5m bar. Updates the 5m MACD; never emits signals."""
        macd_val = self.macd_5m.update(bar.close)
        if macd_val is not None:
            self._macd_5m_prev_hist = self._macd_5m_last.histogram if self._macd_5m_last else None
            self._macd_5m_last = macd_val

    def on_tick(self, partial: Bar) -> Signal | None:
        """Evaluate entry conditions against the in-progress 1m bar
        (Ross-aggressive: fire the moment partial.high breaks the
        pullback level, even mid-candle).

        Read-only on indicator/latch state. Indicators (MACD, VWAP) use
        their LAST CLOSED VALUES — they're not recomputed against the
        partial. The partial's price feeds the trigger and the VWAP
        gate's "close vs vwap" comparison only.

        Returns an ENTER_LONG signal with `extras.is_mid_candle=True`
        when all gates pass. The engine routes it through the same
        approval / risk / executor path as a closed-bar signal.
        """
        # No entries while already in (or pending) a position.
        if self._in_position:
            return None

        # Need at least one closed 1m bar's worth of history before
        # mid-candle triggers are meaningful (pullback structure has to
        # come from somewhere).
        if not self._recent_1m_bars:
            return None

        result = self._evaluate_entry_gates(partial, is_tick=True)
        self._last_entry_gate = result
        if not result.passed:
            return None

        # All gates passed -> emit. Optimistic in-position latch
        # mirrors the on_bar path; the engine rolls it back via
        # `mark_exited()` if microstructure last-look, risk gate, or
        # portfolio mutex rejects the order.
        self._in_position = True
        trig = self._last_trigger_result
        m1 = self._macd_1m_last
        m5 = self._macd_5m_last
        vw = self.vwap.last
        return Signal(
            kind=SignalKind.ENTER_LONG,
            ts=partial.ts,
            price=partial.close,
            reason=(
                f"first_pullback_long ({self.trigger_mode}, mid-candle): "
                "all entry gates passed"
            ),
            extras={
                "gate_notes": dict(result.notes),
                "is_mid_candle": True,
                "macd_1m_hist": m1.histogram if m1 else None,
                "macd_5m_hist": m5.histogram if m5 else None,
                "vwap": vw.value if vw else None,
                "vwap_state": vw.state if vw else "na",
                "trigger": {
                    "mode": trig.mode if trig else self.trigger_mode,
                    "reason": trig.reason if trig else None,
                    "pullback_test_high": trig.pullback_test_high if trig else None,
                    "pullback_low": trig.pullback_low if trig else None,
                    "pullback_bar_count": trig.pullback_bar_count if trig else 0,
                    "impulse_bar_count": trig.impulse_bar_count if trig else 0,
                },
                "stop_suggestion": self.suggest_stop_price(partial),
            },
        )

    # ---- Helpers consumed by the engine ----

    def mark_entered(self) -> None:
        """Confirm the entry actually filled. (We optimistically latched on emit.)"""
        self._in_position = True

    def mark_exited(self) -> None:
        """Engine calls this when the exit fully fills (auto re-arm point)."""
        self._in_position = False
        # Clear cached pullback info so a stale low can't influence the next trade.
        self._last_pullback_low = None
        self._last_pullback_test_high = None

    def record_failed_setup(self) -> None:
        """Engine calls this when an entry/exit cycle ended below entry."""
        self.backside_state.failed_setups_today += 1

    def suggest_stop_price(self, entry_bar: Bar) -> float:
        """Compute the stop price for an entry at `entry_bar.close`.

        Mode precedence:
          - 'pullback_low': prefer the just-detected pullback low (set by the
            pullback_break trigger). If unavailable, fall back to recent_low.
          - 'recent_low':   lowest low of last N bars
          - 'fixed_cents':  entry - fixed_cents_below_entry
        """
        cfg = self.stop_cfg
        buffer = cfg.stop_buffer_cents / 100.0

        if cfg.mode == "pullback_low" and self._last_pullback_low is not None:
            return self._last_pullback_low - buffer

        if cfg.mode == "fixed_cents":
            return entry_bar.close - cfg.fixed_cents_below_entry / 100.0

        # default + fallback: recent_low
        lookback = self._recent_1m_bars[-cfg.recent_low_lookback :]
        if not lookback:
            return entry_bar.close - cfg.fixed_cents_below_entry / 100.0
        recent_low = min(b.low for b in lookback)
        return recent_low - buffer

    def snapshot(self) -> dict[str, Any]:
        m1 = self._macd_1m_last
        m5 = self._macd_5m_last
        vw = self.vwap.last
        last_gate = self._last_entry_gate
        last_trig = self._last_trigger_result
        return {
            "name": self.name,
            "params": dict(self._params),
            "trigger_mode": self.trigger_mode,
            "in_position": self._in_position,
            "macd_line": m1.macd if m1 else None,
            "macd_signal": m1.signal if m1 else None,
            "macd_histogram": m1.histogram if m1 else None,
            "macd_1m_hist": m1.histogram if m1 else None,
            "macd_5m_hist": m5.histogram if m5 else None,
            "macd_5m_signal": m5.signal if m5 else None,
            "vwap": vw.value if vw else None,
            "vwap_state": vw.state if vw else "na",
            "vwap_cum_volume": vw.cum_volume if vw else None,
            "high_of_day": self._high_of_day,
            "pmhod": self.backside_state.pmhod,
            "pdhod": self.backside_state.pdhod,
            "bars_below_vwap_consecutive": self.backside_state.bars_below_vwap_consecutive,
            "macd_1m_crossed_down_today": self.backside_state.macd_1m_has_crossed_down_today,
            "failed_setups_today": self.backside_state.failed_setups_today,
            "last_entry_gate": {
                "passed": last_gate.passed if last_gate else None,
                "failures": (
                    [f.to_dict() for f in last_gate.failures] if last_gate else []
                ),
                "notes": dict(last_gate.notes) if last_gate else {},
            },
            "last_trigger": {
                "mode": last_trig.mode if last_trig else self.trigger_mode,
                "fired": last_trig.fired if last_trig else None,
                "reason": last_trig.reason if last_trig else None,
                "pullback_test_high": last_trig.pullback_test_high if last_trig else None,
                "pullback_low": last_trig.pullback_low if last_trig else None,
                "pullback_bar_count": last_trig.pullback_bar_count if last_trig else 0,
                "impulse_bar_count": last_trig.impulse_bar_count if last_trig else 0,
            },
            "config": self._config_snapshot(),
        }

    # ---- Bootstrap finalisation ----

    def finalize_bootstrap(
        self, *, pmhod: float | None, pdhod: float | None
    ) -> None:
        """Reset live-session latches/counters after the engine has replayed
        historical bars to warm indicators.

        The bootstrap replays ~2 trading days of 1m bars through `on_bar`
        so MACD/VWAP are immediately usable. That same replay, however,
        also populates per-strategy state as a SIDE EFFECT:

          - BacksideState latches (the
            `macd_1m_has_crossed_down_today` flag, intraday HOD
            tracking, VWAP-loss counter, failed-setup tally) get
            pre-loaded with prior-session activity.
          - `_in_position` can latch `True` if a replayed bar's gates
            happened to pass and emit ENTER_LONG. The signal is
            discarded by the engine but the latch is not — leaving the
            strategy convinced it's in a position it never opened, so
            no live entries are evaluated.

        For the live session those side effects are spurious — they
        describe history, not "today". This method clears them while
        preserving:
          - both MACDs (the `_fast`/`_slow`/`_signal` EMA state on the
            indicator objects themselves),
          - the previous-histogram trackers (`_macd_*_prev_hist`), so
            cross-detection on the FIRST live bar uses a real "prev",
          - VWAP cumulative state (SessionVwap auto-resets at the
            13:30 UTC RTH boundary; whatever cum_pv/cum_v it carries
            at this point already reflects the correct anchor),
          - `_recent_1m_bars` — the buffer's tail is the most-recent
            premarket bars, which is legitimate structural context for
            the first live pullback-break detection,
          - reference levels `pmhod` (today's premarket high so far) and
            `pdhod` (previous session's RTH high), which are
            informational features the live session inherits.
        """
        self.backside_state.reset_for_new_session()
        self.backside_state.pmhod = pmhod
        self.backside_state.pdhod = pdhod
        # Live-session intraday HOD starts fresh; bootstrap HOD is now
        # exposed as pmhod/pdhod and no longer drives the post-1030
        # grace logic against historical highs.
        self._high_of_day = None
        # Drop any optimistic in-position latch left over from a
        # replayed signal that was never actually executed.
        self._in_position = False
        # Cached pullback info from the last replayed trigger is stale
        # — the next live trigger will populate these afresh.
        self._last_pullback_low = None
        self._last_pullback_test_high = None
        self._last_entry_gate = None
        self._last_trigger_result = None
        self._last_backside_decision = None

    def _config_snapshot(self) -> dict[str, Any]:
        return {
            "trigger_mode": self.trigger_mode,
            "trend": {
                "require_5m_histogram_positive": self.trend_cfg.require_5m_histogram_positive,
                "require_5m_histogram_not_falling": self.trend_cfg.require_5m_histogram_not_falling,
                "require_1m_positive": self.trend_cfg.require_1m_positive,
                "require_1m_trigger": self.trend_cfg.require_1m_trigger,
                "max_spread_bps": self.trend_cfg.max_spread_bps,
                "require_above_vwap": self.trend_cfg.require_above_vwap,
                "min_bid_ask_imbalance": self.trend_cfg.min_bid_ask_imbalance,
                "min_tape_buy_pct": self.trend_cfg.min_tape_buy_pct,
            },
            "pullback": {
                "min_pullback_bars": self.pullback_cfg.min_pullback_bars,
                "max_pullback_bars": self.pullback_cfg.max_pullback_bars,
                "max_bars_since_pullback_end": self.pullback_cfg.max_bars_since_pullback_end,
                "require_impulse": self.pullback_cfg.require_impulse,
                "min_impulse_bars": self.pullback_cfg.min_impulse_bars,
                "strict_break": self.pullback_cfg.strict_break,
            },
            "backside": {
                "score_block_threshold": self.backside_cfg.score_block_threshold,
                "vwap_loss_bars_required": self.backside_cfg.vwap_loss_bars_required,
                "late_day_grace_bars": self.backside_cfg.late_day_grace_bars,
            },
            "stop": {
                "mode": self.stop_cfg.mode,
                "recent_low_lookback": self.stop_cfg.recent_low_lookback,
                "stop_buffer_cents": self.stop_cfg.stop_buffer_cents,
                "fixed_cents_below_entry": self.stop_cfg.fixed_cents_below_entry,
            },
            "exits": {
                "first_target_rr": self.exit_cfg.first_target_rr,
                "second_target_rr": self.exit_cfg.second_target_rr,
                "first_target_partial_fraction": self.exit_cfg.first_target_partial_fraction,
                "vwap_loss_bars_after_entry": self.exit_cfg.vwap_loss_bars_after_entry,
                "l2_distress_imbalance_floor": self.exit_cfg.l2_distress_imbalance_floor,
                "tape_flip_buy_pct_ceiling": self.exit_cfg.tape_flip_buy_pct_ceiling,
                "time_stop_bars_max": self.exit_cfg.time_stop_bars_max,
            },
        }

    # ---- Gate stack ----

    def _evaluate_entry_gates(self, bar: Bar, *, is_tick: bool = False) -> EntryGateResult:
        """Evaluate the gate stack against `bar`.

        `is_tick=True` means this is a mid-candle evaluation against a
        SYNTHETIC partial bar that is NOT in `_recent_1m_bars`. The only
        path that cares about this distinction is the pullback-break
        trigger, which slices `_recent_1m_bars` to derive the history
        window — see `_evaluate_trigger`.

        IMPORTANT: this method reads indicator state (MACD, VWAP) and
        latches (backside_state) but must not mutate them. State
        mutation belongs to `on_bar`. The only fields this method
        writes are `_last_*` informational caches (intentional, for
        the UI snapshot).
        """
        failures: list[GateFailure] = []
        notes: dict[str, Any] = {}

        # ---- Trend gate (5m) ----
        m5 = self._macd_5m_last
        m5_prev = self._macd_5m_prev_hist
        if self.trend_cfg.require_5m_histogram_positive:
            if m5 is None:
                failures.append(
                    GateFailure(GateFailureCategory.WARMUP, "5m MACD not warmed up yet")
                )
            elif m5.histogram <= 0:
                failures.append(
                    GateFailure(
                        GateFailureCategory.INDICATOR,
                        f"5m MACD histogram <= 0 ({m5.histogram:.6f})",
                    )
                )
        if self.trend_cfg.require_5m_histogram_not_falling and m5 is not None and m5_prev is not None:
            if m5.histogram < m5_prev:
                failures.append(
                    GateFailure(
                        GateFailureCategory.INDICATOR,
                        f"5m MACD histogram is falling ({m5_prev:.6f} -> {m5.histogram:.6f})",
                    )
                )
        notes["macd_5m_hist"] = m5.histogram if m5 else None
        notes["macd_5m_hist_prev"] = m5_prev

        # ---- 1m MACD context gate (used when trigger is structural) ----
        m1 = self._macd_1m_last
        m1_prev = self._macd_1m_prev_hist
        notes["macd_1m_hist"] = m1.histogram if m1 else None
        notes["macd_1m_hist_prev"] = m1_prev
        if (
            self.trigger_mode == self.TRIGGER_MODE_PULLBACK_BREAK
            and self.trend_cfg.require_1m_positive
        ):
            if m1 is None:
                failures.append(
                    GateFailure(GateFailureCategory.WARMUP, "1m MACD not warmed up yet")
                )
            elif m1.histogram <= 0:
                failures.append(
                    GateFailure(
                        GateFailureCategory.INDICATOR,
                        (
                            f"1m MACD histogram not positive ({m1.histogram:.6f}); "
                            "structural trigger requires 1m momentum context"
                        ),
                    )
                )

        # ---- VWAP gate ----
        vw = self.vwap.last
        if self.trend_cfg.require_above_vwap:
            if vw is None:
                # Not enough bars yet - skip rather than block (warm-up)
                notes["vwap"] = None
            elif vw.state == "na":
                # Forex / no volume - skip the VWAP gate
                notes["vwap"] = "na"
            elif vw.state == "below":
                failures.append(
                    GateFailure(
                        GateFailureCategory.VWAP,
                        f"price below VWAP ({bar.close:.4f} vs {vw.value:.4f})",
                    )
                )
                notes["vwap"] = vw.value
            else:
                notes["vwap"] = vw.value

        # ---- Backside gate ----
        is_post_1030_et = self._is_post_1030_et(bar.ts)
        bars_since_hod = (
            self.backside_state.bars_processed_today - self.backside_state.last_new_hod_bar_idx
            if self.backside_state.last_new_hod_bar_idx is not None
            else None
        )
        lower_highs_count = self._count_lower_highs()
        backside_inp = BacksideInputs(
            now=bar.ts,
            macd_5m_histogram=m5.histogram if m5 else None,
            macd_5m_histogram_prev=m5_prev,
            macd_1m_has_crossed_down_today=self.backside_state.macd_1m_has_crossed_down_today,
            bars_below_vwap_consecutive=self.backside_state.bars_below_vwap_consecutive,
            is_post_1030_et=is_post_1030_et,
            bars_since_last_new_hod=bars_since_hod,
            lower_highs_count=lower_highs_count,
            tape_buy_pct_60s=None,        # set by engine via update_features if available
            tape_speed_decay_pct=None,
            failed_setups_today=self.backside_state.failed_setups_today,
            volume_decay_pct=None,
        )
        backside_decision = self.backside_gate.evaluate(backside_inp)
        self._last_backside_decision = backside_decision
        notes["backside"] = backside_decision.to_dict()
        if backside_decision.block:
            for r in backside_decision.reasons:
                failures.append(
                    GateFailure(GateFailureCategory.BACKSIDE, f"backside: {r}")
                )

        # ---- Trigger (last gate; the "this bar is the one" check) ----
        trigger_result = self._evaluate_trigger(bar, is_tick=is_tick)
        self._last_trigger_result = trigger_result
        notes["trigger"] = {
            "mode": trigger_result.mode,
            "fired": trigger_result.fired,
            "reason": trigger_result.reason,
            "pullback_test_high": trigger_result.pullback_test_high,
            "pullback_low": trigger_result.pullback_low,
            "pullback_bar_count": trigger_result.pullback_bar_count,
            "impulse_bar_count": trigger_result.impulse_bar_count,
            "crossed_up": trigger_result.crossed_up,
            "positive_and_rising": trigger_result.positive_and_rising,
        }
        if trigger_result.fired:
            # Cache the pullback low for stop computation.
            if trigger_result.pullback_low is not None:
                self._last_pullback_low = trigger_result.pullback_low
                self._last_pullback_test_high = trigger_result.pullback_test_high
        else:
            failures.append(
                GateFailure(
                    GateFailureCategory.TRIGGER,
                    f"trigger ({trigger_result.mode}): {trigger_result.reason}",
                )
            )

        passed = not failures
        return EntryGateResult(passed=passed, failures=failures, notes=notes)

    def _evaluate_trigger(self, bar: Bar, *, is_tick: bool = False) -> TriggerResult:
        """Dispatch to the configured trigger and return its result.

        For `pullback_break`, the history window is the closed 1m bars
        STRICTLY BEFORE the candidate bar:
          - `on_bar` path: the candidate bar was just appended to
            `_recent_1m_bars`, so history is `_recent_1m_bars[:-1]`.
          - `on_tick` path: the candidate is the in-progress partial,
            which is NOT in `_recent_1m_bars`, so the whole buffer is
            the history window.
        """
        if self.trigger_mode == self.TRIGGER_MODE_PULLBACK_BREAK:
            history = (
                list(self._recent_1m_bars)
                if is_tick
                else self._recent_1m_bars[:-1]
            )
            return detect_pullback_break(
                current_bar=bar,
                history=history,
                config=self.pullback_cfg,
            )
        # macd_cross
        m1 = self._macd_1m_last
        m1_prev = self._macd_1m_prev_hist
        return detect_macd_cross_up(
            histogram=m1.histogram if m1 else None,
            histogram_prev=m1_prev,
        )

    def evaluate_microstructure_gates(
        self, *, snapshot: FeatureSnapshot | None
    ) -> tuple[bool, list[GateFailure], dict[str, Any]]:
        """Evaluate L2/T&S gates against a feature snapshot. Pure - the
        strategy doesn't store snapshot history.

        Returns (passed, failures, notes). Used by the engine immediately
        before submitting an order, so we don't fire if the book deteriorated
        between bar close and order ack.
        """
        failures: list[GateFailure] = []
        notes: dict[str, Any] = {}
        if snapshot is None:
            notes["l2_ts"] = "na"
            return True, [], notes

        # Spread
        if snapshot.has_depth and snapshot.spread_bps is not None:
            if snapshot.spread_bps > self.trend_cfg.max_spread_bps:
                failures.append(
                    GateFailure(
                        GateFailureCategory.MICROSTRUCTURE,
                        (
                            f"spread {snapshot.spread_bps:.1f}bps > max "
                            f"{self.trend_cfg.max_spread_bps:.1f}bps"
                        ),
                    )
                )
            notes["spread_bps"] = snapshot.spread_bps
        elif snapshot.has_depth:
            failures.append(
                GateFailure(
                    GateFailureCategory.MICROSTRUCTURE,
                    "no spread observable (book empty)",
                )
            )

        # Bid-ask imbalance
        if snapshot.has_depth and snapshot.bid_ask_imbalance is not None:
            if snapshot.bid_ask_imbalance < self.trend_cfg.min_bid_ask_imbalance:
                failures.append(
                    GateFailure(
                        GateFailureCategory.MICROSTRUCTURE,
                        (
                            f"bid:ask imbalance {snapshot.bid_ask_imbalance:.2f} < min "
                            f"{self.trend_cfg.min_bid_ask_imbalance:.2f}"
                        ),
                    )
                )
            notes["bid_ask_imbalance"] = snapshot.bid_ask_imbalance

        # Tape buy %
        if snapshot.has_tape and snapshot.tape_buy_pct_60s is not None:
            if snapshot.tape_buy_pct_60s < self.trend_cfg.min_tape_buy_pct:
                failures.append(
                    GateFailure(
                        GateFailureCategory.MICROSTRUCTURE,
                        (
                            f"tape buy% {snapshot.tape_buy_pct_60s:.2f} < min "
                            f"{self.trend_cfg.min_tape_buy_pct:.2f}"
                        ),
                    )
                )
            notes["tape_buy_pct_60s"] = snapshot.tape_buy_pct_60s

        return not failures, failures, notes

    # ---- Utilities ----

    def _is_post_1030_et(self, ts: dt.datetime) -> bool:
        """True if `ts` is past 14:30 UTC (~10:30 ET, daylight-savings-agnostic
        for our hot zone). This is intentionally crude - we don't care about
        the 1-hour DST drift because the gate has a grace_bars buffer anyway.
        """
        if ts.tzinfo is None:
            return False
        ts_utc = ts.astimezone(dt.timezone.utc)
        return ts_utc.time() >= dt.time(14, 30)

    def _count_lower_highs(self) -> int:
        highs = self.backside_state.highs_history
        lookback = self.backside_cfg.lower_highs_lookback_bars
        if len(highs) < 2:
            return 0
        window = highs[-(lookback + 1) :]
        lower = 0
        for i in range(1, len(window)):
            if window[i] < window[i - 1]:
                lower += 1
        return lower
