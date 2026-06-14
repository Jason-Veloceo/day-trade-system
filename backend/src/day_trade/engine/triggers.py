"""Entry-trigger detection (Ross-style).

This module owns the per-bar pattern recognition that says "this is the
candle to enter on". It is intentionally separate from the trend/backside/
microstructure GATES - the gates ask "should we be willing to consider an
entry at all" and the trigger asks "did the pattern just complete".

The two implemented triggers:

  detect_pullback_break(...)
    The classic micro/first-pullback breakout. Walks back through the
    recent 1m bar history, finds the most recent contiguous red-candle
    run, treats it as the pullback, and fires when the CURRENT bar is
    green AND its high exceeds the high of the LAST (most recent) red
    candle in the pullback.

    Example (left = oldest, right = newest):

        G G G R R G G                  <- history
                    ^
                    current bar (green)

    The pullback is the two R bars. The "test high" is the second R's
    high. If the current G's high > that test high, the trigger fires.
    The intermediate green bars between the pullback and the current bar
    are allowed up to `max_bars_since_pullback_end`. The Ross convention
    is to look at the SMALLER (more recent) red, which here means the
    last red in the run.

  detect_macd_cross_up(...)
    The simpler indicator-only trigger that the POC originally shipped.
    Fires when the 1m MACD histogram either just crossed >=0 from <0 or
    is positive and increasing. Used as an alternative trigger for the
    `macd_cross` mode.

Both triggers return a frozen `TriggerResult`; the strategy hosts no
state for them beyond what it passes in.
"""

from __future__ import annotations

from dataclasses import dataclass

from .strategies.base import Bar


@dataclass(frozen=True, slots=True)
class TriggerResult:
    fired: bool
    reason: str
    mode: str
    # pullback_break specifics
    pullback_test_high: float | None = None
    pullback_low: float | None = None
    pullback_bar_count: int = 0
    impulse_bar_count: int = 0
    # macd_cross specifics
    crossed_up: bool | None = None
    positive_and_rising: bool | None = None


@dataclass(frozen=True, slots=True)
class PullbackBreakConfig:
    """All knobs for `detect_pullback_break`. Every field is surfaced in the
    strategy's snapshot so the UI can show the active values."""

    # Pullback shape constraints.
    min_pullback_bars: int = 1
    max_pullback_bars: int = 3
    # How many green bars are allowed between the pullback end and the
    # current breakout candle. 0 means the current candle must immediately
    # follow the last red. The user's NVFY example needs >= 1 (one green
    # candle attempted the break, the second green succeeded).
    max_bars_since_pullback_end: int = 3

    # Impulse: how many green bars must precede the pullback.
    require_impulse: bool = True
    min_impulse_bars: int = 1

    # The break test is `current.high > test_high`. We also support strict
    # ">" or non-strict ">=" via this knob; default ">".
    strict_break: bool = True


