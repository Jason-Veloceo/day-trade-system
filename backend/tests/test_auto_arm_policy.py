"""Pure-function tests for the auto-arm policy module.

Covers every gate in `decide()` and every branch in `is_engine_stale()`
so we can land changes to the gate stack with confidence the rejection
order doesn't drift.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from day_trade.auto_arm.policy import (
    AutoArmConfig,
    CandidateView,
    EngineStatusView,
    PolicyContext,
    RecentArm,
    decide,
    is_engine_stale,
    parse_window,
)


# ----- helpers -----


def _ts_et(et_str: str) -> dt.datetime:
    """`et_str` like '2026-06-30 09:35' (interpreted as US/Eastern).
    Returns a UTC-aware datetime."""
    from zoneinfo import ZoneInfo

    naive = dt.datetime.strptime(et_str, "%Y-%m-%d %H:%M")
    et = naive.replace(tzinfo=ZoneInfo("America/New_York"))
    return et.astimezone(dt.timezone.utc)


def _cfg(**overrides) -> AutoArmConfig:
    base = dict(
        enabled=True,
        widgets=("Momo",),
        strategy="first_pullback_long",
        quantity=100,
        order_type="LMT",
        limit_offset_cents=5.0,
        enable_depth=True,
        enable_tape=True,
        require_5m_macd=False,
        autonomous=False,
        max_daily_loss_usd=50.0,
        max_trades_per_run=3,
        max_position_value_usd=5000.0,
        max_position_qty=25000,
        window_start_et=dt.time(4, 0),
        window_end_et=dt.time(11, 30),
        max_per_day=10,
        max_per_hour=3,
        rearm_cooldown_minutes=30,
        stale_after_minutes=5,
        lookback_seconds=90.0,
        grace_period_seconds=120.0,
        poll_seconds=2.0,
    )
    base.update(overrides)
    return AutoArmConfig(**base)


def _cand(
    *,
    cid: int = 1,
    symbol: str = "JEM",
    status: str = "passed",
    widgets: tuple[str, ...] = ("Momo",),
    price: float | None = 5.00,
    last_alert_minutes_ago: float = 1.0,
    widget_last_alert_seconds_ago: float | None = 30.0,
    now: dt.datetime | None = None,
) -> CandidateView:
    """Default: last aggregate alert 1min ago, widget-specific 30s ago
    (both fresh w.r.t. the default 90s lookback / 5-min stale threshold).
    Pass `widget_last_alert_seconds_ago=None` to model a symbol that
    has never fired on a matching widget.
    """
    now = now or _ts_et("2026-06-30 09:35")
    widget_last = (
        None
        if widget_last_alert_seconds_ago is None
        else now - dt.timedelta(seconds=widget_last_alert_seconds_ago)
    )
    return CandidateView(
        id=cid,
        symbol=symbol,
        status=status,
        widgets_fired=widgets,
        last_close_price=Decimal(str(price)) if price is not None else None,
        last_alert_at=now - dt.timedelta(minutes=last_alert_minutes_ago),
        widget_specific_last_alert_at=widget_last,
    )


def _ctx(
    *,
    cfg: AutoArmConfig | None = None,
    now: dt.datetime | None = None,
    engines: tuple[EngineStatusView, ...] = (),
    arms: tuple[RecentArm, ...] = (),
    kill: bool = False,
) -> PolicyContext:
    return PolicyContext(
        config=cfg or _cfg(),
        now_utc=now or _ts_et("2026-06-30 09:35"),
        active_engines=engines,
        recent_arms=arms,
        portfolio_kill_switch_on=kill,
    )


# ----- decide() : happy path -----


def test_decide_arms_when_all_gates_pass() -> None:
    d = decide(_cand(), _ctx())
    assert d.action == "arm"
    assert d.reason == "all_gates_passed"


# ----- decide() : each gate -----


def test_decide_skips_when_disabled() -> None:
    d = decide(_cand(), _ctx(cfg=_cfg(enabled=False)))
    assert d.action == "skip"
    assert d.reason == "auto_arm_disabled"


def test_decide_skips_when_status_not_passed() -> None:
    d = decide(_cand(status="failed_filter"), _ctx())
    assert d.action == "skip"
    assert d.reason == "status_not_passed"


def test_decide_skips_when_no_matching_widget() -> None:
    d = decide(_cand(widgets=("Running_Up",)), _ctx())
    assert d.action == "skip"
    assert d.reason == "no_matching_widget"


def test_decide_arms_when_one_of_many_widgets_matches() -> None:
    d = decide(_cand(widgets=("Running_Up", "Momo", "OtherScanner")), _ctx())
    assert d.action == "arm"


def test_decide_skips_when_no_price() -> None:
    d = decide(_cand(price=None), _ctx())
    assert d.action == "skip"
    assert d.reason == "no_price"


def test_decide_skips_when_before_window() -> None:
    too_early = _ts_et("2026-06-30 03:30")  # before 04:00 ET
    d = decide(
        _cand(now=too_early, last_alert_minutes_ago=0.5),
        _ctx(now=too_early),
    )
    assert d.action == "skip"
    assert d.reason == "outside_window"


def test_decide_skips_when_after_window() -> None:
    too_late = _ts_et("2026-06-30 12:00")  # after 11:30 ET
    d = decide(
        _cand(now=too_late, last_alert_minutes_ago=0.5),
        _ctx(now=too_late),
    )
    assert d.action == "skip"
    assert d.reason == "outside_window"


def test_decide_skips_when_kill_switch() -> None:
    d = decide(_cand(), _ctx(kill=True))
    assert d.action == "skip"
    assert d.reason == "kill_switch_on"


def test_decide_skips_when_engine_already_running() -> None:
    engines = (
        EngineStatusView(run_id=42, symbol="JEM", has_open_position=False, was_auto_armed=False),
    )
    d = decide(_cand(symbol="JEM"), _ctx(engines=engines))
    assert d.action == "skip"
    assert d.reason == "engine_already_running"


def test_decide_skips_when_symbol_cooldown_active() -> None:
    now = _ts_et("2026-06-30 09:35")
    arms = (
        RecentArm(
            ts=now - dt.timedelta(minutes=10),
            symbol="JEM",
            action="arm",
        ),
    )
    d = decide(_cand(symbol="JEM", now=now), _ctx(now=now, arms=arms))
    assert d.action == "skip"
    assert d.reason == "symbol_cooldown"


def test_decide_arms_after_symbol_cooldown_elapses() -> None:
    now = _ts_et("2026-06-30 09:35")
    arms = (
        RecentArm(
            ts=now - dt.timedelta(minutes=45),  # > 30min cooldown
            symbol="JEM",
            action="arm",
        ),
    )
    d = decide(_cand(symbol="JEM", now=now), _ctx(now=now, arms=arms))
    assert d.action == "arm"


def test_decide_skips_when_hourly_limit_hit() -> None:
    now = _ts_et("2026-06-30 09:35")
    arms = tuple(
        RecentArm(
            ts=now - dt.timedelta(minutes=i * 10),
            symbol=f"SYM{i}",
            action="arm",
        )
        for i in range(1, 4)
    )
    d = decide(_cand(symbol="NEW", now=now), _ctx(now=now, arms=arms))
    assert d.action == "skip"
    assert d.reason == "hour_limit"


def test_decide_skips_when_daily_limit_hit() -> None:
    now = _ts_et("2026-06-30 11:00")
    # 10 arms spread out so hourly is fine but day cap is reached.
    arms = tuple(
        RecentArm(
            ts=now - dt.timedelta(minutes=60 + i * 30),
            symbol=f"SYM{i}",
            action="arm",
        )
        for i in range(10)
    )
    d = decide(_cand(symbol="NEW", now=now), _ctx(now=now, arms=arms))
    assert d.action == "skip"
    assert d.reason == "day_limit"


def test_decide_only_counts_arms_not_skips_for_rate_limit() -> None:
    """A history full of skip decisions should NOT consume the hourly or
    daily quota — only successful arms count."""
    now = _ts_et("2026-06-30 09:35")
    arms = tuple(
        RecentArm(
            ts=now - dt.timedelta(minutes=i * 5),
            symbol=f"SYM{i}",
            action="skip",  # not "arm"
        )
        for i in range(20)
    )
    d = decide(_cand(symbol="NEW", now=now), _ctx(now=now, arms=arms))
    assert d.action == "arm"


# ----- staleness -----


def _eng(
    *,
    run_id: int = 1,
    symbol: str = "JEM",
    has_open_position: bool = False,
    was_auto_armed: bool = True,
    armed_at: dt.datetime | None = None,
) -> EngineStatusView:
    return EngineStatusView(
        run_id=run_id,
        symbol=symbol,
        has_open_position=has_open_position,
        was_auto_armed=was_auto_armed,
        armed_at=armed_at,
    )


def test_staleness_never_stops_manual_arms() -> None:
    d = is_engine_stale(
        _eng(was_auto_armed=False),
        candidate=None,
        ctx=_ctx(),
    )
    assert not d.stop
    assert d.reason == "not_auto_armed"


def test_staleness_never_stops_engines_in_position() -> None:
    d = is_engine_stale(
        _eng(has_open_position=True),
        candidate=None,
        ctx=_ctx(),
    )
    assert not d.stop
    assert d.reason == "has_open_position"


def test_staleness_stops_when_candidate_row_missing_and_no_widget_history() -> None:
    """No Candidate row AND no scanner_events history for a matching widget →
    stop. `candidate_disappeared` used to be a distinct reason but has been
    subsumed by `no_widget_specific_alert` since we now consult scanner_events
    directly (see is_engine_stale docstring)."""
    d = is_engine_stale(_eng(), candidate=None, ctx=_ctx())
    assert d.stop
    assert d.reason == "no_widget_specific_alert"


def test_staleness_stops_when_scanner_went_cold() -> None:
    now = _ts_et("2026-06-30 09:35")
    cand = _cand(
        now=now,
        last_alert_minutes_ago=10.0,  # aggregate stale
        widget_last_alert_seconds_ago=600.0,  # widget-specific also stale (> 5min)
    )
    d = is_engine_stale(_eng(), candidate=cand, ctx=_ctx(now=now))
    assert d.stop
    assert d.reason == "widget_scanner_went_cold"


def test_staleness_keeps_fresh_engine() -> None:
    now = _ts_et("2026-06-30 09:35")
    cand = _cand(now=now, last_alert_minutes_ago=2.0)  # < 5min
    d = is_engine_stale(_eng(), candidate=cand, ctx=_ctx(now=now))
    assert not d.stop
    assert d.reason == "fresh"


# ----- staleness: grace period -----


def test_staleness_grace_period_blocks_stop_when_candidate_disappeared() -> None:
    """An engine armed 30 seconds ago must NOT be killed by the
    staleness watcher even if the underlying candidate disappeared,
    so that bootstrap and the first trigger evaluation get a fair
    shot. Regression test for the "armed and killed within 12s" bug."""
    now = _ts_et("2026-06-30 09:35")
    eng = _eng(armed_at=now - dt.timedelta(seconds=30))
    d = is_engine_stale(eng, candidate=None, ctx=_ctx(now=now))
    assert not d.stop
    assert d.reason == "in_grace_period"


def test_staleness_grace_period_blocks_stop_when_scanner_cold() -> None:
    """Same as above, but with a candidate that would otherwise be
    deemed stale. The grace period overrides the cold-scanner stop."""
    now = _ts_et("2026-06-30 09:35")
    cand = _cand(
        now=now,
        last_alert_minutes_ago=10.0,
        widget_last_alert_seconds_ago=600.0,  # would normally be widget-stale
    )
    eng = _eng(armed_at=now - dt.timedelta(seconds=60))
    d = is_engine_stale(eng, candidate=cand, ctx=_ctx(now=now))
    assert not d.stop
    assert d.reason == "in_grace_period"


def test_staleness_grace_period_expires_after_threshold() -> None:
    """Once the grace period (default 120s) elapses, normal staleness
    rules apply again."""
    now = _ts_et("2026-06-30 09:35")
    cand = _cand(
        now=now,
        last_alert_minutes_ago=10.0,
        widget_last_alert_seconds_ago=600.0,
    )
    eng = _eng(armed_at=now - dt.timedelta(seconds=150))  # > 120s grace
    d = is_engine_stale(eng, candidate=cand, ctx=_ctx(now=now))
    assert d.stop
    assert d.reason == "widget_scanner_went_cold"


def test_staleness_grace_period_skipped_when_armed_at_missing() -> None:
    """Manual arms / legacy engines without armed_at must still be
    subject to staleness checks (was_auto_armed=False excludes them
    earlier, but defensively the grace gate must no-op when
    armed_at is None)."""
    now = _ts_et("2026-06-30 09:35")
    cand = _cand(
        now=now,
        last_alert_minutes_ago=10.0,
        widget_last_alert_seconds_ago=600.0,
    )
    eng = _eng(armed_at=None)  # explicit
    d = is_engine_stale(eng, candidate=cand, ctx=_ctx(now=now))
    assert d.stop
    assert d.reason == "widget_scanner_went_cold"


# ----- widget-specific freshness gate (arm & staleness symmetry) -----


def test_decide_skips_when_widget_specific_alert_missing() -> None:
    """Candidate has historically fired on Momo but the query returned
    no widget-specific ts (e.g. never fired on a configured widget in
    the observable ScannerEvent history)."""
    now = _ts_et("2026-06-30 09:35")
    cand = _cand(
        now=now,
        widgets=("Momo", "Running_Up"),
        widget_last_alert_seconds_ago=None,
        last_alert_minutes_ago=0.5,  # Running_Up firing constantly
    )
    d = decide(cand, _ctx(now=now))
    assert d.action == "skip"
    assert d.reason == "no_widget_specific_alert"


def test_decide_skips_when_widget_alert_older_than_lookback() -> None:
    """TC-bug reproducer: Momo fired 40min ago, Running_Up still firing.
    Aggregate `last_alert_at` looks fresh (via Running_Up) but the Momo-
    specific ts is way beyond lookback_seconds. Must be a skip so the
    engine isn't armed only to be killed by the staleness watcher."""
    now = _ts_et("2026-06-30 09:35")
    cand = _cand(
        now=now,
        widgets=("Momo", "Running_Up"),
        widget_last_alert_seconds_ago=2400.0,  # 40min ago on Momo
        last_alert_minutes_ago=0.05,  # Running_Up 3s ago
    )
    d = decide(cand, _ctx(now=now))
    assert d.action == "skip"
    assert d.reason == "widget_alert_stale"
    assert "widget_last_alert_age_s=2400" in d.detail
    assert "lookback_s=90" in d.detail


