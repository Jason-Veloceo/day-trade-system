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


# ---------- request / response schemas ----------


class RiskCapsIn(BaseModel):
    max_trades_per_run: int = Field(5, ge=1, le=50)
    max_position_value_usd: float = Field(5000.0, gt=0, le=1_000_000)
    max_position_qty: int = Field(25_000, ge=1, le=10_000_000)
    max_daily_loss_usd: float = Field(150.0, gt=0, le=100_000)


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
    try:
        run_id = await registry.start(
            symbol=body.symbol,
            strategy_name=body.strategy_name,
            strategy_params=body.strategy_params,
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
