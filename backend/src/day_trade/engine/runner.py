"""Process-wide engine runner.

The POC enforces at most ONE active engine run at a time. The runner is the
FastAPI-process-wide singleton that holds the currently active TradingEngine
(if any) and exposes start / stop / approve / reject.

The REST router in `day_trade.api.engine` delegates to this runner; the
runner is the only thing that owns the engine's lifecycle.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from day_trade.config import get_settings
from day_trade.ws.broker import get_broker

from .engine import EngineConfig, TradingEngine
from .ibkr_client import get_ibkr_client
from .risk import RiskCaps

logger = logging.getLogger(__name__)


class EngineBusyError(RuntimeError):
    """Raised when an engine is already running and the user tries to start
    another."""


class EngineRunner:
    def __init__(self) -> None:
        self._engine: TradingEngine | None = None
        self._lock = asyncio.Lock()

    @property
    def active(self) -> TradingEngine | None:
        if self._engine is None:
            return None
        if self._engine.status == "stopped":
            return None
        return self._engine

    async def start(
        self,
        *,
        symbol: str,
        strategy_name: str,
        strategy_params: dict[str, Any],
        quantity: int,
        autonomous: bool,
        risk_caps: RiskCaps,
        order_type: str = "MKT",
        limit_offset_cents: float = 10.0,
        sell_anchor: str = "bid",
        cancel_lmt_after_seconds: float = 3.0,
        enable_depth: bool = False,
        enable_tape: bool = False,
        dtd_context: dict[str, Any] | None = None,
    ) -> int:
        async with self._lock:
            if self.active is not None:
                raise EngineBusyError(
                    f"an engine is already running (run_id={self._engine.run_id})"
                )

            cfg = EngineConfig(
                symbol=symbol,
                strategy_name=strategy_name,
                strategy_params=strategy_params,
                quantity=quantity,
                autonomous=autonomous,
                risk_caps=risk_caps,
                order_type=order_type,
                limit_offset_cents=limit_offset_cents,
                sell_anchor=sell_anchor,
                cancel_lmt_after_seconds=cancel_lmt_after_seconds,
                enable_depth=enable_depth,
                enable_tape=enable_tape,
                dtd_context=dict(dtd_context or {}),
            )
            engine = TradingEngine(
                config=cfg,
                ibkr=get_ibkr_client(),
                broker=get_broker(),
                settings=get_settings(),
            )
            run_id = await engine.start()
            self._engine = engine
            logger.info(
                "EngineRunner started run_id=%s symbol=%s strategy=%s autonomous=%s "
                "order_type=%s sell_anchor=%s depth=%s tape=%s",
                run_id, symbol, strategy_name, autonomous,
                order_type, sell_anchor, enable_depth, enable_tape,
            )
            return run_id

    async def stop(self, reason: str = "user_stop") -> bool:
        async with self._lock:
            if self._engine is None:
                return False
            engine = self._engine
            await engine.stop(reason=reason)
            self._engine = None
            return True

    def approve(self) -> bool:
        if self._engine is None:
            return False
        return self._engine.approve_pending()

    def reject(self) -> bool:
        if self._engine is None:
            return False
        return self._engine.reject_pending()

    def status(self) -> dict[str, Any]:
        e = self._engine
        if e is None:
            return {"active": False}
        snap = e.strategy.snapshot() if e.strategy else None
        from .features import compute_snapshot
        features = (
            compute_snapshot(e.market_state).to_dict() if e.market_state is not None else None
        )
        return {
            "active": True,
            "run_id": e.run_id,
            "status": e.status,
            "symbol": e.config.symbol,
            "strategy": e.config.strategy_name,
            "autonomous": e.config.autonomous,
            "quantity": e.config.quantity,
            "ibkr_account": e.ibkr.account,
            "order_type": e.config.order_type,
            "limit_offset_cents": e.config.limit_offset_cents,
            "sell_anchor": e.config.sell_anchor,
            "cancel_lmt_after_seconds": e.config.cancel_lmt_after_seconds,
            "enable_depth": e.config.enable_depth,
            "enable_tape": e.config.enable_tape,
            "dtd_context": dict(e.config.dtd_context),
            "risk_state": {
                "trades_count": e.risk.state.trades_count if e.risk else 0,
                "open_position_qty": e.risk.state.open_position_qty if e.risk else 0,
                "realized_pnl_usd": e.risk.state.realized_pnl_usd if e.risk else 0.0,
                "kill_switch_on": e.risk.state.kill_switch_on if e.risk else False,
            },
            "strategy_state": snap,
            "features": features,
            "has_pending_approval": e._pending is not None and not e._pending.future.done(),
        }


_RUNNER: EngineRunner | None = None


def get_runner() -> EngineRunner:
    global _RUNNER
    if _RUNNER is None:
        _RUNNER = EngineRunner()
    return _RUNNER
