"""Background worker that drives the auto-arm and staleness loops.

The worker is a single async task spawned from the FastAPI lifespan.
Every `poll_seconds` it:

  1. Re-reads the AutoArmConfig from Settings (so .env edits during a
     trading session take effect without a restart).
  2. Cheap early-exit if `enabled=False`.
  3. Builds a PolicyContext from the DB (candidates, recent auto-arms)
     and the in-process registry (active engines, portfolio caps).
  4. For each candidate matching the widget filter, calls `decide()`
     and arms via `registry.start()` if the gates pass.
  5. For each running auto-armed engine, calls `is_engine_stale()`
     and stops it if the scanner has gone cold.

Every arm/skip/stop decision is journaled into the database. Arm
decisions also persist via the new EngineRun row (`dtd_context.auto_armed=true`),
which is the source of truth queried by step 3's recent_arms.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from contextlib import suppress
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select

from day_trade.auto_arm.policy import (
    AutoArmConfig,
    CandidateView,
    Decision,
    EngineStatusView,
    PolicyContext,
    RecentArm,
    StalenessDecision,
    decide,
    is_engine_stale,
    parse_window,
)
from day_trade.config import Settings, get_settings
from day_trade.db.models import Candidate, EngineRun, ScannerEvent
from day_trade.db.session import session_scope
from day_trade.engine.registry import (
    EngineAlreadyRunningError,
    EngineRegistry,
    EngineSlotFullError,
    get_registry,
)
from day_trade.engine.risk import RiskCaps

logger = logging.getLogger(__name__)


# ---------------- config snapshot ----------------


def build_config(settings: Settings) -> AutoArmConfig:
    """Snapshot the operator's auto-arm settings into an immutable
    config. Run at every poll so .env edits take effect."""
    try:
        start = parse_window(settings.auto_arm_window_start_et)
        end = parse_window(settings.auto_arm_window_end_et)
    except ValueError:
        logger.exception(
            "invalid auto_arm window (start=%r end=%r); falling back to 04:00-11:30 ET",
            settings.auto_arm_window_start_et,
            settings.auto_arm_window_end_et,
        )
        start, end = dt.time(4, 0), dt.time(11, 30)
    return AutoArmConfig(
        enabled=settings.auto_arm_enabled,
        widgets=tuple(settings.auto_arm_widget_list),
        strategy=settings.auto_arm_strategy,
        quantity=settings.auto_arm_quantity,
        order_type=settings.auto_arm_order_type,
        limit_offset_cents=settings.auto_arm_limit_offset_cents,
        enable_depth=settings.auto_arm_enable_depth,
        enable_tape=settings.auto_arm_enable_tape,
        require_5m_macd=settings.auto_arm_require_5m_macd,
        autonomous=settings.auto_arm_autonomous,
        max_daily_loss_usd=settings.auto_arm_max_daily_loss_usd,
        max_trades_per_run=settings.auto_arm_max_trades_per_run,
        max_position_value_usd=settings.auto_arm_max_position_value_usd,
        max_position_qty=settings.auto_arm_max_position_qty,
        window_start_et=start,
        window_end_et=end,
        max_per_day=settings.auto_arm_max_per_day,
        max_per_hour=settings.auto_arm_max_per_hour,
        rearm_cooldown_minutes=settings.auto_arm_rearm_cooldown_minutes,
        stale_after_minutes=settings.auto_arm_stale_after_minutes,
        lookback_seconds=settings.auto_arm_lookback_seconds,
        grace_period_seconds=settings.auto_arm_grace_period_seconds,
        poll_seconds=settings.auto_arm_poll_seconds,
    )


# ---------------- worker ----------------


class AutoArmWorker:
    """Background task that polls and orchestrates auto-arm decisions.

    Lifecycle:
      - construct once (per FastAPI process)
      - `await start()` from the lifespan startup
      - `await stop()` from the lifespan shutdown

    Designed to be resilient: any exception from a single tick is
    logged and swallowed so the worker keeps running. The loop sleeps
    on an `asyncio.Event` so shutdown is immediate (no waiting for the
    next poll).
    """

    def __init__(
        self,
        *,
        registry: EngineRegistry | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._registry = registry or get_registry()
        self._settings = settings or get_settings()
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._last_tick_at: dt.datetime | None = None
        self._last_decision_count: int = 0
        # In-memory record of when this worker armed each symbol. Used
        # by the staleness watcher to enforce a minimum grace period
        # so freshly-armed engines aren't killed before they bootstrap.
        # Keyed by (symbol, run_id) so re-arms after a stop don't
        # carry stale timestamps.
        self._armed_at: dict[tuple[str, int], dt.datetime] = {}

    # ---- lifecycle ----

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="auto_arm_worker")
        logger.info("AutoArmWorker started")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("AutoArmWorker stopped")

    # ---- introspection (for /auto_arm/status endpoint, future) ----

    def snapshot(self) -> dict[str, Any]:
        cfg = build_config(self._settings)
        return {
            "enabled": cfg.enabled,
            "widgets": list(cfg.widgets),
            "window_et": f"{cfg.window_start_et.isoformat(timespec='minutes')}"
                          f"-{cfg.window_end_et.isoformat(timespec='minutes')}",
            "autonomous": cfg.autonomous,
            "max_per_day": cfg.max_per_day,
            "max_per_hour": cfg.max_per_hour,
            "rearm_cooldown_minutes": cfg.rearm_cooldown_minutes,
            "stale_after_minutes": cfg.stale_after_minutes,
            "poll_seconds": cfg.poll_seconds,
            "last_tick_at": self._last_tick_at.isoformat() if self._last_tick_at else None,
            "last_tick_decisions": self._last_decision_count,
            "running": self._task is not None and not self._task.done(),
        }

    # ---- loop ----

    async def _run(self) -> None:
        # First tick happens immediately so the operator sees activity
        # right after enabling. Subsequent ticks honour the poll cadence.
        while not self._stop_event.is_set():
            cfg = build_config(self._settings)
            try:
                await self._tick(cfg)
            except Exception:
                logger.exception("AutoArmWorker tick failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=max(0.5, cfg.poll_seconds)
                )
            except asyncio.TimeoutError:
                continue

    async def _tick(self, cfg: AutoArmConfig) -> None:
        self._last_tick_at = dt.datetime.now(dt.timezone.utc)
        self._last_decision_count = 0

        # Cheap exit when disabled. We still re-read every tick so flipping
        # the env var is responsive.
        if not cfg.enabled:
            return

        ctx = await self._build_context(cfg)
        decisions = await self._evaluate_candidates(ctx)
        await self._evaluate_staleness(ctx)
        self._last_decision_count = decisions

    # ---- context assembly ----

    async def _build_context(self, cfg: AutoArmConfig) -> PolicyContext:
        now_utc = dt.datetime.now(dt.timezone.utc)
        active = await self._fetch_active_engines()
        recent = await self._fetch_recent_arms(now_utc)
        kill = bool(
            self._registry.portfolio_risk.snapshot().get("kill_switch_on")
        )
        return PolicyContext(
            config=cfg,
            now_utc=now_utc,
            active_engines=tuple(active),
            recent_arms=tuple(recent),
            portfolio_kill_switch_on=kill,
        )

    async def _fetch_active_engines(self) -> list[EngineStatusView]:
        out: list[EngineStatusView] = []
        active_keys: set[tuple[str, int]] = set()
        for e in self._registry.active():
            ctx = dict(e.config.dtd_context or {})
            was_auto = bool(ctx.get("auto_armed"))
            qty = e.risk.state.open_position_qty if e.risk else 0
            # A pending BUY (submitted, not yet fully filled) is
            # ALSO reason enough not to stop the engine. Without
            # this, the auto-arm staleness watchdog can kill an
            # engine in the seconds between `order_submit` and
            # the position-size update from `_on_entry_fill`,
            # leaving an orphaned position with no exit framework.
            # See TC 2026-07-01 incident.
            has_pending = getattr(e, "_pending_entry", None) is not None
            run_id = e.run_id or 0
            key = (e.config.symbol, run_id)
            active_keys.add(key)
            armed_at = self._armed_at.get(key) if was_auto else None
            out.append(
                EngineStatusView(
                    run_id=run_id,
                    symbol=e.config.symbol,
                    has_open_position=(qty > 0) or has_pending,
                    was_auto_armed=was_auto,
                    armed_at=armed_at,
                )
            )
        # Garbage-collect armed_at entries for engines that have
        # stopped (whether by us or manually). Otherwise this dict
        # would grow indefinitely.
        for key in list(self._armed_at.keys()):
            if key not in active_keys:
                self._armed_at.pop(key, None)
        return out

    async def _fetch_recent_arms(self, now_utc: dt.datetime) -> list[RecentArm]:
        """Query engine_runs for auto-armed runs in the last 24 hours."""
        since = now_utc - dt.timedelta(hours=24)
        async with session_scope() as s:
            rows = (
                await s.execute(
                    select(EngineRun.symbol, EngineRun.started_at, EngineRun.dtd_context)
                    .where(
                        EngineRun.started_at >= since,
                        EngineRun.dtd_context.contains({"auto_armed": True}),
                    )
                )
            ).all()
        return [
            RecentArm(ts=r.started_at, symbol=r.symbol, action="arm")
            for r in rows
        ]

    async def _widget_specific_last_alert_at(
        self,
        session,
        symbols: list[str],
        widgets: tuple[str, ...],
    ) -> dict[str, dt.datetime]:
        """Return {symbol: MAX(ts)} for scanner_events where widget IN cfg.widgets.

        Symbols with no matching-widget alerts (ever) are absent from the
        dict — the caller interprets missing as "no widget-specific alert",
        which both `decide()` and `is_engine_stale()` treat as a skip / stop.

        This is the key freshness signal that keeps arm and staleness
        symmetric: both sides consult the same per-widget history rather
        than the aggregate candidate `last_alert_at` (which is polluted
        by other widgets firing on the same ticker).
        """
        if not symbols or not widgets:
            return {}
        stmt = (
            select(ScannerEvent.symbol, func.max(ScannerEvent.ts))
            .where(
                ScannerEvent.symbol.in_(symbols),
                ScannerEvent.widget.in_(list(widgets)),
            )
            .group_by(ScannerEvent.symbol)
        )
        rows = (await session.execute(stmt)).all()
        return {sym: ts for sym, ts in rows}

    async def _fetch_recent_passed_candidates(
        self, lookback_seconds: float, widgets: tuple[str, ...]
    ) -> list[CandidateView]:
        """Candidates whose status=passed AND last_alert_at within the
        lookback window. Dedupe by id; the policy layer handles the
        "already running engine" and "rearm cooldown" cases.

        Also joins each candidate to its widget-specific max ts (from
        scanner_events) so the policy layer can enforce widget-aware
        freshness — critical to avoid the arm/stale asymmetry that lets
        us arm on Momo based on stale Momo history + fresh Running_Up
        activity.

        The lookback MUST be tighter than the staleness threshold so
        we don't arm on alerts that the staleness watcher would
        immediately kill (the original "armed and killed within 12s"
        bug). Default config: 90s lookback vs 5-min staleness ⇒ at
        least 3:30 of runway after arm.
        """
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=lookback_seconds)
        async with session_scope() as s:
            rows = (
                await s.execute(
                    select(Candidate).where(
                        Candidate.status == "passed",
                        Candidate.last_alert_at >= cutoff,
                    )
                )
            ).scalars().all()
            widget_last = await self._widget_specific_last_alert_at(
                s, [r.symbol for r in rows], widgets
            )
        return [
            CandidateView(
                id=r.id,
                symbol=r.symbol,
                status=r.status,
                widgets_fired=tuple(r.widgets_fired or []),
                last_close_price=r.last_close_price,
                last_alert_at=r.last_alert_at,
                widget_specific_last_alert_at=widget_last.get(r.symbol),
            )
            for r in rows
        ]

    async def _fetch_candidate_for_symbol(
        self, symbol: str, widgets: tuple[str, ...]
    ) -> CandidateView | None:
        """Return a CandidateView for `symbol` used by the staleness watcher.

        Historically this returned None when the Candidate row's
        `cooldown_until` had expired — that caused false-positive
        `candidate_disappeared` stops while scanner_events was still
        actively firing on the symbol (Candidate.cooldown_until is a
        fixed 10-min window from first_alert_at, NOT extended by
        subsequent alerts). We now decouple staleness from the
        administrative cooldown:

          1. Prefer the live candidate row (cooldown > now) if one
             exists so we get the true widgets_fired / price snapshot.
          2. Fall back to the most-recent Candidate row (ignoring
             cooldown) so we still have a symbol/price/history to
             report — the widget_specific ts drives the actual decision.
          3. Widget-specific freshness comes from scanner_events
             directly, which is always the source of truth.
          4. Return None only when the symbol has NEVER appeared as a
             Candidate (truly unknown) — the staleness watcher then
             concludes "no widget specific alert" and stops.
        """
        now_utc = dt.datetime.now(dt.timezone.utc)
        async with session_scope() as s:
            row = (
                await s.execute(
                    select(Candidate)
                    .where(
                        Candidate.symbol == symbol,
                        Candidate.cooldown_until > now_utc,
                    )
                    .order_by(Candidate.last_alert_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                # Fall back to the most-recent Candidate row for this
                # symbol (may be beyond cooldown). Preserves history/price.
                row = (
                    await s.execute(
                        select(Candidate)
                        .where(Candidate.symbol == symbol)
                        .order_by(Candidate.last_alert_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if row is None:
                    return None
            widget_last = await self._widget_specific_last_alert_at(
                s, [symbol], widgets
            )
        return CandidateView(
            id=row.id,
            symbol=row.symbol,
            status=row.status,
            widgets_fired=tuple(row.widgets_fired or []),
            last_close_price=row.last_close_price,
            last_alert_at=row.last_alert_at,
            widget_specific_last_alert_at=widget_last.get(symbol),
        )

    # ---- evaluation: arm path ----

    async def _evaluate_candidates(self, ctx: PolicyContext) -> int:
        candidates = await self._fetch_recent_passed_candidates(
            lookback_seconds=ctx.config.lookback_seconds,
            widgets=ctx.config.widgets,
        )
        # Sort by last_alert_at desc so the freshest mover gets the
        # first slot if multiple qualify in the same tick.
        candidates.sort(key=lambda c: c.last_alert_at, reverse=True)

        # Dedupe: skip the second occurrence of a symbol in the same
        # tick (the second is older by virtue of the sort).
        seen: set[str] = set()
        decisions_made = 0
        for cand in candidates:
            if cand.symbol in seen:
                continue
            seen.add(cand.symbol)
            decision = decide(cand, ctx)
            decisions_made += 1
            await self._record_decision(cand, decision)
            if decision.action == "arm":
                await self._do_arm(cand, decision, ctx)
                # After arming, refresh the context's active_engines so
                # the next candidate in the same tick sees the new
                # engine and rate-limit counters update.
                ctx = await self._build_context(ctx.config)
        return decisions_made

    async def _do_arm(
        self, cand: CandidateView, decision: Decision, ctx: PolicyContext
    ) -> None:
        cfg = ctx.config
        caps = RiskCaps(
            max_trades_per_run=cfg.max_trades_per_run,
            max_position_value_usd=cfg.max_position_value_usd,
            max_position_qty=cfg.max_position_qty,
            max_daily_loss_usd=cfg.max_daily_loss_usd,
        )
        dtd_context = {
            "auto_armed": True,
            "auto_arm_widgets": list(cand.widgets_fired),
            "auto_arm_strategy": cfg.strategy,
            "auto_arm_decision": decision.reason,
            "candidate_id": cand.id,
            "last_close_price": (
                float(cand.last_close_price)
                if isinstance(cand.last_close_price, Decimal)
                else cand.last_close_price
            ),
        }
        strategy_params = self._default_strategy_params(cfg.strategy)
        try:
            run_id = await self._registry.start(
                symbol=cand.symbol,
                strategy_name=cfg.strategy,
                strategy_params=strategy_params,
                quantity=cfg.quantity,
                autonomous=cfg.autonomous,
                risk_caps=caps,
                order_type=cfg.order_type,
                limit_offset_cents=cfg.limit_offset_cents,
                sell_anchor="bid",
                cancel_lmt_after_seconds=3.0,
                enable_depth=cfg.enable_depth,
                enable_tape=cfg.enable_tape,
                require_5m_macd=cfg.require_5m_macd,
                dtd_context=dtd_context,
            )
            # Stamp the arm time so the staleness watcher can enforce
            # the grace period. Use ctx.now_utc so a single tick is
            # internally consistent (next staleness eval in the same
            # tick will see this engine as freshly armed).
            self._armed_at[(cand.symbol, run_id)] = ctx.now_utc
            logger.info(
                "AutoArmWorker armed %s run_id=%s widget_match=%s",
                cand.symbol,
                run_id,
                decision.detail,
            )
        except (EngineAlreadyRunningError, EngineSlotFullError) as e:
            # Lost a race vs a manual arm or another auto-arm tick.
            logger.info(
                "AutoArmWorker arm for %s rejected by registry: %s",
                cand.symbol,
                e,
            )
            await self._record_decision(
                cand,
                Decision("skip", "registry_race", str(e)),
            )
        except Exception:
            logger.exception("AutoArmWorker.arm failed for %s", cand.symbol)
            await self._record_decision(
                cand,
                Decision("skip", "registry_error", "see backend logs"),
            )

    @staticmethod
    def _default_strategy_params(strategy: str) -> dict[str, Any]:
        if strategy == "first_pullback_long":
            return {
                "macd_fast": 12,
                "macd_slow": 26,
                "macd_signal": 9,
                "trigger_mode": "pullback_break",
            }
        # macd_crossover_long etc.
        return {"fast": 12, "slow": 26, "signal": 9}

    # ---- evaluation: staleness path ----

    async def _evaluate_staleness(self, ctx: PolicyContext) -> None:
        for eng in ctx.active_engines:
            if not eng.was_auto_armed:
                continue
            cand = await self._fetch_candidate_for_symbol(eng.symbol, ctx.config.widgets)
            stale = is_engine_stale(eng, cand, ctx)
            if not stale.stop:
                continue
            try:
                stopped = await self._registry.stop(
                    eng.symbol,
                    reason=f"auto_arm_staleness:{stale.reason}",
                )
                if stopped:
                    logger.info(
                        "AutoArmWorker auto-stopped %s run_id=%s reason=%s detail=%s",
                        eng.symbol,
                        eng.run_id,
                        stale.reason,
                        stale.detail,
                    )
            except Exception:
                logger.exception(
                    "AutoArmWorker.staleness stop failed for %s", eng.symbol
                )

    # ---- audit ----

    async def _record_decision(
        self, cand: CandidateView, decision: Decision
    ) -> None:
        """Audit trail for every auto-arm decision.

        MVP: skip decisions are logged to the standard logger (visible
        in the backend terminal) but not persisted to a dedicated
        DB table. The `engine_runs` row already audits successful arms
        via `dtd_context.auto_armed=true` plus
        `dtd_context.auto_arm_decision=<reason>`, so the persistent
        record for arms lives on the EngineRun itself. A future
        iteration may add a dedicated `auto_arm_decisions` table for
        skip history.
        """
        if decision.action == "arm":
            logger.info(
                "auto_arm DECISION arm symbol=%s candidate_id=%s widgets=%s reason=%s detail=%s",
                cand.symbol,
                cand.id,
                list(cand.widgets_fired),
                decision.reason,
                decision.detail,
            )
        else:
            logger.info(
                "auto_arm DECISION skip symbol=%s candidate_id=%s reason=%s detail=%s",
                cand.symbol,
                cand.id,
                decision.reason,
                decision.detail,
            )


# ---- process-wide singleton ----


_WORKER: AutoArmWorker | None = None


def get_worker() -> AutoArmWorker:
    global _WORKER
    if _WORKER is None:
        _WORKER = AutoArmWorker()
    return _WORKER


def reset_worker_for_testing() -> None:
    global _WORKER
    _WORKER = None
