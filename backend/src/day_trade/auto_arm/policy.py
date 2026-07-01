"""Pure decision logic for the auto-arm worker.

These functions are deliberately free of DB / async / registry
coupling. The worker handles all I/O; this module just answers two
questions:

  - decide(candidate, ctx)        -> should we arm this candidate now?
  - is_engine_stale(engine, ctx)  -> should we auto-stop this engine?

Keeping them pure means we can unit-test every gate path without
spinning up Postgres or IBKR.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


# ---------------- value objects ----------------


@dataclass(frozen=True, slots=True)
class AutoArmConfig:
    """Snapshot of the operator's auto-arm configuration. Built from
    `Settings` at worker start; re-read on every poll so .env edits
    can take effect without a restart (within the lifespan of the
    process)."""

    enabled: bool
    widgets: tuple[str, ...]
    strategy: str
    quantity: int
    order_type: str
    limit_offset_cents: float
    enable_depth: bool
    enable_tape: bool
    require_5m_macd: bool
    autonomous: bool
    max_daily_loss_usd: float
    max_trades_per_run: int
    max_position_value_usd: float
    max_position_qty: int
    window_start_et: dt.time
    window_end_et: dt.time
    max_per_day: int
    max_per_hour: int
    rearm_cooldown_minutes: int
    stale_after_minutes: int
    # Only consider candidates with last_alert_at newer than this many
    # seconds when deciding to arm. Must be < stale_after_minutes*60.
    lookback_seconds: float
    # Staleness watcher will not kill engines younger than this.
    grace_period_seconds: float
    poll_seconds: float


@dataclass(frozen=True, slots=True)
class CandidateView:
    """The subset of a candidates row this module needs.

    Note the two freshness fields:
      - `last_alert_at`             — aggregate across ALL widgets. Used
        for UI and generic "is this candidate still moving?" queries.
      - `widget_specific_last_alert_at` — max ts of alerts that fired on
        a widget IN cfg.widgets. This is what BOTH arm and staleness
        decisions use, so the two policies stay in agreement: we arm on
        fresh Momo activity and only kill on stale Momo activity.
        None if the symbol has NEVER fired on a configured widget.
    """

    id: int
    symbol: str
    status: str  # "passed" | "failed_filter" | ...
    widgets_fired: tuple[str, ...]
    last_close_price: Decimal | None
    last_alert_at: dt.datetime
    widget_specific_last_alert_at: dt.datetime | None = None


@dataclass(frozen=True, slots=True)
class EngineStatusView:
    """The subset of a running engine this module needs."""

    run_id: int
    symbol: str
    has_open_position: bool
    was_auto_armed: bool
    # When the worker armed this engine. Used by the staleness watcher
    # to enforce a minimum grace period before staleness can fire.
    # None for manually-armed engines (their staleness gate exits
    # early on `not_auto_armed` anyway).
    armed_at: dt.datetime | None = None


@dataclass(frozen=True, slots=True)
class RecentArm:
    """An auto-arm decision (armed or skipped) that happened recently
    and is relevant to current rate-limit / dedupe calculations."""

    ts: dt.datetime  # UTC
    symbol: str
    action: str  # "arm" | "skip"


@dataclass(frozen=True, slots=True)
class PolicyContext:
    """All the state `decide` needs, gathered by the worker."""

    config: AutoArmConfig
    now_utc: dt.datetime
    active_engines: tuple[EngineStatusView, ...]
    recent_arms: tuple[RecentArm, ...]
    portfolio_kill_switch_on: bool


@dataclass(frozen=True, slots=True)
class Decision:
    action: str  # "arm" | "skip"
    reason: str  # short, machine-parseable code
    detail: str = ""  # optional human-readable extras


@dataclass(frozen=True, slots=True)
class StalenessDecision:
    stop: bool
    reason: str
    detail: str = ""


# ---------------- helpers ----------------


def parse_window(value: str) -> dt.time:
    """Parse "HH:MM" into a dt.time. Raises ValueError on malformed
    input. Used by the worker when building AutoArmConfig from Settings.
    """
    hh, mm = value.strip().split(":")
    return dt.time(hour=int(hh), minute=int(mm))


def _is_within_window(now_utc: dt.datetime, start_et: dt.time, end_et: dt.time) -> bool:
    """Returns True iff the current US/Eastern wall-clock time-of-day
    is within [start_et, end_et]. The window is interpreted on the
    current ET trading day; cross-midnight windows aren't supported
    (auto-arm windows are always intraday)."""
    now_et = now_utc.astimezone(ET).time()
    if start_et <= end_et:
        return start_et <= now_et <= end_et
    # Defensive: if someone configures a wrap-around window, treat as
    # "outside" rather than crash.
    return False


def _et_date(now_utc: dt.datetime) -> dt.date:
    return now_utc.astimezone(ET).date()


# ---------------- decide(): arm vs skip ----------------


def decide(candidate: CandidateView, ctx: PolicyContext) -> Decision:
    """Walk the gate stack and return either ("arm", reason) or
    ("skip", reason). Gates are checked in order from cheapest to
    most expensive so we short-circuit on the most common rejections
    first.
    """
    cfg = ctx.config

    if not cfg.enabled:
        return Decision("skip", "auto_arm_disabled")

    if candidate.status != "passed":
        return Decision("skip", "status_not_passed", f"status={candidate.status!r}")

    widget_set = set(cfg.widgets)
    matching = [w for w in candidate.widgets_fired if w in widget_set]
    if not matching:
        return Decision(
            "skip",
            "no_matching_widget",
            f"widgets_fired={list(candidate.widgets_fired)} cfg.widgets={list(cfg.widgets)}",
        )

    # Widget-specific freshness. A candidate might have fired on Momo
    # HOURS ago but still look "fresh" via the aggregate last_alert_at
    # (because Running_Up is currently firing). We only want to arm on
    # symbols with a RECENT hit on one of the configured widgets — this
    # is symmetric with the staleness watchdog's later check.
    if candidate.widget_specific_last_alert_at is None:
        return Decision(
            "skip",
            "no_widget_specific_alert",
            f"widgets_fired_ever={list(candidate.widgets_fired)} "
            f"cfg.widgets={list(cfg.widgets)}",
        )
    widget_age = ctx.now_utc - candidate.widget_specific_last_alert_at
    if widget_age.total_seconds() > cfg.lookback_seconds:
        return Decision(
            "skip",
            "widget_alert_stale",
            f"widget_last_alert_age_s={widget_age.total_seconds():.0f} "
            f"lookback_s={cfg.lookback_seconds:.0f}",
        )

    if candidate.last_close_price is None:
        return Decision("skip", "no_price")

    if not _is_within_window(ctx.now_utc, cfg.window_start_et, cfg.window_end_et):
        now_et = ctx.now_utc.astimezone(ET).time().isoformat(timespec="seconds")
        return Decision(
            "skip",
            "outside_window",
            f"now_et={now_et} window={cfg.window_start_et}-{cfg.window_end_et}",
        )

    if ctx.portfolio_kill_switch_on:
        return Decision("skip", "kill_switch_on")

    # Already a running engine for this symbol? Don't double-arm.
    for e in ctx.active_engines:
        if e.symbol == candidate.symbol:
            return Decision("skip", "engine_already_running", f"run_id={e.run_id}")

    # Per-symbol re-arm cooldown. 0 = disabled.
    if cfg.rearm_cooldown_minutes > 0:
        cooldown = dt.timedelta(minutes=cfg.rearm_cooldown_minutes)
        symbol_arms = [a for a in ctx.recent_arms if a.symbol == candidate.symbol and a.action == "arm"]
        if symbol_arms:
            last = max(a.ts for a in symbol_arms)
            age = ctx.now_utc - last
            if age < cooldown:
                return Decision(
                    "skip",
                    "symbol_cooldown",
                    f"last_arm_age_min={age.total_seconds()/60:.1f} cooldown_min={cfg.rearm_cooldown_minutes}",
                )

    # Per-hour rate limit. 0 = disabled (unlimited).
    if cfg.max_per_hour > 0:
        hour_ago = ctx.now_utc - dt.timedelta(hours=1)
        hour_arm_count = sum(1 for a in ctx.recent_arms if a.action == "arm" and a.ts >= hour_ago)
        if hour_arm_count >= cfg.max_per_hour:
            return Decision(
                "skip",
                "hour_limit",
                f"arms_last_hour={hour_arm_count} max={cfg.max_per_hour}",
            )

    # Per-day (ET) rate limit. 0 = disabled (unlimited).
    if cfg.max_per_day > 0:
        today_et = _et_date(ctx.now_utc)
        day_arm_count = sum(
            1
            for a in ctx.recent_arms
            if a.action == "arm" and _et_date(a.ts) == today_et
        )
        if day_arm_count >= cfg.max_per_day:
            return Decision(
                "skip",
                "day_limit",
                f"arms_today={day_arm_count} max={cfg.max_per_day}",
            )

    return Decision("arm", "all_gates_passed", f"widget={matching[0]}")


# ---------------- is_engine_stale(): stop an idle auto-armed engine ----------------


def is_engine_stale(
    engine: EngineStatusView,
    candidate: CandidateView | None,
    ctx: PolicyContext,
) -> StalenessDecision:
    """Return whether `engine` should be auto-stopped due to scanner
    staleness. Rules:

      - Manual arms are never stopped by this watcher.
      - Engines that hold an open position are never stopped here;
        the exit-trigger framework owns those decisions.
      - If the underlying candidate row has disappeared (cooldown
        expired and no recent alerts), stop.
      - If the candidate's `last_alert_at` is older than the configured
        staleness window, stop.

    The worker only acts on StalenessDecision(stop=True). The reason
    string is journaled on the engine_stop event for post-mortem.
    """
    if not engine.was_auto_armed:
        return StalenessDecision(False, "not_auto_armed")

    if engine.has_open_position:
        return StalenessDecision(False, "has_open_position")

    # Grace period: give every freshly auto-armed engine a guaranteed
    # minimum runtime to bootstrap, evaluate gates, and (potentially)
    # take a trade. Without this, an engine armed on a candidate that
    # was already near-stale could be killed within seconds — the
    # original "armed and killed within 12s" bug. The grace period
    # also covers the case where we accidentally armed on a stale
    # alert and the scanner is still firing (just hasn't fired again
    # yet).
    if engine.armed_at is not None:
        age_since_arm = ctx.now_utc - engine.armed_at
        grace = dt.timedelta(seconds=ctx.config.grace_period_seconds)
        if age_since_arm < grace:
            return StalenessDecision(
                False,
                "in_grace_period",
                f"age_since_arm_s={age_since_arm.total_seconds():.0f} grace_s={ctx.config.grace_period_seconds:.0f}",
            )

    # Widget-specific freshness is the SINGLE source of truth for
    # staleness. We deliberately do NOT branch on `candidate is None`
    # here (previous versions did, which caused "candidate_disappeared"
    # false-positives whenever the Candidate row's administrative
    # cooldown_until expired mid-flow even though scanner_events kept
    # firing on the symbol). The worker is responsible for computing
    # widget_specific_last_alert_at from scanner_events regardless of
    # whether the Candidate row is still active.
    threshold = dt.timedelta(minutes=ctx.config.stale_after_minutes)
    widget_last = (
        candidate.widget_specific_last_alert_at if candidate is not None else None
    )
    if widget_last is None:
        return StalenessDecision(
            True,
            "no_widget_specific_alert",
            f"cfg.widgets={list(ctx.config.widgets)}",
        )
    widget_age = ctx.now_utc - widget_last
    if widget_age > threshold:
        return StalenessDecision(
            True,
            "widget_scanner_went_cold",
            f"widget_last_alert_age_min={widget_age.total_seconds()/60:.1f} "
            f"threshold_min={ctx.config.stale_after_minutes}",
        )

    return StalenessDecision(False, "fresh")
