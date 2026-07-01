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
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ib_async import Contract, Ticker

from day_trade.config import Settings, get_settings
from day_trade.db.models import BarAggregate, EngineRun
from day_trade.db.session import session_scope
from day_trade.ws import topics as T
from day_trade.ws.broker import MessageBroker

from .bars import BarFeed, PartialBar
from .executor import Executor
from .exits import ExitConfig, ExitDecision, ExitEvaluationInputs, ExitTriggerSet
from .features import FeatureSnapshot, compute_snapshot
from .ibkr_client import IBKRClient, is_permanent_symbol_error
from .instruments import InstrumentSpec, build_contract
from .journal import Journal
from .multitf import HigherTimeframeAggregator
from .orderbook import MarketState
from .portfolio_risk import PortfolioRiskGate
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


@dataclass(slots=True)
class _PendingEntry:
    """A BUY order that has been submitted to IBKR but not yet confirmed
    filled OR cancelled with zero fills. Held by the engine between
    `_handle_enter` (submit) and `_on_entry_fill` / `_on_entry_status`
    (promote or rollback). The exit framework, risk.state position size,
    and `_entry_price` are all gated on the FIRST fill so a cancelled
    BUY never leaves us with a phantom position.
    """

    signal: Signal
    trade: Any                          # ib_async Trade (kept untyped to avoid import cycle)
    quantity: int
    stop_suggestion: float
    # Handler currently registered with IBKRClient for this order's
    # error events, if any. Cleared when the order reaches a terminal
    # state so the client's registry doesn't leak.
    error_handler: Any = None


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
    # When True (default, safer): the FirstPullback gate requires 5m MACD
    # histogram > 0 and not falling, which is Ross's "broader trend filter".
    # When False: the engine ignores 5m MACD entirely and trades off 1m MACD
    # + VWAP + backside + trigger only. Useful for fast-pivot scenarios on
    # brand-new movers where 5m MACD hasn't warmed up yet (needs ~26 5m bars
    # = ~130 minutes of trading history). Caveat emptor: trading without the
    # 5m context filter catches more false starts.
    require_5m_macd: bool = True
    # Sub-bar evaluation cadence. The strategy and the exit framework are
    # invoked on this clock (rate-limited from IBKR's 5s real-time bar
    # callbacks) in addition to the 1m bar-close path. 10s matches the
    # Ross-style intra-candle execution window where the pullback-break
    # trigger should fire the instant price ticks above the test high,
    # and where L2 distress should bail before the minute is up. Set to
    # 0 to disable tick evaluation (legacy 1m-only behaviour).
    eval_tick_seconds: float = 10.0
    dtd_context: dict[str, Any] = field(default_factory=dict)