def test_decide_arms_on_fresh_widget_specific_alert() -> None:
    """A candidate whose Momo fired 30s ago should still be arm-able even
    with older widgets_fired history."""
    now = _ts_et("2026-06-30 09:35")
    cand = _cand(now=now, widget_last_alert_seconds_ago=30.0)
    d = decide(cand, _ctx(now=now))
    assert d.action == "arm"


def test_staleness_stops_when_widget_alert_absent() -> None:
    """Symbol currently has a live candidate row (Running_Up firing) but
    no Momo alerts. Widget-specific ts is None so the Momo-scanner has
    'gone cold' from our POV and the engine should be stopped."""
    now = _ts_et("2026-06-30 09:35")
    cand = _cand(
        now=now,
        widgets=("Running_Up",),  # never fired on Momo
        widget_last_alert_seconds_ago=None,
        last_alert_minutes_ago=0.5,
    )
    # Engine past its grace period
    eng = _eng(armed_at=now - dt.timedelta(seconds=200))
    d = is_engine_stale(eng, candidate=cand, ctx=_ctx(now=now))
    assert d.stop
    assert d.reason == "no_widget_specific_alert"


def test_staleness_keeps_engine_when_widget_alert_fresh() -> None:
    """As long as Momo fires within the staleness window, the engine
    survives even after grace period expires."""
    now = _ts_et("2026-06-30 09:35")
    cand = _cand(
        now=now,
        widget_last_alert_seconds_ago=60.0,  # 1min < 5min threshold
        last_alert_minutes_ago=1.0,
    )
    eng = _eng(armed_at=now - dt.timedelta(seconds=200))  # past grace
    d = is_engine_stale(eng, candidate=cand, ctx=_ctx(now=now))
    assert not d.stop
    assert d.reason == "fresh"


# ----- helper functions -----


def test_parse_window_accepts_HHMM() -> None:
    assert parse_window("04:00") == dt.time(4, 0)
    assert parse_window("11:30") == dt.time(11, 30)
    assert parse_window(" 09:45 ") == dt.time(9, 45)


def test_parse_window_rejects_malformed() -> None:
    with pytest.raises(ValueError):
        parse_window("not-a-time")
    with pytest.raises(ValueError):
        parse_window("4:00 PM")
