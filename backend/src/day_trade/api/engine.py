"""REST API for the POC trading engine.

  POST /engine/start                    -> start a new run
  POST /engine/stop                     -> stop the active run
  POST /engine/approve                  -> approve a parked signal (non-autonomous run)
  POST /engine/reject                   -> reject a parked signal
  GET  /engine/status                   -> current active run + state snapshot
  GET  /engine/runs                     -> recent runs (paginated)
  GET  /engine/runs/{run_id}            -> single run detail
  GET  /engine/runs/{run_id}/events     -> audit-log events (paginated)
  GET  /engine/runs/{run_id}/bars       -> persisted 1m bars (for chart)
  GET  /engine/strategies               -> registry of available strategies
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
from day_trade.engine.risk import RiskCaps
from day_trade.engine.runner import EngineBusyError, get_runner
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
    status: str


class StopOut(BaseModel):
    stopped: bool


class ApprovalOut(BaseModel):
    handled: bool


class StatusOut(BaseModel):
    active: bool
    run_id: int | None = None
    status: str | None = None
    symbol: str | None = None
    strategy: str | None = None
    autonomous: bool | None = None
    quantity: int | None = None
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
    runner = get_runner()
    caps = RiskCaps(
        max_trades_per_run=body.risk_caps.max_trades_per_run,
        max_position_value_usd=body.risk_caps.max_position_value_usd,
        max_position_qty=body.risk_caps.max_position_qty,
        max_daily_loss_usd=body.risk_caps.max_daily_loss_usd,
    )
    try:
        run_id = await runner.start(
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
    except EngineBusyError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except IBKRSafetyError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except IBKRConnectionError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except KeyError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return StartOut(run_id=run_id, status="running")


@router.post("/stop", response_model=StopOut)
async def stop() -> StopOut:
    runner = get_runner()
    stopped = await runner.stop(reason="user_stop")
    return StopOut(stopped=stopped)


@router.post("/approve", response_model=ApprovalOut)
async def approve() -> ApprovalOut:
    return ApprovalOut(handled=get_runner().approve())


@router.post("/reject", response_model=ApprovalOut)
async def reject() -> ApprovalOut:
    return ApprovalOut(handled=get_runner().reject())


@router.get("/status", response_model=StatusOut)
async def status() -> StatusOut:
    return StatusOut(**get_runner().status())


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