class TradingEngine:
    def __init__(
        self,
        *,
        config: EngineConfig,
        ibkr: IBKRClient,
        broker: MessageBroker,
        settings: Settings | None = None,
        portfolio_risk: PortfolioRiskGate | None = None,
    ) -> None:
        self.config = config
        self.ibkr = ibkr
        self.broker = broker
        self.settings = settings or get_settings()
        # When set, the engine consults this gate before submitting any
        # entry order and releases it on position-flat. The production
        # EngineRegistry always wires this in. Tests that construct a
        # TradingEngine directly may pass None to skip mutex enforcement
        # (engine behaves as v1.1). See docs/multi_engine_design.md.
        self.portfolio_risk = portfolio_risk

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

        # Wall-clock timestamp of the most recent sub-bar evaluation
        # tick. Used to rate-limit `_on_tick` from the 5s partial-bar
        # callback path down to `config.eval_tick_seconds` (default
        # 10s).
        self._last_tick_eval_at: dt.datetime | None = None

        # True iff WE currently hold the portfolio mutex. Used to ensure
        # we only release a mutex we ourselves acquired (defense-in-depth
        # for the asymmetric release case where stop() runs unexpectedly).
        self._holds_portfolio_mutex: bool = False

        self._pending: PendingApproval | None = None
        # An unfilled BUY in flight (post-submit, pre-first-fill). Populated
        # by _handle_enter; cleared by _on_entry_fill (promote to in-position)
        # or _on_entry_status (rollback if cancelled with 0 fills). Without
        # this, the engine optimistically opened the exit framework on
        # SUBMIT — so a BUY that later cancelled left a "phantom position"
        # that subsequent bars would try to exit. See HKIT incident
        # Fri 26 Jun PM.
        self._pending_entry: _PendingEntry | None = None
        self._stop_event = asyncio.Event()
        self._running_task: asyncio.Task | None = None

    @property
    def status(self) -> str:
        if self._stop_event.is_set():
            return "stopped"
        # `feed` is the last thing wired in start(), right before it
        # returns. Once it's non-None we're past bootstrap and live bars
        # are flowing into the strategy — i.e. genuinely running.
        # (The legacy _running_task field was a dead check; the engine
        # doesn't run as a top-level task, it runs via callbacks.)
        if self.feed is None:
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

        # Strategy params can include a `trend` (TrendGateConfig) for the
        # FirstPullback family. We respect anything the caller passed explicitly,
        # but if the caller is using the simple `require_5m_macd` boolean toggle
        # and hasn't provided their own `trend`, we synthesize one here so the
        # engine config and strategy config stay consistent.
        strategy_params = dict(self.config.strategy_params)
        if (
            self.config.strategy_name == "first_pullback_long"
            and not self.config.require_5m_macd
            and "trend" not in strategy_params
        ):
            from .strategies.first_pullback_long import TrendGateConfig

            strategy_params["trend"] = TrendGateConfig(
                require_5m_histogram_positive=False,
                require_5m_histogram_not_falling=False,
            )

        self.strategy = get_strategy(self.config.strategy_name)(**strategy_params)
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
                "require_5m_macd": self.config.require_5m_macd,
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

        # Warm up indicators (1m MACD, 5m MACD, VWAP if applicable, pullback
        # history) with recent historical bars from IBKR. Without this the
        # engine would need ~26 minutes of live 1m bars before 1m MACD becomes
        # available, and ~130 minutes before 5m MACD does. With this, every
        # arm is immediately useful.
        try:
            await self._bootstrap_indicators(contract)
        except Exception as e:
            # Bootstrap failure is non-fatal: the engine falls back to live
            # warm-up. We journal the error so it shows up in the audit log.
            logger.exception("indicator bootstrap failed; falling back to live warm-up")
            await self.journal.record(
                "error",
                {"where": "bootstrap_indicators", "error": f"{type(e).__name__}: {e}"},
            )

        self.feed = BarFeed(
            self.ibkr,
            contract,
            self.spec.what_to_show,
            self._on_bar,
            on_partial_bar=self._on_partial_bar,
        )
        self.feed.start()

        return run_id

    async def _bootstrap_indicators(self, contract: Contract) -> None:
        """Pull recent 1m historical bars and replay them into the strategy
        and 5m aggregator so MACD / VWAP / pullback history are ready to
        trade immediately on the first live bar.

        Signals emitted by the strategy during replay are discarded - they
        are based on stale data and must not be executed. The journal records
        a single `bootstrap` event summarising what was preloaded; no
        per-bar `bar` / `indicator` events are written (those are for live
        bars only).
        """
        assert self.strategy is not None
        assert self.tf5 is not None
        assert self.journal is not None

        # 2 trading days of 1m bars. This is the TradingView-style "carry
        # through across session boundaries" approach: 5m MACD warms instantly
        # on any name that traded yesterday (even a fresh Ross-scanner pivot
        # mid-session). A 4-hour window was insufficient for hot-start: e.g.
        # FRTT's first 80 minutes of pre-market yields only 16 5m bars, but
        # 5m MACD(12/26/9) needs ~26 5m bars to compute. With "2 D" IBKR
        # returns yesterday's full session + today-so-far, comfortably warming
        # both timeframes.
        #
        # Trade-off: cross-session MACD inherits any overnight gap as a real
        # bar (so a +400% gap-up reads as "huge histogram"). For our intended
        # use case (catching gap-and-go small caps) this signal is feature,
        # not bug - it tells the strategy "this is on the front side of a
        # massive move", which is what we want.
        duration_str = "2 D"

        raw_bars = await self.ibkr.fetch_historical_1m_bars(
            contract,
            self.spec.what_to_show,
            duration_str=duration_str,
            use_rth=False,
        )
        if not raw_bars:
            await self.journal.record(
                "bootstrap",
                {"bars_1m": 0, "note": "no historical bars returned by IBKR"},
            )
            return

        # Convert ib_async BarData -> our Bar. BarData.date is a date or
        # datetime depending on barSizeSetting; for "1 min" it's a tz-aware
        # datetime that represents the bar START. Engine convention is bar
        # CLOSE, so we add one minute.
        bars_1m: list[Bar] = []
        for bd in raw_bars:
            ts = bd.date
            if not isinstance(ts, dt.datetime):
                # Defensive: skip date-only entries (would only happen for
                # daily/weekly bars, which we don't request).
                continue
            close_ts = ts + dt.timedelta(minutes=1)
            bars_1m.append(
                Bar(
                    ts=close_ts,
                    open=float(bd.open),
                    high=float(bd.high),
                    low=float(bd.low),
                    close=float(bd.close),
                    volume=float(bd.volume) if bd.volume is not None else 0.0,
                )
            )

        # Replay 1m bars into the strategy. Discard any emitted signals -
        # they are based on stale data and must not be acted upon.
        #
        # IMPORTANT: `strategy.on_bar` has side effects beyond indicator
        # state (notably the BacksideState latches in FirstPullbackLong).
        # We let those side effects accumulate here, then call
        # `finalize_bootstrap` below to wipe the live-session latches
        # while preserving warm indicators. Without that clear, the
        # backside gate's "1m MACD has already crossed down today" latch
        # would fire immediately on the first live bar because the
        # replay almost certainly contained at least one cross-down.
        for bar in bars_1m:
            _ = self.strategy.on_bar(bar)

        # Prime the 5m aggregator with the same 1m bars. It returns the list
        # of 5m bars that closed during the backfill; we feed those into
        # strategy.on_5m_bar to warm up the 5m MACD.
        emitted_5m = self.tf5.prime_with_history(bars_1m)
        for bar5m in emitted_5m:
            self.strategy.on_5m_bar(bar5m)

        # Compute reference levels from the replayed bars, then hand them
        # to the strategy as part of finalising the bootstrap.
        pmhod, pdhod = _compute_session_levels(bars_1m)
        self.strategy.finalize_bootstrap(pmhod=pmhod, pdhod=pdhod)

        first_ts = bars_1m[0].ts.isoformat() if bars_1m else None
        last_ts = bars_1m[-1].ts.isoformat() if bars_1m else None
        snap = self.strategy.snapshot()
        await self.journal.record(
            "bootstrap",
            {
                "bars_1m": len(bars_1m),
                "bars_5m_emitted": len(emitted_5m),
                "first_bar_close_utc": first_ts,
                "last_bar_close_utc": last_ts,
                "macd_1m_hist_after": snap.get("macd_1m_hist"),
                "macd_5m_hist_after": snap.get("macd_5m_hist"),
                "vwap_after": snap.get("vwap"),
                "vwap_state_after": snap.get("vwap_state"),
                "pmhod": pmhod,
                "pdhod": pdhod,
            },
        )
        logger.info(
            "bootstrap complete: replayed %d 1m bars, %d 5m bars; "
            "macd_1m_hist=%s macd_5m_hist=%s pmhod=%s pdhod=%s",
            len(bars_1m), len(emitted_5m),
            snap.get("macd_1m_hist"), snap.get("macd_5m_hist"),
            pmhod, pdhod,
        )

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

        # If we have a BUY in flight (submitted but not yet filled or
        # cancelled), cancel it at IBKR. The _on_entry_status callback
        # will then see status=Cancelled+filled=0 and roll back, but we
        # also clear _pending_entry locally here so the rollback path
        # is robust even if the status event arrives after we've torn
        # down (e.g. if the WS disconnects mid-shutdown).
        if self._pending_entry is not None:
            pending_trade = self._pending_entry.trade
            self._pending_entry = None
            try:
                self.ibkr.cancel_order(pending_trade)
            except Exception:
                logger.exception("failed to cancel pending BUY during stop")
            if self.journal is not None:
                await self.journal.record(
                    "entry_cancelled_without_fill",
                    {
                        "ibkr_order_id": getattr(pending_trade.order, "orderId", None),
                        "filled": int(getattr(pending_trade.orderStatus, "filled", 0) or 0),
                        "msg": "engine stopping; pending BUY cancelled",
                    },
                )

        # Stop-while-holding-the-mutex semantics (Phase 1):
        #   If we still hold the portfolio mutex when stopped, release it
        #   with pnl=0 so sibling engines can resume. If we also have an
        #   open IBKR position, that position lingers — Phase 1 does NOT
        #   auto-close on stop (matches legacy single-engine behaviour),
        #   so we log a warning. Phase 1 safety hardening (in the design
        #   doc) covers the orphan-position recovery on backend restart.
        if self._holds_portfolio_mutex:
            open_qty = self.risk.state.open_position_qty if self.risk else 0
            if open_qty > 0 and self.journal is not None:
                await self.journal.record(
                    "warning",
                    {
                        "where": "stop",
                        "msg": (
                            "stopping engine while position is open at IBKR; "
                            "portfolio mutex released to unblock sibling "
                            "engines but the IBKR position must be closed "
                            "manually"
                        ),
                        "open_qty": open_qty,
                        "entry_price": self._entry_price,
                    },
                )
            await self._release_portfolio_mutex(realized_pnl_usd=0.0)

        if self.journal is not None:
            await self.journal.record(
                "engine_stop",
                {
                    "reason": reason,
                    "realized_pnl": self.risk.state.realized_pnl_usd if self.risk else 0.0,
                    "trades_count": self.risk.state.trades_count if self.risk else 0,
                },
            )
        # Belt-and-suspenders: persist final stats even if a close path
        # somehow didn't (e.g. stop called while in position, or future
        # exit paths added without remembering to call _persist_run_stats).
        await self._persist_run_stats()
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

    async def _on_partial_bar(self, snapshot: PartialBar) -> None:
        """Publish the in-progress 1m bar's running OHLC for live UI updates
        AND drive the sub-bar evaluation tick.

        Fires every ~5 seconds (each time IBKR emits a new 5s real-time
        bar). Two concerns:

          1. UI broadcast — unconditionally publish the running OHLC
             onto `ENGINE_BAR_TICK` so the engine page can render the
             forming candle live. Cheap (no DB, no strategy mutation).

          2. Sub-bar evaluation — rate-limited at
             `config.eval_tick_seconds` (default 10s), drive
             `_on_tick(snapshot)` which:
               - runs the entry gate stack against the partial bar
                 (Ross-aggressive mid-candle pullback break) and
               - re-checks price-driven + L2 exit triggers
             without touching MACD/VWAP cumulative state or
             consecutive-bar counters. Per spec: indicators stay on
             closed-bar cadence (1m / 5m), only the DECISION cadence
             accelerates.
        """
        if self._stop_event.is_set() or self.run_id is None:
            return
        try:
            await self.broker.publish(
                T.ENGINE_BAR_TICK,
                {
                    "run_id": self.run_id,
                    "event_type": "bar_tick",
                    "ts": dt.datetime.now(dt.UTC).isoformat(),
                    "payload": {
                        "ts": snapshot.ts.isoformat(),
                        "open": snapshot.open,
                        "high": snapshot.high,
                        "low": snapshot.low,
                        "close": snapshot.close,
                        "volume": snapshot.volume,
                    },
                },
            )
        except Exception:
            logger.exception("failed to publish bar_tick")

        # Sub-bar evaluation tick. Skip when disabled (eval_tick_seconds
        # <= 0) or before the rate-limit window has elapsed.
        cadence = float(self.config.eval_tick_seconds)
        if cadence <= 0:
            return
        now = dt.datetime.now(dt.UTC)
        last = self._last_tick_eval_at
        if last is not None and (now - last).total_seconds() < cadence:
            return
        self._last_tick_eval_at = now
        try:
            await self._on_tick(snapshot)
        except Exception:
            logger.exception("_on_tick raised")

    async def _on_tick(self, partial: PartialBar) -> None:
        """Sub-bar evaluation. Called every `eval_tick_seconds` from
        `_on_partial_bar`.

        Read-only on indicator state. Routes any emitted signal /
        exit decision through the existing `_handle_enter` /
        `_handle_exit_decision` paths so all the downstream concerns
        (risk gate, microstructure last-look, portfolio mutex,
        executor, journal) behave identically to the closed-bar path.
        Adds `"is_mid_candle": True` to the signal extras so the
        journal can distinguish the two cadences in post-mortem.
        """
        if self._stop_event.is_set():
            return
        if self.strategy is None or self.risk is None or self.exits is None:
            return

        # Synthesise a Bar from the partial OHLC. Carries the bar's
        # close-time (same value the eventual closed Bar will carry),
        # which keeps timestamps coherent between tick and bar-close
        # journal entries.
        synthetic = Bar(
            ts=partial.ts,
            open=partial.open,
            high=partial.high,
            low=partial.low,
            close=partial.close,
            volume=partial.volume,
        )

        # Build the feature snapshot once and reuse across both paths.
        snap = self._snapshot_features(synthetic.ts)

        # ---- Exit triggers (if we're in a position) ----
        if self.risk.state.open_position_qty > 0 and self._entry_price is not None:
            exit_inputs = self._make_exit_inputs(synthetic, snap)
            decision = self.exits.on_tick(exit_inputs)
            if decision is not None:
                await self._handle_exit_decision(synthetic, decision)
                # An exit just fired — don't also evaluate an entry on
                # the same tick. The next tick will reassess cleanly.
                return

        # ---- Entry tick eval ----
        signal = self.strategy.on_tick(synthetic)
        if signal is None:
            return

        # Real event — now we journal. (No `bar` / `indicator` events;
        # those are reserved for the closed-bar cadence so the DB
        # doesn't fill up with 6 tick-eval snapshots per minute.)
        if self.journal is not None:
            await self.journal.record(
                "signal",
                {
                    "kind": signal.kind.value,
                    "ts": signal.ts.isoformat(),
                    "price": signal.price,
                    "reason": signal.reason,
                    "extras": signal.extras or {},
                    "cadence": "tick",
                },
            )
        if signal.kind == SignalKind.ENTER_LONG:
            await self._handle_enter(synthetic, signal, snap)
        elif signal.kind == SignalKind.EXIT_LONG:
            await self._handle_exit_signal(signal)
        else:
            if self.journal is not None:
                await self.journal.record(
                    "error",
                    {
                        "where": "_on_tick",
                        "msg": f"unsupported signal kind {signal.kind}",
                    },
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

        # ---- Portfolio execution mutex ----
        # In multi-engine mode, every entry must acquire the portfolio-wide
        # mutex (enforces 1 open position across all engines) and pass the
        # portfolio-level caps (daily loss kill switch, total-trades-per-day).
        # Deferred until just before submit so we don't block siblings during
        # the approval wait in non-autonomous mode. See
        # docs/multi_engine_design.md decisions 1 + 2.
        if self.portfolio_risk is not None:
            acquire = await self.portfolio_risk.try_acquire_for_entry(
                symbol=self.config.symbol,
                intended_qty=self.config.quantity,
            )
            if not acquire.granted:
                # Audit "would-have-fired" event so calibration can see the
                # alternative setup we passed on (decision 2: full
                # observability of blocked entries).
                await self.journal.record(
                    "entry_blocked_by_portfolio_mutex",
                    {
                        "symbol": self.config.symbol,
                        "intended_qty": self.config.quantity,
                        "reason": acquire.reason,
                        "current_holder": acquire.holder,
                        "signal": {
                            "kind": signal.kind.value,
                            "ts": signal.ts.isoformat(),
                            "price": signal.price,
                            "reason": signal.reason,
                            "extras": signal.extras or {},
                        },
                    },
                )
                self.strategy.mark_exited()
                return
            self._holds_portfolio_mutex = True

        # ---- Submit (no position state opened yet — see _PendingEntry) ----
        #
        # Previously we did `risk.record_open(qty)` and `exits.open(...)`
        # BEFORE the order even left the wire — so if IBKR later cancelled
        # the BUY with 0 fills (wide-spread micro-cap, halt, etc.), the
        # engine was left with a "phantom position": next bar's exit
        # framework would fire HARD_STOP and submit a SELL to close
        # something that never existed. Surfaced by HKIT incident
        # Fri 26 Jun PM. Now we wait for IBKR to confirm the first fill
        # before opening any position state. See `_on_entry_fill` /
        # `_on_entry_status`.
        stop_price = signal.extras.get("stop_suggestion") if signal.extras else None
        if not isinstance(stop_price, (int, float)) or stop_price <= 0:
            stop_price = signal.price * 0.99

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
            # Submit failed (never made it over the wire). Roll back the
            # strategy latch and release the mutex.
            self.strategy.mark_exited()
            await self._release_portfolio_mutex(realized_pnl_usd=0.0)
            return

        # Latch the strategy NOW (even though the order isn't filled) so it
        # doesn't re-emit ENTER_LONG on the very next bar while we wait. We
        # unlatch via mark_exited() if the order ends up cancelled with 0
        # fills (see _on_entry_status).
        self.strategy.mark_entered()

        self._pending_entry = _PendingEntry(
            signal=signal,
            trade=trade,
            quantity=self.config.quantity,
            stop_suggestion=float(stop_price),
        )

        # Wire engine-level fill / status hooks on top of the executor's
        # journaling subscribers. ib_async events accept sync callables
        # only; we forward into asyncio tasks ourselves. These coexist
        # with executor._on_status / executor._on_fill.
        loop = asyncio.get_running_loop()
        trade.fillEvent += lambda t, _fill: loop.create_task(self._on_entry_fill(t))
        trade.statusEvent += lambda t: loop.create_task(self._on_entry_status(t))

        # Register a per-order IBKR error handler so we capture the
        # human-readable rejection text (e.g. "closing-only status")
        # that arrives on `errorEvent` separately from the terse
        # `orderStatus.status='Inactive'`. If the text matches a
        # permanent-symbol pattern we schedule an engine stop so we
        # don't burn CPU + IBKR quota retrying the same doomed symbol.
        order_id = int(getattr(trade.order, "orderId", 0) or 0)
        if order_id > 0 and self.ibkr is not None:
            handler = self._make_ibkr_order_error_handler(order_id, signal)
            self.ibkr.register_order_error_handler(order_id, handler)
            self._pending_entry.error_handler = handler

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

        # P&L approximation on full close. The same approximation goes to
        # BOTH the per-engine risk.state and the portfolio mutex so the
        # Active-run panel, Portfolio top bar, and engine_runs row all
        # agree on what happened. The number is `(bar.close - entry_price)
        # * qty` — gross of commissions and using bar close rather than
        # actual fill price. Good enough for the daily kill switch + the
        # Recent Runs table; fill-accurate P&L (avg buy fill vs avg sell
        # fill, net of commissions) is a TODO that ties into the
        # fill-callback refactor planned alongside the exit-framework
        # redesign.
        if qty >= held:
            approx_pnl = self._approx_realized_pnl(bar.close, held)
            self.risk.record_close(realized_pnl_usd=approx_pnl)
            # Track whether this was a losing trade for the backside score.
            if self._entry_price is not None and bar.close < self._entry_price:
                self.strategy.record_failed_setup()
            self._entry_price = None
            self._entry_ts = None
            self.exits.close()
            self.strategy.mark_exited()
            await self._persist_run_stats()
            await self._release_portfolio_mutex(realized_pnl_usd=approx_pnl)

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
        approx_pnl = self._approx_realized_pnl(signal.price, qty)
        self.risk.record_close(realized_pnl_usd=approx_pnl)
        self._entry_price = None
        self._entry_ts = None
        if self.exits is not None:
            self.exits.close()
        if self.strategy is not None:
            self.strategy.mark_exited()
        await self._persist_run_stats()
        await self._release_portfolio_mutex(realized_pnl_usd=approx_pnl)

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

    # --- pending-entry transitions (BUY submitted, awaiting fill) ---

    async def _on_entry_fill(self, trade: Any) -> None:
        """Called by ib_async when our pending BUY gets a fill (partial or
        full). On the FIRST fill we promote the pending entry into a real
        in-position state: set entry price, open the exit framework,
        record the position size on the risk gate. Subsequent partial-fill
        events on the same trade just resize the position to the new
        total filled.

        Race note (TC 2026-07-01 incident): `fillEvent` and `statusEvent`
        both fire from ib_async around the same time. If `statusEvent`
        clears `_pending_entry` on `status=Filled` BEFORE this handler
        runs, we used to bail early and never call `record_open` — the
        risk gate thought there was no position, the auto-arm staleness
        watcher then stopped the engine, and the position was orphaned
        with no exits armed. Fix: `record_open` is now called BEFORE
        any pending-entry check, and `_on_entry_status` no longer
        clears `_pending_entry` on fills (that ownership moved here).
        """
        if self.risk is None or self.journal is None:
            return

        filled = int(getattr(trade.orderStatus, "filled", 0) or 0)
        avg_px = float(getattr(trade.orderStatus, "avgFillPrice", 0.0) or 0.0)
        if filled <= 0 or avg_px <= 0.0:
            return

        # Sync the position size on the risk gate FIRST so that any
        # concurrent observer (auto-arm worker, portfolio checks) sees
        # `has_open_position=True` immediately, even if the promotion
        # branch below can't run because _pending_entry got cleared.
        # Idempotent setter: safe on partial-then-completion fills.
        self.risk.record_open(filled)

        pe = self._pending_entry
        if pe is None or pe.trade is not trade:
            # Race with _on_entry_status or a stray event on a stale
            # trade. record_open already ran so the position IS tracked;
            # we just can't promote (would need pe.signal / stop). Log
            # and return.
            logger.warning(
                "engine.on_entry_fill: no matching pending_entry for order "
                "id=%s filled=%d avg_px=%.4f; position size recorded but "
                "promotion (exits.open, entry_price) skipped",
                getattr(trade.order, "orderId", None), filled, avg_px,
            )
            return

        if self._entry_price is None:
            # First fill: open exits framework anchored to the ACTUAL fill
            # price (better than signal price, which is what we previously
            # used). Stop price comes from the pending entry's
            # `stop_suggestion`.
            self._entry_price = avg_px
            self._entry_ts = pe.signal.ts
            try:
                if self.exits is not None:
                    self.exits.open(
                        entry_price=avg_px,
                        stop_price=pe.stop_suggestion,
                        entry_ts=pe.signal.ts,
                        quantity=filled,
                    )
            except ValueError:
                # Defensive: stop >= entry shouldn't happen for longs.
                if self.exits is not None:
                    self.exits.open(
                        entry_price=avg_px,
                        stop_price=avg_px * 0.99,
                        entry_ts=pe.signal.ts,
                        quantity=filled,
                    )
            # Route through `position_open` (real enum value) so the
            # event actually persists — `entry_promoted` was silently
            # rejected by the DB enum, hiding this whole event class
            # from the audit log.
            await self.journal.record(
                "position_open",
                {
                    "kind": "entry_promoted",
                    "ibkr_order_id": getattr(trade.order, "orderId", None),
                    "filled": filled,
                    "avg_fill_price": avg_px,
                    "stop_price": pe.stop_suggestion,
                    "signal_ts": pe.signal.ts.isoformat(),
                    "msg": (
                        "pending BUY confirmed filled at IBKR; position state "
                        "and exit framework now active"
                    ),
                },
            )

            # Ownership of _pending_entry lifecycle moved here so the
            # promotion path is atomic w.r.t. the pending_entry state.
            # Also unregister the per-order error handler now that the
            # order is fully filled and we don't need to react to
            # further errors on this reqId.
            oid = int(getattr(trade.order, "orderId", 0) or 0)
            handler = pe.error_handler
            self._pending_entry = None
            if oid > 0 and handler is not None and self.ibkr is not None:
                self.ibkr.unregister_order_error_handler(oid, handler)

    def _make_ibkr_order_error_handler(
        self, order_id: int, signal: Signal
    ) -> Callable[[int, str], None]:
        """Build the callback we register with IBKRClient for `order_id`.

        The callback fires from the ib_async thread on any IBKR error
        addressed to this order (reqId == order_id). It:

          1. Journals the raw error via an asyncio task (async-safe from
             a sync callback: schedule with `loop.call_soon_threadsafe`).
          2. Classifies the text as `permanent` or transient. Permanent
             means retrying the same symbol will fail again — we stop
             the engine so the arm slot is freed for a tradeable name.

        We deliberately do NOT roll back the strategy latch here; the
        `_on_entry_status` handler still runs on the same order and
        owns the rollback so the two code paths don't fight over state.
        """
        loop = asyncio.get_running_loop()
        symbol = self.config.symbol

        def _handler(error_code: int, error_string: str) -> None:
            permanent = is_permanent_symbol_error(error_string)
            payload = {
                "ibkr_order_id": order_id,
                "symbol": symbol,
                "code": error_code,
                "message": error_string,
                "permanent": permanent,
                "signal_ts": signal.ts.isoformat(),
            }

            async def _journal_and_maybe_stop() -> None:
                if self.journal is not None:
                    await self.journal.record("error", payload)
                logger.warning(
                    "IBKR order error run=%d oid=%d code=%d permanent=%s: %s",
                    self.run_id or -1, order_id, error_code, permanent, error_string,
                )
                if permanent:
                    # Fire-and-forget stop so we don't block the error
                    # dispatch (which may still be delivering to other
                    # handlers). `stop_reason` gets surfaced in the UI.
                    reason = f"ibkr_permanent_reject:{error_code}:{error_string[:80]}"
                    asyncio.create_task(self._stop_from_error(reason))

            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(_journal_and_maybe_stop())
            )

        return _handler

    async def _stop_from_error(self, reason: str) -> None:
        """Stop the engine because an unrecoverable error was observed.
        Safe to call from an error handler — it awaits the normal stop
        path but swallows any exception so a bad stop doesn't leave the
        error dispatcher in a bad state."""
        try:
            await self.stop(reason=reason)
        except Exception:
            logger.exception("stop-from-error failed for reason=%s", reason)

    async def _on_entry_status(self, trade: Any) -> None:
        """Called by ib_async on every status change for our pending BUY.
        We watch for terminal states: a Cancelled/Inactive status with
        ZERO fills means rollback (release mutex, unlatch strategy);
        Filled or Cancelled-with-partial-fills means clear pending (the
        position is now managed normally by the exit framework).
        """
        pe = self._pending_entry
        if pe is None or pe.trade is not trade:
            return
        if self.journal is None:
            return

        status = str(getattr(trade.orderStatus, "status", "") or "").lower()
        filled = int(getattr(trade.orderStatus, "filled", 0) or 0)

        if status in ("cancelled", "apicancelled", "inactive") and filled == 0:
            # Pure cancel with no fill: roll back ALL the entry-side state
            # we set up in _handle_enter.
            oid = int(getattr(trade.order, "orderId", 0) or 0)
            handler = pe.error_handler
            self._pending_entry = None
            # Journal via the existing enum-safe "error" type so the DB
            # accepts it. (There is no `entry_cancelled_without_fill`
            # enum value — using it would silently fail the INSERT.)
            await self.journal.record(
                "error",
                {
                    "kind": "entry_cancelled_without_fill",
                    "ibkr_order_id": getattr(trade.order, "orderId", None),
                    "status": getattr(trade.orderStatus, "status", None),
                    "filled": filled,
                    "signal_ts": pe.signal.ts.isoformat(),
                    "msg": (
                        "BUY cancelled without filling; no position opened, "
                        "exit framework not armed. Mutex released and "
                        "strategy unlatched."
                    ),
                },
            )
            if self.strategy is not None:
                self.strategy.mark_exited()
            await self._release_portfolio_mutex(realized_pnl_usd=0.0)
            if oid > 0 and handler is not None and self.ibkr is not None:
                self.ibkr.unregister_order_error_handler(oid, handler)
            return

        # NOTE: Terminal states with fills (`filled` or
        # `cancelled/inactive` with partial fills) used to clear
        # `_pending_entry` here. That created a race with
        # `_on_entry_fill`: if this handler ran first, the fill
        # handler saw pe=None and bailed without calling
        # `record_open` / `exits.open`. Ownership of the "fill
        # succeeded" clear moved to `_on_entry_fill` where it is
        # atomic with the promotion of position + exit framework
        # state. See TC 2026-07-01 incident for the full post-mortem.

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

    # --- portfolio mutex helpers ---

    async def _release_portfolio_mutex(self, *, realized_pnl_usd: float) -> None:
        """Release the portfolio mutex if we currently hold it. Idempotent
        and no-op when `portfolio_risk` is None (single-engine mode) or
        when we never acquired it for this lifecycle. The realized P&L is
        added to the portfolio's daily aggregate; when it pushes the
        cumulative loss past the cap, the kill switch trips."""
        if self.portfolio_risk is None or not self._holds_portfolio_mutex:
            return
        try:
            await self.portfolio_risk.release(
                symbol=self.config.symbol,
                realized_pnl_usd=realized_pnl_usd,
            )
        finally:
            self._holds_portfolio_mutex = False

    def _approx_realized_pnl(self, exit_price: float, qty: int) -> float:
        """Approximate realized P&L (gross of commissions) used by the
        portfolio kill switch. Computed as `(exit_price - entry_price) *
        qty` from the strategy's tracked entry price and the bar / signal
        exit price. NOT a substitute for fill-accurate P&L in the trade
        journal — those will come from the IBKR fill callbacks once we
        wire them. Returns 0.0 if entry price is unknown."""
        if self._entry_price is None:
            return 0.0
        return (exit_price - self._entry_price) * float(qty)

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
                row.stopped_at = dt.datetime.now(dt.UTC)
                if reason:
                    row.stop_reason = reason

    async def _persist_run_stats(self) -> None:
        """Snapshot per-engine trade count + realized P&L back onto the
        `engine_runs` row so the Recent Runs table reflects what actually
        happened during this run. Called on every position-flat (in both
        exit paths) and once more from stop() as a final safety net.

        This is the same approximate-P&L value that goes to the portfolio
        mutex, NOT a fill-accurate per-trade aggregation — see the comment
        on `_approx_realized_pnl`. Once we wire fill callbacks back, this
        method will pull from the fill-accurate accumulator instead.
        """
        if self.run_id is None or self.risk is None:
            return
        try:
            async with session_scope() as s:
                row = await s.get(EngineRun, self.run_id)
                if row is None:
                    return
                row.realized_pnl = Decimal(str(self.risk.state.realized_pnl_usd))
                row.trades_count = self.risk.state.trades_count
        except Exception:
            logger.exception("failed to persist run stats")

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


