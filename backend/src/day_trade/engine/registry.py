"""Process-wide engine registry — Phase 1 multi-engine substrate.

Replaces the single-engine `EngineRunner` with a registry that can hold
up to N concurrent `TradingEngine` instances (one per symbol), each fully
independent (own bars, indicators, gates, exits, journal). All engines
share a single `PortfolioRiskGate` that enforces "at most ONE open
position at any time" across the whole registry.

This module is the new owner of engine lifecycle for v1.3+. The legacy
`EngineRunner` (in `runner.py`) continues to exist alongside during the
migration — the API layer cuts over in Day 3 of the multi-engine slice.

See `docs/multi_engine_design.md` for the architecture decisions.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from day_trade.config import get_settings
from day_trade.ws.broker import get_broker

from .engine import EngineConfig, TradingEngine
from .ibkr_client import get_ibkr_client
from .portfolio_risk import PortfolioRiskCaps, PortfolioRiskGate
from .risk import RiskCaps

logger = logging.getLogger(__name__)


class EngineSlotFullError(RuntimeError):
    """Raised when a new engine start is rejected because the registry
    is already at `portfolio_caps.max_concurrent_engines`."""


class EngineAlreadyRunningError(RuntimeError):
    """Raised when a start is requested for a symbol that already has
    an active engine in the registry. The caller should stop the existing
    engine first, or use Drop-and-Replace."""


EngineFactory = Callable[[EngineConfig, PortfolioRiskGate], TradingEngine]
"""Factory function injected for testability. Production wires the
default factory which uses `get_ibkr_client()` / `get_broker()` /
`get_settings()` and threads the registry's `PortfolioRiskGate` into
the engine constructor so the engine consults the mutex on every entry.
Tests inject a mock factory that returns lightweight fake engines
without needing IBKR; the test factory may also ignore portfolio_risk."""


def _default_engine_factory(
    cfg: EngineConfig, portfolio_risk: PortfolioRiskGate
) -> TradingEngine:
    return TradingEngine(
        config=cfg,
        ibkr=get_ibkr_client(),
        broker=get_broker(),
        settings=get_settings(),
        portfolio_risk=portfolio_risk,
    )


class EngineRegistry:
    """Multi-engine registry.

    Holds 0..N `TradingEngine` instances keyed by symbol. Lifecycle
    operations (start / stop / stop_all) are serialised via a single
    `asyncio.Lock` so two concurrent start requests for the same symbol
    cannot race. Approve / reject are routed by `run_id`.

    The registry exposes a `portfolio_risk: PortfolioRiskGate` that every
    engine should consult before submitting an entry order (wired in
    Day 2 of the multi-engine slice).
    """

    def __init__(
        self,
        *,
        engine_factory: EngineFactory | None = None,
        portfolio_caps: PortfolioRiskCaps | None = None,
    ) -> None:
        self._engines: dict[str, TradingEngine] = {}
        self._lock = asyncio.Lock()
        self._portfolio_risk = PortfolioRiskGate(portfolio_caps)
        self._engine_factory = engine_factory or _default_engine_factory

    # ---- accessors ----

    @property
    def portfolio_risk(self) -> PortfolioRiskGate:
        return self._portfolio_risk

    @property
    def max_concurrent_engines(self) -> int:
        return self._portfolio_risk.caps.max_concurrent_engines

    def active(self) -> list[TradingEngine]:
        """Snapshot of currently-active engines (status != 'stopped')."""
        return [e for e in self._engines.values() if e.status != "stopped"]

    def engine_for_symbol(self, symbol: str) -> TradingEngine | None:
        """Get the engine for `symbol` if one is active, else None."""
        e = self._engines.get(symbol)
        if e is None or e.status == "stopped":
            return None
        return e

    def engine_for_run_id(self, run_id: int) -> TradingEngine | None:
        """Find an active engine by its run_id. Used by approve/reject
        which take a run_id rather than a symbol."""
        for e in self._engines.values():
            if e.status != "stopped" and e.run_id == run_id:
                return e
        return None

    # ---- lifecycle ----

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
        require_5m_macd: bool = True,
        dtd_context: dict[str, Any] | None = None,
    ) -> int:
        """Start a new engine for `symbol`.

        Raises:
            EngineAlreadyRunningError: an engine for `symbol` is already
                active. Stop it first.
            EngineSlotFullError: registry is at `max_concurrent_engines`.
        """
        async with self._lock:
            existing = self._engines.get(symbol)
            if existing is not None and existing.status != "stopped":
                raise EngineAlreadyRunningError(
                    f"engine for {symbol} already running "
                    f"(run_id={existing.run_id}, status={existing.status})"
                )

            # Garbage-collect any leftover stopped entry for this symbol
            # before counting slots (prevents stale entries from blocking
            # a re-arm of the same symbol).
            if existing is not None and existing.status == "stopped":
                del self._engines[symbol]

            n_active = sum(1 for e in self._engines.values() if e.status != "stopped")
            if n_active >= self.max_concurrent_engines:
                raise EngineSlotFullError(
                    f"registry full ({n_active}/{self.max_concurrent_engines} "
                    f"engines active); stop one before starting another"
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
                require_5m_macd=require_5m_macd,
                dtd_context=dict(dtd_context or {}),
            )
            engine = self._engine_factory(cfg, self._portfolio_risk)
            run_id = await engine.start()
            self._engines[symbol] = engine
            logger.info(
                "EngineRegistry started run_id=%s symbol=%s strategy=%s "
                "autonomous=%s order_type=%s sell_anchor=%s depth=%s tape=%s "
                "require_5m_macd=%s active_engines=%d/%d",
                run_id,
                symbol,
                strategy_name,
                autonomous,
                order_type,
                sell_anchor,
                enable_depth,
                enable_tape,
                require_5m_macd,
                len(self.active()),
                self.max_concurrent_engines,
            )
            return run_id

    async def stop(self, symbol: str, reason: str = "user_stop") -> bool:
        """Stop the engine for `symbol`. Returns True if an engine was
        actually stopped, False if no active engine existed for that
        symbol. The stopped engine is removed from the registry."""
        async with self._lock:
            engine = self._engines.get(symbol)
            if engine is None or engine.status == "stopped":
                return False
            await engine.stop(reason=reason)
            # Keep the stopped engine in the dict briefly for status
            # queries, but mark it inactive. Garbage-collect on next start
            # for the same symbol, or via explicit prune.
            del self._engines[symbol]
            logger.info(
                "EngineRegistry stopped symbol=%s reason=%s active_engines=%d/%d",
                symbol,
                reason,
                len(self.active()),
                self.max_concurrent_engines,
            )
            return True

    async def stop_all(self, reason: str = "user_stop_all") -> int:
        """Stop every active engine in the registry. Returns the count
        of engines stopped."""
        async with self._lock:
            stopped = 0
            for symbol, engine in list(self._engines.items()):
                if engine.status == "stopped":
                    continue
                try:
                    await engine.stop(reason=reason)
                    stopped += 1
                except Exception:
                    logger.exception("error stopping engine for %s", symbol)
            self._engines.clear()
            logger.info("EngineRegistry stop_all reason=%s stopped=%d", reason, stopped)
            return stopped

    # ---- approval routing ----

    def approve(self, run_id: int) -> bool:
        """Approve the pending entry on the engine with this run_id.
        Returns True if approval was handled, False if no matching
        engine or no pending approval."""
        engine = self.engine_for_run_id(run_id)
        if engine is None:
            return False
        return engine.approve_pending()

    def reject(self, run_id: int) -> bool:
        """Reject the pending entry on the engine with this run_id."""
        engine = self.engine_for_run_id(run_id)
        if engine is None:
            return False
        return engine.reject_pending()

    # ---- status / introspection ----

    def status(self) -> dict[str, Any]:
        """Top-level registry snapshot. Returns:

        ```
        {
            "engines": [<per-engine status dict>, ...],
            "portfolio": <portfolio_risk.snapshot()>,
            "slots": {"active": N, "max": M},
        }
        ```

        Used by `GET /engine/status` once the API cuts over to the
        registry. Per-engine status format matches the legacy
        single-engine status shape for frontend continuity.
        """
        engines = [self._engine_status(e) for e in self.active()]
        return {
            "engines": engines,
            "portfolio": self._portfolio_risk.snapshot(),
            "slots": {
                "active": len(engines),
                "max": self.max_concurrent_engines,
            },
        }

    def status_for_symbol(self, symbol: str) -> dict[str, Any] | None:
        """Per-engine status for a single symbol, or None if no active
        engine exists for that symbol."""
        engine = self.engine_for_symbol(symbol)
        if engine is None:
            return None
        return self._engine_status(engine)

    def _engine_status(self, e: TradingEngine) -> dict[str, Any]:
        """Build the per-engine status dict (matches the legacy single-
        engine shape so the frontend doesn't need two payload formats)."""
        snap = e.strategy.snapshot() if e.strategy else None
        from .features import compute_snapshot

        features = (
            compute_snapshot(e.market_state).to_dict()
            if e.market_state is not None
            else None
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
            "require_5m_macd": e.config.require_5m_macd,
            "dtd_context": dict(e.config.dtd_context),
            "risk_state": {
                "trades_count": e.risk.state.trades_count if e.risk else 0,
                "open_position_qty": e.risk.state.open_position_qty if e.risk else 0,
                "realized_pnl_usd": e.risk.state.realized_pnl_usd if e.risk else 0.0,
                "kill_switch_on": e.risk.state.kill_switch_on if e.risk else False,
            },
            "strategy_state": snap,
            "features": features,
            "has_pending_approval": (
                e._pending is not None and not e._pending.future.done()
            ),
        }


_REGISTRY: EngineRegistry | None = None


def get_registry() -> EngineRegistry:
    """Process-wide singleton accessor for the engine registry."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = EngineRegistry()
    return _REGISTRY


def reset_registry_for_testing() -> None:
    """TEST-ONLY: drops the singleton so the next `get_registry()` builds
    a fresh one. Use in tests that need a clean registry; never call from
    production code."""
    global _REGISTRY
    _REGISTRY = None
