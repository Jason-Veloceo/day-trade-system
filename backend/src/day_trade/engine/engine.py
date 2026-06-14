"""TradingEngine - per-run orchestrator.

Owns one EngineRun. Wires up:
  - 1-minute BarFeed (existing) -> primary strategy callback `on_bar`
  - 5-minute aggregator on top of the 1m feed -> `on_5m_bar`
  - Optional L2 (reqMktDepth) + T&S (reqTickByTickData) subscriptions
  - Optional NBBO quote (reqMktData) for marketable-LMT pricing
  - Risk gate, executor, exit-trigger framework, journal
  - Single asyncio.Queue of pending approvals (manual mode)

Auto re-arm: the strategy stays "live" across multiple entry/exit cycles
inside one engine run. When an exit fills, the strategy resets its
`in_position` latch and gates run again from the next bar. The user stops
the run via the Stop button (or daily-loss caps are breached).

Order routing: if `order_type=LMT`, the executor reads NBBO from the quote
ticker and submits LMT @ ask+offset (BUY) or LMT @ bid-offset (SELL) with
cancel-on-timeout. Default `order_type=MKT` for the legacy macd_crossover
strategy.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ib_async import Ticker

from day_trade.config import Settings, get_settings
from day_trade.db.models import BarAggregate, EngineRun
from day_trade.db.session import session_scope
from day_trade.ws.broker import MessageBroker

from .bars import BarFeed
from .exits import ExitConfig, ExitDecision, ExitEvaluationInputs, ExitTriggerSet
from .executor import Executor
from .features import FeatureSnapshot, compute_snapshot
from .ibkr_client import IBKRClient
from .instruments import InstrumentSpec, build_contract
from .journal import Journal
from .multitf import HigherTimeframeAggregator
from .orderbook import MarketState
from .risk import RiskCaps, RiskGate
from .strategies import Strategy, get_strategy
from .strategies.base import Bar, Signal, SignalKind
from .strategies.first_pullback_long import FirstPullbackLong

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PendingApproval:
    """A signal waiting for manual approve/reject."""

    signal: Signal
    intended_qty: int
    future: asyncio.Future


@dataclass(frozen=True, slots=True)
class EngineConfig:
    """User-facing config for one engine run.

    Fields:
      order_type: "MKT" (default - back-compat with macd_crossover) or "LMT".
      limit_offset_cents: per-side offset for LMT.
                          BUY  -> limit = ask + offset (always)
                          SELL -> limit = bid - offset  if sell_anchor='bid'
                                  limit = ask - offset  if sell_anchor='ask'
      sell_anchor: 'bid' (aggressive, default) or 'ask' (passive). Mirrors the
                   user's hotkey choice between "Sell at Bid" and "Sell at Ask".
                   Applies to ALL exits (hard stop, targets, distress, time).
      cancel_lmt_after_seconds: cancel unfilled LMTs after this many seconds.
      enable_depth: subscribe to reqMktDepth (10 levels).
      enable_tape: subscribe to reqTickByTickData ('AllLast').
      dtd_context: free-form DTD context fields the user typed on the Arm form
                   (alert_type, gap_pct, float_shares, news_catalyst, ...).
    """

    symbol: str
    strategy_name: str
    strategy_params: dict[str, Any]
    quantity: int
    autonomous: bool
    risk_caps: RiskCaps

    order_type: str = "MKT"
    limit_offset_cents: float = 10.0
    sell_anchor: str = "bid"
    cancel_lmt_after_seconds: float = 3.0
    enable_depth: bool = False
    enable_tape: bool = False
    dtd_context: dict[str, Any] = field(default_factory=dict)


class TradingEngine:
    def __init__(
        self,
        *,
        config: EngineConfig,
        ibkr: IBKRClient,
        broker: MessageBroker,
        settings: Settings | None = None,
    ) -> None:
        self.config = config
        self.ibkr = ibkr
        self.broker = broker
        self.settings = settings or get_settings()

        self.spec: InstrumentSpec | None = None
        self.run_id: int | None = None
        self.journal: Journal | None = None
        self.strategy: Strategy | None = None
        self.feed: BarFeed | None = None
        self.tf5: HigherTimeframeAggregator | None = None
        self.executor: Executor | None = None
        self.risk: RiskGate | None = None
        self.exits: ExitTriggerSet | None = None

        self.market_state: MarketState | None = None
        self._depth_ticker: Ticker | None = None
        self._tape_ticker: Ticker | None = None
        self._quote_ticker: Ticker | None = None

        # Open-position bookkeeping for exit triggers (we own the entry context).
        self._entry_price: float | None = None
        self._entry_ts: dt.datetime | None = None

        self._pending: PendingApproval | None = None
        self._stop_event = asyncio.Event()
        self._running_task: asyncio.Task | None = None

    @property
    def status(self) -> str:
        if self._stop_event.is_set():
            return "stopped"
        if self._running_task is None:
            return "starting"
        return "running"

    # --- lifecycle ---

    async def start(self) -> int:
        from .instruments import parse_instrument

        self.spec = parse_instrument(self.config.symbol)

        await self.ibkr.connect()  # idempotent

        contract_raw = build_contract(self.spec)
        contract = await self.ibkr.qualify(contract_raw)

        run_id = await self._create_run_row()
        self.run_id = run_id
        self.journal = Journal(run_id=run_id, broker=self.broker)

        self.strategy = get_strategy(self.config.strategy_name)(**self.config.strategy_params)
        self.risk = RiskGate(self.settings, self.config.risk_caps)
        self.exits = ExitTriggerSet(getattr(self.strategy, "exit_cfg", ExitConfig()))

        # Market state for L2/T&S features. Always created; subscriptions
        # are opt-in via config.
        self.market_state = MarketState()

        # Optional NBBO quote for LMT pricing.
        if self.config.order_type.upper() == "LMT":
            try:
                self._quote_ticker = self.ibkr.subscribe_quote(contract)
            except Exception:
                logger.exception("failed to subscribe NBBO quote; LMT pricing will fail")

        # Optional L2 + T&S subscriptions.
        if self.config.enable_depth:
            try:
                self._depth_ticker = self.ibkr.subscribe_depth(
                    contract, self.market_state, num_rows=10
                )
            except Exception as e:
                logger.exception("depth subscription failed")
                await self.journal.record(
                    "error",
                    {"where": "subscribe_depth", "error": f"{type(e).__name__}: {e}"},
                )

        if self.config.enable_tape:
            try:
                self._tape_ticker = self.ibkr.subscribe_tape(
                    contract, self.market_state, tick_type="AllLast"
                )
            except Exception as e:
                logger.exception("tape subscription failed")
                await self.journal.record(
                    "error",
                    {"where": "subscribe_tape", "error": f"{type(e).__name__}: {e}"},
                )

        self.executor = Executor(
            run_id=run_id,
            symbol_display=self.spec.display,
            contract=contract,
            ibkr=self.ibkr,
            journal=self.journal,
        )

        # 5m aggregator (always wired - cheap, and the FirstPullback strategy
        # needs it). For single-TF strategies it just calls a no-op on_5m_bar.
        self.tf5 = HigherTimeframeAggregator(window_minutes=5, on_close=self._on_5m_bar)

        await self.journal.record(
            "engine_start",
            {
                "symbol": self.spec.display,
                "instrument": self.spec.instrument,
                "strategy": self.config.strategy_name,
                "params": self.config.strategy_params,
                "quantity": self.config.quantity,
                "autonomous": self.config.autonomous,
                "order_type": self.config.order_type,
                "limit_offset_cents": self.config.limit_offset_cents,
                "sell_anchor": self.config.sell_anchor,
                "enable_depth": self.config.enable_depth,
                "enable_tape": self.config.enable_tape,
                "ibkr_account": self.ibkr.account,
                "market_data_type": self.settings.ibkr_market_data_type,
                "risk_caps": self._risk_caps_dict(),
                "dtd_context": dict(self.config.dtd_context),
            },
        )
        await self.journal.record(
            "ibkr_connected",
            {"account": self.ibkr.account, "client_id": self.settings.ibkr_client_id},
        )

        await self._set_run_status("running")

        self.feed = BarFeed(
            self.ibkr,
            contract,
            self.spec.what_to_show,
            self._on_bar,
        )
        self.feed.start()

        return run_id

    async def stop(self, reason: str = "user_stop") -> None:
        if self._stop_event.is_set():
            return
        self._stop_event.set()

        if self.feed is not None:
            self.feed.stop()

        if self._depth_ticker is not None and self.market_state is not None:
            self.ibkr.cancel_depth(self._depth_ticker, self.market_state)
            self._depth_ticker = None
        if self._tape_ticker is not None and self.market_state is not None:
            self.ibkr.cancel_tape(self._tape_ticker, self.market_state)
            self._tape_ticker = None
        if self._quote_ticker is not None:
            self.ibkr.cancel_quote(self._quote_ticker)
            self._quote_ticker = None

        if self._pending is not None and not self._pending.future.done():
            self._pending.future.set_result(False)

        if self.journal is not None:
            await self.journal.record(
                "engine_stop",
                {
                    "reason": reason,
                    "realized_pnl": self.risk.state.realized_pnl_usd if self.risk else 0.0,
                    "trades_count": self.risk.state.trades_count if self.risk else 0,
                },
            )
        await self._set_run_status("stopped", reason=reason)

    # --- bar consumers ---

    async def _on_bar(self, bar: Bar) -> None:
        if self._stop_event.is_set():
            return
        assert self.journal is not None
        assert self.strategy is not None
        assert self.risk is not None
        assert self.executor is not None
        assert self.exits is not None
        assert self.tf5 is not None

        # Push to 5m aggregator (it will call _on_5m_bar when a bucket closes).
        await self.tf5.push(bar)

        # Journal the 1m bar.
        await self.journal.record(
            "bar",
            {
                "ts": bar.ts.isoformat(),
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
            },
        )

        # Strategy on_bar -> may emit ENTER_LONG.
        signal = self.strategy.on_bar(bar)

        # Feature snapshot for the live panel + downstream gates.
        snap = self._snapshot_features(bar.ts)
        await self.journal.record(
            "indicator",
            {
                "strategy": self.strategy.snapshot(),
                "features": snap.to_dict() if snap else None,
                "bar_ts": bar.ts.isoformat(),
            },
        )

        # Persist the bar + indicator snapshot.
        await self._persist_bar(bar, self.strategy.snapshot())

        # ---- Exit triggers (if we're in a position) ----
        if self.risk.state.open_position_qty > 0 and self._entry_price is not None:
            exit_inputs = self._make_exit_inputs(bar, snap)
            decision = self.exits.on_bar(exit_inputs)
            if decision is not None:
                await self._handle_exit_decision(bar, decision)

        # ---- Entry signal handling ----
        if signal is None:
            return
        await self.journal.record(
            "signal",
            {
                "kind": signal.kind.value,
                "ts": signal.ts.isoformat(),
                "price": signal.price,
                "reason": signal.reason,
                "extras": signal.extras or {},
            },
        )
        if signal.kind == SignalKind.ENTER_LONG:
            await self._handle_enter(bar, signal, snap)
        elif signal.kind == SignalKind.EXIT_LONG:
            # Legacy strategies still emit EXIT_LONG signals; honour them.
            await self._handle_exit_signal(signal)
        else:
            await self.journal.record(
                "error", {"where": "_on_bar", "msg": f"unsupported signal kind {signal.kind}"}
            )

    async def _on_5m_bar(self, bar: Bar) -> None:
        if self._stop_event.is_set() or self.strategy is None or self.journal is None:
            return
        self.strategy.on_5m_bar(bar)
        await self.journal.record(
            "indicator",
            {"strategy": self.strategy.snapshot(), "tf": "5m", "bar_ts": bar.ts.isoformat()},
        )

    # --- entry path ---

    async def _handle_enter(self, bar: Bar, signal: Signal, snap: FeatureSnapshot | None) -> None:
        assert self.journal is not None
        assert self.risk is not None
        assert self.executor is not None
        assert self.exits is not None
        assert self.strategy is not None

        # ---- Microstructure last-look ----
        # For the FirstPullback strategy, do one more pass on L2/T&S right
        # before placing the order, using the latest snapshot.
        if isinstance(self.strategy, FirstPullbackLong):
            passed, failures, notes = self.strategy.evaluate_microstructure_gates(snapshot=snap)
            await self.journal.record(
                "decision",
                {
                    "stage": "microstructure_gate",
                    "passed": passed,
                    "failures": failures,
                    "notes": notes,
                },
            )
            if not passed:
                # Unlatch optimistic in_position; strategy will retry next bar.
                self.strategy.mark_exited()
                return

        # ---- Risk gate ----
        decision = self.risk.can_enter(
            intended_qty=self.config.quantity, intended_price=signal.price
        )
        if not decision.allowed:
            self.strategy.mark_exited()
            await self.journal.record(
                "risk_block",
                {"action": "enter", "reasons": list(decision.reasons), "signal_ts": signal.ts.isoformat()},
            )
            return

        # ---- Approval gate ----
        if not self.config.autonomous:
            loop = asyncio.get_running_loop()
            self._pending = PendingApproval(
                signal=signal,
                intended_qty=self.config.quantity,
                future=loop.create_future(),
            )
            await self.journal.record(
                "ready_for_approval",
                {
                    "signal_kind": signal.kind.value,
                    "ts": signal.ts.isoformat(),
                    "price": signal.price,
                    "intended_qty": self.config.quantity,
                    "reason": signal.reason,
                },
            )
            approved = await self._pending.future
            self._pending = None
            if not approved:
                self.strategy.mark_exited()
                await self.journal.record("approval_rejected", {"ts": signal.ts.isoformat()})
                return
            await self.journal.record("approval_granted", {"ts": signal.ts.isoformat()})
        else:
            await self.journal.record(
                "decision",
                {"action": "auto_execute_enter", "qty": self.config.quantity, "ts": signal.ts.isoformat()},
            )

        # ---- Submit ----
        self.risk.record_open(self.config.quantity)
        self._entry_price = signal.price
        self._entry_ts = signal.ts

        trade = await self.executor.execute(
            signal=signal,
            side="BUY",
            quantity=self.config.quantity,
            order_type=self.config.order_type,
            limit_offset_cents=self.config.limit_offset_cents,
            sell_anchor=self.config.sell_anchor,
            cancel_after_seconds=self.config.cancel_lmt_after_seconds,
            quote_ticker=self._quote_ticker,
        )
        if trade is None:
            # Submit failed - roll back state.
            self.risk.record_close(realized_pnl_usd=0.0)
            self.strategy.mark_exited()
            self._entry_price = None
            self._entry_ts = None
            return

        # Open the exit-trigger framework against a sensible stop suggestion.
        # The FirstPullback strategy exposes `suggest_stop_price`; legacy
        # strategies don't, in which case we fall back to entry - 1%.
        stop_price = signal.extras.get("stop_suggestion") if signal.extras else None
        if not isinstance(stop_price, (int, float)) or stop_price <= 0:
            stop_price = signal.price * 0.99
        try:
            self.exits.open(
                entry_price=signal.price,
                stop_price=float(stop_price),
                entry_ts=signal.ts,
                quantity=self.config.quantity,
            )
        except ValueError:
            # If stop >= entry (shouldn't happen for longs but be defensive),
            # widen to 1% below entry.
            self.exits.open(
                entry_price=signal.price,
                stop_price=signal.price * 0.99,
                entry_ts=signal.ts,
                quantity=self.config.quantity,
            )

        self.strategy.mark_entered()

    # --- exit path ---

    async def _handle_exit_decision(self, bar: Bar, decision: ExitDecision) -> None:
        """Exit triggered by the framework (NOT a strategy-emitted EXIT_LONG)."""
        assert self.journal is not None
        assert self.risk is not None
        assert self.executor is not None
        assert self.exits is not None
        assert self.strategy is not None

        await self.journal.record(
            "decision",
            {
                "stage": "exit_trigger",
                "kind": decision.kind.value,
                "reason": decision.reason,
                "fraction": decision.fraction,
                "price_observed": decision.price_observed,
                "extras": dict(decision.extras),
            },
        )

        # Translate the fraction into a quantity (round down to int).
        held = self.risk.state.open_position_qty
        qty = max(int(held * decision.fraction), 0)
        if qty == 0:
            qty = held  # never leave a fractional dust position
        qty = min(qty, held)

        synthetic_signal = Signal(
            kind=SignalKind.EXIT_LONG,
            ts=bar.ts,
            price=bar.close,
            reason=f"exit_trigger={decision.kind.value}: {decision.reason}",
            extras={"exit_trigger": decision.kind.value, **dict(decision.extras)},
        )

        await self.executor.execute(
            signal=synthetic_signal,
            side="SELL",
            quantity=qty,
            order_type=self.config.order_type,
            limit_offset_cents=self.config.limit_offset_cents,
            sell_anchor=self.config.sell_anchor,
            cancel_after_seconds=self.config.cancel_lmt_after_seconds,
            quote_ticker=self._quote_ticker,
        )

        # P&L tracked via fill callbacks (TODO once we wire realized pnl back).
        # For now we increment trades_count optimistically on full close.
        if qty >= held:
            self.risk.record_close(realized_pnl_usd=0.0)
            # Track whether this was a losing trade for the backside score.
            if self._entry_price is not None and bar.close < self._entry_price:
                self.strategy.record_failed_setup()
            self._entry_price = None
            self._entry_ts = None
            self.exits.close()
            self.strategy.mark_exited()

    async def _handle_exit_signal(self, signal: Signal) -> None:
        """Legacy EXIT_LONG from a single-TF strategy (e.g. macd_crossover)."""
        assert self.journal is not None
        assert self.risk is not None
        assert self.executor is not None

        if self.risk.state.open_position_qty <= 0:
            await self.journal.record(
                "risk_block",
                {"action": "exit", "reasons": ["no_open_position"], "signal_ts": signal.ts.isoformat()},
            )
            return
        qty = self.risk.state.open_position_qty
        await self.executor.execute(
            signal=signal,
            side="SELL",
            quantity=qty,
            order_type=self.config.order_type,
            limit_offset_cents=self.config.limit_offset_cents,
            sell_anchor=self.config.sell_anchor,
            cancel_after_seconds=self.config.cancel_lmt_after_seconds,
            quote_ticker=self._quote_ticker,
        )
        self.risk.record_close(realized_pnl_usd=0.0)
        self._entry_price = None
        self._entry_ts = None
        if self.exits is not None:
            self.exits.close()
        if self.strategy is not None:
            self.strategy.mark_exited()

    # --- approval API (called from REST handler) ---

    def approve_pending(self) -> bool:
        if self._pending is None or self._pending.future.done():
            return False
        self._pending.future.set_result(True)
        return True

    def reject_pending(self) -> bool:
        if self._pending is None or self._pending.future.done():
            return False
        self._pending.future.set_result(False)
        return True

    # --- features ---

    def _snapshot_features(self, ts: dt.datetime) -> FeatureSnapshot | None:
        if self.market_state is None:
            return None
        return compute_snapshot(self.market_state, now=ts)

    def _make_exit_inputs(self, bar: Bar, snap: FeatureSnapshot | None) -> ExitEvaluationInputs:
        assert self.strategy is not None

        # Pull macd_1m_histogram from the strategy snapshot.
        s = self.strategy.snapshot()
        macd_1m_hist = s.get("macd_1m_hist")
        if macd_1m_hist is None:
            macd_1m_hist = s.get("macd_histogram")
        macd_1m_hist_prev = s.get("prev_histogram")  # legacy strategy exposes this

        vw_state = s.get("vwap_state")
        above_vwap: bool | None
        if vw_state == "above":
            above_vwap = True
        elif vw_state == "below":
            above_vwap = False
        else:
            above_vwap = None  # at / na / unknown -> N/A

        return ExitEvaluationInputs(
            ts=bar.ts,
            close=bar.close,
            low=bar.low,
            high=bar.high,
            macd_1m_histogram_prev=macd_1m_hist_prev,
            macd_1m_histogram=macd_1m_hist,
            above_vwap=above_vwap,
            feature_snapshot=snap,
        )

    # --- DB helpers ---

    def _risk_caps_dict(self) -> dict[str, Any]:
        c = self.config.risk_caps
        return {
            "max_trades_per_run": c.max_trades_per_run,
            "max_position_value_usd": c.max_position_value_usd,
            "max_position_qty": c.max_position_qty,
            "max_daily_loss_usd": c.max_daily_loss_usd,
        }

    async def _create_run_row(self) -> int:
        assert self.spec is not None
        async with session_scope() as s:
            row = EngineRun(
                symbol=self.spec.display,
                instrument_type=self.spec.instrument,
                strategy_name=self.config.strategy_name,
                params=self.config.strategy_params,
                risk_caps=self._risk_caps_dict(),
                autonomous=self.config.autonomous,
                market_data_type=self.settings.ibkr_market_data_type,
                ibkr_client_id=self.settings.ibkr_client_id,
                ibkr_account=self.ibkr.account,
                status="starting",
                dtd_context=dict(self.config.dtd_context),
                order_type=self.config.order_type,
                limit_offset_cents=Decimal(str(self.config.limit_offset_cents)),
                sell_anchor=self.config.sell_anchor,
                enable_depth=self.config.enable_depth,
                enable_tape=self.config.enable_tape,
            )
            s.add(row)
            await s.flush()
            return row.id

    async def _set_run_status(self, status: str, reason: str | None = None) -> None:
        if self.run_id is None:
            return
        async with session_scope() as s:
            row = await s.get(EngineRun, self.run_id)
            if row is None:
                return
            row.status = status
            if status in ("stopped", "error"):
                row.stopped_at = dt.datetime.now(dt.timezone.utc)
                if reason:
                    row.stop_reason = reason

    async def _persist_bar(self, bar: Bar, snapshot: dict[str, Any]) -> None:
        if self.run_id is None:
            return
        macd_line = snapshot.get("macd_line")
        macd_signal = snapshot.get("macd_signal")
        macd_hist = snapshot.get("macd_histogram") or snapshot.get("macd_1m_hist")
        try:
            async with session_scope() as s:
                s.add(
                    BarAggregate(
                        run_id=self.run_id,
                        ts=bar.ts,
                        open=Decimal(str(bar.open)),
                        high=Decimal(str(bar.high)),
                        low=Decimal(str(bar.low)),
                        close=Decimal(str(bar.close)),
                        volume=Decimal(str(bar.volume)),
                        macd_line=Decimal(str(macd_line)) if macd_line is not None else None,
                        macd_signal=Decimal(str(macd_signal)) if macd_signal is not None else None,
                        macd_hist=Decimal(str(macd_hist)) if macd_hist is not None else None,
                    )
                )
        except Exception:
            logger.exception("failed to persist bar_aggregate")