# --- module-level helpers ---


_ET = ZoneInfo("America/New_York")


def _compute_session_levels(
    bars_1m: list[Bar],
) -> tuple[float | None, float | None]:
    """Compute PMHOD and PDHOD from a chronological list of historical
    1-minute bars used to warm indicators at engine start.

    Definitions:
      - PMHOD (today's premarket high of day): the highest `high` of any
        bar whose CLOSE timestamp falls within (04:00, 09:30] ET on the
        same calendar date (in ET) as the most-recent bar in `bars_1m`.
        Returns None if no such bars are present (e.g. engine started
        post-open, or after-hours / weekend with no fresh premarket).
      - PDHOD (previous-day high of day): the highest `high` of any bar
        whose CLOSE timestamp falls within (09:30, 16:00] ET on the
        most-recent calendar date (in ET) strictly earlier than "today"
        that has any such bars. Returns None if no prior-session RTH
        bars are present in the replay window.

    Bar close-time convention: the engine appends 1 minute to IBKR's
    bar-start timestamps, so a close at 09:30:00 ET represents the bar
    covering [09:29:00, 09:30:00) — i.e. the LAST premarket bar.
    Accordingly the boundary tests use `<=` on the premarket close and
    `>` on the RTH start.
    """
    if not bars_1m:
        return None, None

    today_et = bars_1m[-1].ts.astimezone(_ET).date()
    pm_open = dt.time(4, 0)
    rth_open = dt.time(9, 30)
    rth_close = dt.time(16, 0)

    pmhod: float | None = None
    pdhod_by_date: dict[dt.date, float] = {}

    for b in bars_1m:
        ts_et = b.ts.astimezone(_ET)
        d = ts_et.date()
        t = ts_et.time()
        if d == today_et:
            if pm_open < t <= rth_open:
                pmhod = b.high if pmhod is None else max(pmhod, b.high)
        elif d < today_et:
            if rth_open < t <= rth_close:
                prev = pdhod_by_date.get(d)
                pdhod_by_date[d] = b.high if prev is None else max(prev, b.high)

    pdhod: float | None = None
    if pdhod_by_date:
        most_recent_prior = max(pdhod_by_date)
        pdhod = pdhod_by_date[most_recent_prior]

    return pmhod, pdhod