def detect_pullback_break(
    *,
    current_bar: Bar,
    history: list[Bar],
    config: PullbackBreakConfig | None = None,
) -> TriggerResult:
    """Ross-style micro/first-pullback breakout detector.

    `history` is the list of CLOSED 1m bars in chronological order, NOT
    including `current_bar`. The caller (strategy) maintains this buffer.
    """
    cfg = config or PullbackBreakConfig()

    # ---- gate the current bar itself: must be green ----
    if not _is_green(current_bar):
        return TriggerResult(
            fired=False,
            reason="current bar is not green (close <= open)",
            mode="pullback_break",
        )

    if not history:
        return TriggerResult(
            fired=False,
            reason="no history yet",
            mode="pullback_break",
        )

    # ---- walk back through history to find the most recent red run ----
    # Skip up to `max_bars_since_pullback_end` consecutive green bars first.
    green_skip = 0
    last_red_idx = -1
    for i in range(len(history) - 1, -1, -1):
        b = history[i]
        if _is_red(b):
            last_red_idx = i
            break
        if _is_green(b) or _is_doji(b):
            green_skip += 1
            if green_skip > cfg.max_bars_since_pullback_end:
                return TriggerResult(
                    fired=False,
                    reason=(
                        f"no pullback within last {cfg.max_bars_since_pullback_end} "
                        "bars before current"
                    ),
                    mode="pullback_break",
                )

    if last_red_idx == -1:
        return TriggerResult(
            fired=False,
            reason="no red bars in history within lookback",
            mode="pullback_break",
        )

    # ---- extend backward through contiguous red bars ----
    first_red_idx = last_red_idx
    while first_red_idx > 0 and _is_red(history[first_red_idx - 1]):
        first_red_idx -= 1

    pullback_bars = history[first_red_idx : last_red_idx + 1]
    n_pullback = len(pullback_bars)
    if n_pullback < cfg.min_pullback_bars:
        return TriggerResult(
            fired=False,
            reason=f"pullback too short ({n_pullback} < {cfg.min_pullback_bars})",
            mode="pullback_break",
            pullback_bar_count=n_pullback,
        )
    if n_pullback > cfg.max_pullback_bars:
        return TriggerResult(
            fired=False,
            reason=f"pullback too long ({n_pullback} > {cfg.max_pullback_bars})",
            mode="pullback_break",
            pullback_bar_count=n_pullback,
        )

    # ---- impulse check ----
    impulse_count = 0
    if cfg.require_impulse:
        for j in range(first_red_idx - 1, -1, -1):
            b = history[j]
            if _is_green(b):
                impulse_count += 1
            else:
                break
        if impulse_count < cfg.min_impulse_bars:
            return TriggerResult(
                fired=False,
                reason=(
                    f"insufficient green impulse before pullback "
                    f"({impulse_count} < {cfg.min_impulse_bars})"
                ),
                mode="pullback_break",
                pullback_bar_count=n_pullback,
                impulse_bar_count=impulse_count,
            )

    # ---- evaluate the break ----
    # Ross convention: test against the LAST red in the run (the "smaller"
    # / more recent red, closest to the current bar).
    test_high = pullback_bars[-1].high
    pullback_low = min(b.low for b in pullback_bars)
    breaks = (
        current_bar.high > test_high if cfg.strict_break else current_bar.high >= test_high
    )

    if not breaks:
        return TriggerResult(
            fired=False,
            reason=(
                f"current high {current_bar.high:.4f} did not exceed "
                f"last-red-pullback high {test_high:.4f}"
            ),
            mode="pullback_break",
            pullback_test_high=test_high,
            pullback_low=pullback_low,
            pullback_bar_count=n_pullback,
            impulse_bar_count=impulse_count,
        )

    return TriggerResult(
        fired=True,
        reason=(
            f"green bar broke last-red-pullback high {test_high:.4f} "
            f"(current high {current_bar.high:.4f}); pullback={n_pullback} bars, "
            f"impulse={impulse_count} bars"
        ),
        mode="pullback_break",
        pullback_test_high=test_high,
        pullback_low=pullback_low,
        pullback_bar_count=n_pullback,
        impulse_bar_count=impulse_count,
    )


def detect_macd_cross_up(
    *,
    histogram: float | None,
    histogram_prev: float | None,
) -> TriggerResult:
    """1m MACD histogram cross-up trigger. Fires when histogram either:
      - just crossed from <= 0 to > 0 (the "cross up"), OR
      - is already positive and strictly increasing.

    `None` inputs mean MACD hasn't warmed up yet.
    """
    if histogram is None or histogram_prev is None:
        return TriggerResult(
            fired=False,
            reason="MACD not warmed up yet",
            mode="macd_cross",
            crossed_up=False,
            positive_and_rising=False,
        )
    crossed_up = histogram_prev <= 0 < histogram
    positive_and_rising = histogram_prev > 0 and histogram > histogram_prev
    fired = crossed_up or positive_and_rising
    return TriggerResult(
        fired=fired,
        reason=(
            f"macd_cross trigger fired (crossed_up={crossed_up}, "
            f"positive_and_rising={positive_and_rising})"
            if fired
            else f"MACD neither crossing up nor positive-and-rising "
            f"({histogram_prev:.6f} -> {histogram:.6f})"
        ),
        mode="macd_cross",
        crossed_up=crossed_up,
        positive_and_rising=positive_and_rising,
    )


# ---------- helpers ----------


def _is_green(b: Bar) -> bool:
    return b.close > b.open


def _is_red(b: Bar) -> bool:
    return b.close < b.open


def _is_doji(b: Bar) -> bool:
    return b.close == b.open
