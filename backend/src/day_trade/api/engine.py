"""REST API for the v1.3 multi-engine trading engine.

  POST /engine/start                       -> start a new engine for a symbol
  POST /engine/stop?symbol=X               -> stop the engine running on X
  POST /engine/stop_all                    -> stop every active engine
  POST /engine/approve?run_id=X            -> approve a parked signal
  POST /engine/reject?run_id=X             -> reject a parked signal
  GET  /engine/status                      -> all engines + portfolio + slots
  GET  /engine/portfolio                   -> just the portfolio gate snapshot
  POST /engine/portfolio/reset_kill_switch -> manually clear the daily kill switch
  GET  /engine/runs                        -> recent runs (paginated)
  GET  /engine/runs/{run_id}               -> single run detail
  GET  /engine/runs/{run_id}/events        -> audit-log events (paginated)
  GET  /engine/runs/{run_id}/bars          -> persisted 1m bars (for chart)
  GET  /engine/strategies                  -> registry of available strategies

Migration note (v1.2 -> v1.3): /engine/stop, /engine/approve, /engine/reject
now require their respective ?symbol / ?run_id query parameters. /engine/status
returns a list of engines under `engines`, not a single flat object.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import desc, select

from day_trade.db.models import BarAggregate, EngineEvent, EngineRun
from day_trade.db.session import session_scope
from day_trade.engine.ibkr_client import IBKRConnectionError, IBKRSafetyError
from day_trade.engine.registry import (
    EngineAlreadyRunningError,
    EngineSlotFullError,
    get_registry,
)
from day_trade.engine.risk import RiskCaps
from day_trade.engine.strategies import STRATEGIES

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/engine", tags=["engine"])


def _apply_microstructure_overrides(
    strategy_name: str,
    strategy_params: dict[str, Any],
    require_5m_macd: bool,
    overrides: MicrostructureIn | None,
) -> dict[str, Any]:
    """Return a copy of `strategy_params` with any user-supplied
    microstructure overrides materialised into a `TrendGateConfig`
    instance under the `"trend"` key.

    Only applies to `first_pullback_long` (other strategies don't accept
    a `trend` kwarg). Fields left as `None` on `overrides` fall through
    to the strategy's built-in defaults.

    The `require_5m_macd` flag is folded into the same `TrendGateConfig`
    so the two configuration paths compose correctly (previously the
    engine's __init__ handled `require_5m_macd=False` via a separate
    fallback that we would otherwise silently skip once `"trend"` is
    already populated).
    """
    result = dict(strategy_params)
    if strategy_name != "first_pullback_long":
        return result

    override_dict: dict[str, Any] = {}
    if overrides is not None:
        override_dict = overrides.model_dump(exclude_none=True)

    # No user overrides -> leave strategy_params untouched and let the
    # engine's own __init__ handle the require_5m_macd fallback as
    # before. This keeps auto-armed engines (which never send an
    # overrides block) on the strategy defaults, and preserves any
    # explicit `trend` a caller might have stuffed into
    # `strategy_params` directly.
    if not override_dict:
        return result

    # Local import to avoid a top-level dependency on the strategies
    # package (keeps the API module cheap to import).
    from day_trade.engine.strategies.first_pullback_long import TrendGateConfig

    kwargs: dict[str, Any] = {
        "require_5m_histogram_positive": require_5m_macd,
        "require_5m_histogram_not_falling": require_5m_macd,
    }
    kwargs.update(override_dict)
    result["trend"] = TrendGateConfig(**kwargs)
    return result


# ---------- request / response schemas ----------


class RiskCapsIn(BaseModel):
    max_trades_per_run: int = Field(5, ge=1, le=50)
    max_position_value_usd: float = Field(5000.0, gt=0, le=1_000_000)
    max_position_qty: int = Field(25_000, ge=1, le=10_000_000)
    max_daily_loss_usd: float = Field(150.0, gt=0, le=100_000)


class MicrostructureIn(BaseModel):
    """Optional per-engine overrides for the entry-time microstructure gate.

    Applies only to `first_pullback_long`. All fields are optional; anything
    left unset falls through to the strategy's built-in defaults (see
    `TrendGateConfig`). Ignored by other strategies.

    Rationale for exposing this per-engine (as opposed to the Stage-1
    Filter Rules which govern candidate selection): the microstructure
    gate is evaluated at every tick against live L2/T&S, and different
    price bands / setups warrant different thresholds. Auto-armed engines
    use the defaults; manual arms can override on a case-by-case basis.
    """

    # Price-tiered spread caps (bps of mid). Basis-points scale is
    # deliberately wide because 5c on a $2.50 stock is 200 bps and Ross
    # still trades it.
    max_spread_bps: float | None = Field(
        default=None, ge=1.0, le=1000.0,
        description=">= $20 spread cap in bps (default 50).",
    )
    max_spread_bps_under_5: float | None = Field(
        default=None, ge=1.0, le=1000.0,
        description="Sub-$5 spread cap in bps (default 200).",
    )
    max_spread_bps_under_20: float | None = Field(
        default=None, ge=1.0, le=1000.0,
        description="$5-$20 spread cap in bps (default 100).",
    )

    # L2 / T&S balance gates. 0..1 is the natural range (share of bid
    # size, share of buy prints).
    min_bid_ask_imbalance: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description="Min bid share of top-of-book size (default 0.45).",
    )
    min_tape_buy_pct: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description="Min buy % of last 60s prints (default 0.45).",
    )


class DtdContextIn(BaseModel):
    """Free-form DTD context the user types when arming a symbol.

    All fields are optional - we don't validate the trader's selection beyond
    type-checking the values. They are persisted with the engine_run and
    available for post-mortem analysis.
    """

    alert_type: str | None = None        # e.g. "Momo / New High"
    setup_type: str | None = None        # e.g. "first_pullback" / "micro_pullback" / "hod_break"
    gap_pct: float | None = None
    float_shares_millions: float | None = None
    rel_vol: float | None = None
    has_news: bool | None = None
    news_headline: str | None = None
    premarket_high: float | None = None
    dollar_volume_millions: float | None = None
    notes: str | None = None


class StartIn(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=20)
    strategy_name: str = "macd_crossover_long"
    strategy_params: dict[str, Any] = Field(default_factory=lambda: {"fast": 12, "slow": 26, "signal": 9})
    quantity: int = Field(..., ge=1, le=10_000_000)
    autonomous: bool = False
    risk_caps: RiskCapsIn = Field(default_factory=RiskCapsIn)

    # v1.1 fields (semi-automated FirstPullback workflow)
    order_type: str = Field("MKT", pattern="^(MKT|LMT)$")
    limit_offset_cents: float = Field(10.0, ge=0.0, le=500.0)
    # SELL leg routing - mirrors DAS "Sell at Bid" vs "Sell at Ask" hotkeys.
    # 'bid' (aggressive, default): limit = bid - offset
    # 'ask' (passive):             limit = ask - offset
    sell_anchor: str = Field("bid", pattern="^(bid|ask)$")
    cancel_lmt_after_seconds: float = Field(3.0, ge=0.5, le=120.0)
    enable_depth: bool = False
    enable_tape: bool = False
    # When True (default): the FirstPullback gate requires 5m MACD positive
    # and not falling - Ross's broader-trend filter. When False: ignore the
    # 5m MACD entirely (1m MACD + VWAP + backside + trigger only). Useful
    # for fast-pivot scenarios on brand-new movers where 5m MACD hasn't
    # warmed up (needs ~26 5m bars = ~130 minutes of trading history).
    require_5m_macd: bool = True
    dtd_context: DtdContextIn = Field(default_factory=DtdContextIn)
    # Optional per-engine microstructure overrides. When None (default),
    # the strategy uses its built-in TrendGateConfig defaults - which is
    # what auto-armed engines get. Manual arms may override individual
    # fields via this block.
    microstructure: MicrostructureIn | None = None


class StartOut(BaseModel):
    run_id: int
    symbol: str
    status: str


class StopOut(BaseModel):
    stopped: bool
    symbol: str


class StopAllOut(BaseModel):
    stopped: int


class ApprovalOut(BaseModel):
    handled: bool


class EngineStatusItem(BaseModel):
    """Per-engine status. Same shape as the legacy single-engine
    StatusOut, except `active` is implicit (only active engines appear
    in the list) and `symbol` is always populated."""

    active: bool = True
    run_id: int
    status: str
    symbol: str
    strategy: str
    autonomous: bool
    quantity: int
    ibkr_account: str | None = None
    order_type: str | None = None
    limit_offset_cents: float | None = None
    sell_anchor: str | None = None
    cancel_lmt_after_seconds: float | None = None
    enable_depth: bool | None = None
    enable_tape: bool | None = None
    require_5m_macd: bool | None = None
    dtd_context: dict[str, Any] | None = None
    risk_state: dict[str, Any] | None = None
    strategy_state: dict[str, Any] | None = None
    features: dict[str, Any] | None = None
    has_pending_approval: bool | None = None


class PortfolioCapsOut(BaseModel):
    max_daily_loss_usd: float
    max_concurrent_engines: int
    max_total_trades_per_day: int


class PortfolioStatusOut(BaseModel):
    caps: PortfolioCapsOut
    holder: str | None
    is_holding: bool
    realized_pnl_usd: float
    trades_count: int
    kill_switch_on: bool
    day_utc: str | None


class SlotsStatusOut(BaseModel):
    active: int
    max: int


class StatusOut(BaseModel):
    """Top-level registry status: list of active engines, portfolio gate
    snapshot, and the slot capacity summary."""

    engines: list[EngineStatusItem]
    portfolio: PortfolioStatusOut
    slots: SlotsStatusOut


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    instrument_type: str
    strategy_name: str
    params: dict[str, Any]
    risk_caps: dict[str, Any]
    autonomous: bool
    market_data_type: str
    ibkr_client_id: int
    ibkr_account: str | None
    status: str
    started_at: dt.datetime
    stopped_at: dt.datetime | None
    stop_reason: str | None
    realized_pnl: Decimal
    trades_count: int
    # v1.1
    dtd_context: dict[str, Any] = {}
    order_type: str = "MKT"
    limit_offset_cents: Decimal = Decimal("10.00")
    sell_anchor: str = "bid"
    enable_depth: bool = False
    enable_tape: bool = False


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    run_id: int
    ts: dt.datetime
    event_type: str
    payload: dict[str, Any]


class BarOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ts: dt.datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    macd_line: Decimal | None
    macd_signal: Decimal | None
    macd_hist: Decimal | None


class StrategyOut(BaseModel):
    name: str
    params_schema: dict[str, Any]


# ---------- handlers ----------


_STRATEGY_DEFAULTS: dict[str, dict[str, Any]] = {
    "macd_crossover_long": {"fast": 12, "slow": 26, "signal": 9},
    "first_pullback_long": {
        "macd_fast": 12,
        "macd_slow": 26,
        "macd_signal": 9,
        # Default trigger: Ross-style "first 1m candle to make a new high after
        # the most recent red-candle pullback". Alternative: 'macd_cross'
        # (1m MACD histogram cross-up).
        "trigger_mode": "pullback_break",
    },
}


@router.get("/strategies", response_model=list[StrategyOut])
async def list_strategies() -> list[StrategyOut]:
    """Return the strategy registry so the UI can render the picker."""
    return [
        StrategyOut(
            name=name,
            params_schema=_STRATEGY_DEFAULTS.get(name, {}),
        )
        for name in STRATEGIES
    ]


@router.post("/start", response_model=StartOut)
async def start(body: StartIn) -> StartOut:
    """Start a new engine for `body.symbol`. Errors:
      - 409 EngineAlreadyRunningError: an engine for that symbol is
        already active (stop it first or use drop-and-replace).
      - 409 EngineSlotFullError: registry already at
        max_concurrent_engines.
      - 403 IBKRSafetyError: paper/live safety guards refused.
      - 502 IBKRConnectionError: TWS / Gateway unreachable.
      - 400 KeyError / ValueError: invalid strategy or params.
    """
    registry = get_registry()
    caps = RiskCaps(
        max_trades_per_run=body.risk_caps.max_trades_per_run,
        max_position_value_usd=body.risk_caps.max_position_value_usd,
        max_position_qty=body.risk_caps.max_position_qty,
        max_daily_loss_usd=body.risk_caps.max_daily_loss_usd,
    )
    strategy_params = _apply_microstructure_overrides(
        strategy_name=body.strategy_name,
        strategy_params=body.strategy_params,
        require_5m_macd=body.require_5m_macd,
        overrides=body.microstructure,
    )
    try:
        run_id = await registry.start(
            symbol=body.symbol,
            strategy_name=body.strategy_name,
            strategy_params=strategy_params,
            quantity=body.quantity,
            autonomous=body.autonomous,
            risk_caps=caps,
            order_type=body.order_type,
            limit_offset_cents=body.limit_offset_cents,
            sell_anchor=body.sell_anchor,
            cancel_lmt_after_seconds=body.cancel_lmt_after_seconds,
            enable_depth=body.enable_depth,
            enable_tape=body.enable_tape,
            require_5m_macd=body.require_5m_macd,
            dtd_context=body.dtd_context.model_dump(exclude_none=True),
        )
    except EngineAlreadyRunningError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except EngineSlotFullError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except IBKRSafetyError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except IBKRConnectionError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except KeyError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return StartOut(run_id=run_id, symbol=body.symbol, status="running")


@router.post("/stop", response_model=StopOut)
async def stop(
    symbol: str = Query(..., min_length=1, max_length=20, description="Symbol to stop"),
) -> StopOut:
    """Stop the engine running on `symbol`. Returns `stopped=false` if
    no active engine existed for that symbol (idempotent)."""
    stopped = await get_registry().stop(symbol, reason="user_stop")
    return StopOut(stopped=stopped, symbol=symbol)


@router.post("/stop_all", response_model=StopAllOut)
async def stop_all() -> StopAllOut:
    """Stop every active engine in the registry. Returns the count of
    engines stopped."""
    count = await get_registry().stop_all(reason="user_stop_all")
    return StopAllOut(stopped=count)


@router.post("/approve", response_model=ApprovalOut)
async def approve(
    run_id: int = Query(..., ge=1, description="run_id of the engine with the pending approval"),
) -> ApprovalOut:
    return ApprovalOut(handled=get_registry().approve(run_id))


@router.post("/reject", response_model=ApprovalOut)
async def reject(
    run_id: int = Query(..., ge=1, description="run_id of the engine with the pending approval"),
) -> ApprovalOut:
    return ApprovalOut(handled=get_registry().reject(run_id))


class AutonomousPatch(BaseModel):
    autonomous: bool


class AutonomousOut(BaseModel):
    run_id: int
    symbol: str
    autonomous: bool


@router.post("/runs/{run_id}/autonomous", response_model=AutonomousOut)
async def set_run_autonomous(run_id: int, patch: AutonomousPatch) -> AutonomousOut:
    """Flip a running engine's autonomous flag at runtime.

    Autonomous=True means signals bypass the manual-approval gate and
    execute directly (subject to normal risk checks). This mutates the
    live engine's frozen config via `object.__setattr__` so the next
    signal fire path observes the new value; it also updates the DB
    row so restarts / audit-logs reflect the change.

    Safety: requires PAPER_TRADING_ONLY=true. In live-trading mode this
    endpoint refuses so operators can't accidentally bypass approvals on
    a real-money account via a REST call.
    """
    from day_trade.config import get_settings

    settings = get_settings()
    if not settings.paper_trading_only:
        raise HTTPException(
            status_code=403,
            detail="Runtime autonomous toggle is disabled in live-trading mode",
        )

    engine = get_registry().engine_for_run_id(run_id)
    if engine is None:
        raise HTTPException(
            status_code=404, detail=f"No active engine for run_id={run_id}"
        )
    if not settings.manual_approval_required and patch.autonomous is False:
        # Not fatal, but worth reporting: system-level default already flat.
        pass

    # EngineConfig is a frozen dataclass; use object.__setattr__ to mutate
    # in-place. This is intentional (the alternative would be to swap the
    # entire config object which would race with any concurrent reader).
    object.__setattr__(engine.config, "autonomous", patch.autonomous)

    async with session_scope() as s:
        row = (
            await s.execute(select(EngineRun).where(EngineRun.id == run_id))
        ).scalar_one_or_none()
        if row is not None:
            row.autonomous = patch.autonomous

    logger.info(
        "Runtime autonomous flag toggled: run_id=%d symbol=%s autonomous=%s",
        run_id, engine.config.symbol, patch.autonomous,
    )
    return AutonomousOut(
        run_id=run_id, symbol=engine.config.symbol, autonomous=patch.autonomous
    )


@router.get("/status", response_model=StatusOut)
async def status() -> StatusOut:
    return StatusOut(**get_registry().status())


@router.get("/portfolio", response_model=PortfolioStatusOut)
async def get_portfolio() -> PortfolioStatusOut:
    """Just the portfolio gate snapshot, without the per-engine list.
    Used by the top-bar summary in the dashboard."""
    return PortfolioStatusOut(**get_registry().portfolio_risk.snapshot())


@router.post("/portfolio/reset_kill_switch", response_model=PortfolioStatusOut)
async def reset_kill_switch() -> PortfolioStatusOut:
    """Manually clear the daily kill switch. Used when the operator has
    reviewed the day's trades and explicitly wants to re-arm the bot
    before UTC midnight. Realized P&L and trade counter are preserved."""
    registry = get_registry()
    await registry.portfolio_risk.reset_kill_switch()
    return PortfolioStatusOut(**registry.portfolio_risk.snapshot())


@router.get("/runs", response_model=list[RunOut])
async def list_runs(limit: int = Query(50, ge=1, le=500)) -> list[RunOut]:
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(EngineRun).order_by(desc(EngineRun.started_at)).limit(limit)
            )
        ).scalars().all()
        return [RunOut.model_validate(r) for r in rows]


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run(run_id: int) -> RunOut:
    async with session_scope() as s:
        row = await s.get(EngineRun, run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="run not found")
        return RunOut.model_validate(row)


@router.get("/runs/{run_id}/events", response_model=list[EventOut])
async def get_run_events(
    run_id: int,
    limit: int = Query(500, ge=1, le=5000),
    after_id: int | None = Query(None, ge=1),
    event_type: str | None = Query(None),
) -> list[EventOut]:
    async with session_scope() as s:
        stmt = select(EngineEvent).where(EngineEvent.run_id == run_id)
        if after_id is not None:
            stmt = stmt.where(EngineEvent.id > after_id)
        if event_type is not None:
            stmt = stmt.where(EngineEvent.event_type == event_type)
        stmt = stmt.order_by(EngineEvent.id.asc()).limit(limit)
        rows = (await s.execute(stmt)).scalars().all()
        return [EventOut.model_validate(r) for r in rows]


@router.get("/runs/{run_id}/bars", response_model=list[BarOut])
async def get_run_bars(
    run_id: int,
    limit: int = Query(500, ge=1, le=5000),
) -> list[BarOut]:
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(BarAggregate)
                .where(BarAggregate.run_id == run_id)
                .order_by(BarAggregate.ts.asc())
                .limit(limit)
            )
        ).scalars().all()
        return [BarOut.model_validate(r) for r in rows]
