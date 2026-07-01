"""Thin async wrapper around ib_async.IB for the engine.

Responsibilities:
  - Hold the single shared IBKR connection for the FastAPI process.
  - Enforce the paper-only invariant at connect time (account starts with DU).
  - Set the configured market data type (live | delayed | ...) at connect.
  - Expose a small async API the rest of the engine uses (qualify_contract,
    request_realtime_bars, place_market_order).

We deliberately do NOT try to do auto-reconnect with exponential backoff in
the POC. If the connection drops, the active engine stops with an error
event; the user restarts. We can add reconnect logic once we have a real
session log to learn from.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Callable, Literal

from ib_async import (
    IB,
    Contract,
    LimitOrder,
    MarketOrder,
    RealTimeBarList,
    Ticker,
    Trade,
)

from day_trade.config import Settings, get_settings

from .orderbook import DepthLevel, MarketState, TapeTick

logger = logging.getLogger(__name__)


class IBKRConnectionError(RuntimeError):
    pass


class IBKRSafetyError(RuntimeError):
    """Raised when an IBKR safety invariant is violated (e.g. connected to a
    LIVE account while PAPER_TRADING_ONLY=true)."""


class IBKRSubscriptionError(RuntimeError):
    """Raised when an L2/T&S subscription fails (e.g. no entitlements)."""


# Text patterns (case-insensitive substrings) that indicate an IBKR
# rejection which is PERMANENT for the symbol in the current session.
# When we see one of these on an order, retrying the same symbol is
# guaranteed to fail — we auto-stop the engine to save capacity for
# tradeable symbols. Extend as we encounter new patterns in production
# logs.
_PERMANENT_ORDER_ERROR_PATTERNS: tuple[str, ...] = (
    "closing-only",
    "closing only",
    "closing-only status",
    "not eligible",
    "not tradable",
    "not available for trading",
    "no security definition",
    "security definition has expired",
    "hard to borrow",
    "no shares available",
)

# IBKR error codes that are pure info / warnings and MUST NOT be
# treated as order-fatal (they arrive on the same errorEvent stream as
# real errors). 2103/2104/2105/2106/2158 are farm-connection status;
# 2109 is order events warning about routing; 399 is generic warning.
_IBKR_INFO_CODES: frozenset[int] = frozenset({
    2103, 2104, 2105, 2106, 2107, 2108, 2109, 2158, 202, 399,
})


def is_permanent_symbol_error(text: str) -> bool:
    """Return True if the IBKR error text indicates the symbol cannot
    be traded further this session. Case-insensitive substring match."""
    if not text:
        return False
    lower = text.lower()
    return any(pat in lower for pat in _PERMANENT_ORDER_ERROR_PATTERNS)


# Signature of a per-order error handler: (errorCode, errorString) -> None.
OrderErrorHandler = Callable[[int, str], None]


class IBKRClient:
    """Process-wide IBKR client. Singleton; do not instantiate directly -
    call `get_ibkr_client()`."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._ib = IB()
        self._connected = False
        self._account: str | None = None
        # Concurrency guard: connect() is idempotent and safe to call from
        # multiple engine start requests at once.
        self._connect_lock = asyncio.Lock()
        # Per-order-id error handler registry. `errorEvent` fires with
        # reqId == the ibkr orderId for order-scoped errors, and the
        # engine registers a handler per submitted BUY so it can react
        # to rejections (e.g. "closing-only"). We use a list to allow
        # both the engine and the executor to subscribe; ordering is
        # insertion order.
        self._order_error_handlers: dict[int, list[OrderErrorHandler]] = {}
        self._error_event_bound = False

    def _bind_error_event(self) -> None:
        """Attach the central error dispatcher to `_ib.errorEvent`.
        Called once after the first successful connect. Idempotent."""
        if self._error_event_bound:
            return
        self._ib.errorEvent += self._dispatch_error
        self._error_event_bound = True

    def _dispatch_error(
        self,
        reqId: int,
        errorCode: int,
        errorString: str,
        contract: Contract | None = None,
    ) -> None:
        """Route `errorEvent` to registered per-order handlers.

        ib_async fires `errorEvent(reqId, errorCode, errorString, contract)`.
        For order-scoped errors reqId == orderId; for connection-scoped
        events reqId is -1 or an internal request id. We only dispatch
        to per-order handlers when reqId matches a registered orderId.
        Info-only codes (farm status, warnings) are logged at DEBUG so
        they don't spam the run journal.
        """
        if errorCode in _IBKR_INFO_CODES:
            logger.debug(
                "ibkr info reqId=%s code=%s msg=%s", reqId, errorCode, errorString
            )
            return
        handlers = self._order_error_handlers.get(reqId)
        if not handlers:
            logger.info(
                "ibkr error (unrouted) reqId=%s code=%s msg=%s",
                reqId, errorCode, errorString,
            )
            return
        for h in handlers:
            try:
                h(errorCode, errorString)
            except Exception:
                logger.exception(
                    "order error handler raised for reqId=%s code=%s",
                    reqId, errorCode,
                )

    def register_order_error_handler(
        self, order_id: int, handler: OrderErrorHandler
    ) -> None:
        """Register `handler` to receive IBKR errors for `order_id`.
        Multiple handlers per order are allowed and dispatched in
        insertion order. Unregister via `unregister_order_error_handler`
        when the order reaches a terminal state to avoid leaks."""
        self._order_error_handlers.setdefault(order_id, []).append(handler)

    def unregister_order_error_handler(
        self, order_id: int, handler: OrderErrorHandler | None = None
    ) -> None:
        """Remove `handler` (or all handlers if None) for `order_id`."""
        if handler is None:
            self._order_error_handlers.pop(order_id, None)
            return
        lst = self._order_error_handlers.get(order_id)
        if not lst:
            return
        try:
            lst.remove(handler)
        except ValueError:
            pass
        if not lst:
            self._order_error_handlers.pop(order_id, None)

    # --- lifecycle ---

    @property
    def ib(self) -> IB:
        return self._ib

    @property
    def account(self) -> str | None:
        return self._account

    @property
    def is_connected(self) -> bool:
        return self._connected and self._ib.isConnected()

    async def connect(self) -> str:
        """Connect to TWS, validate paper-only invariant, set market data type.
        Returns the account code. Idempotent."""
        async with self._connect_lock:
            if self.is_connected and self._account is not None:
                return self._account

            s = self._settings
            try:
                await self._ib.connectAsync(
                    s.ibkr_host, s.ibkr_port, clientId=s.ibkr_client_id, timeout=10
                )
            except Exception as e:
                raise IBKRConnectionError(
                    f"could not connect to TWS at {s.ibkr_host}:{s.ibkr_port} "
                    f"as client {s.ibkr_client_id}: {type(e).__name__}: {e}"
                ) from e

            accounts = self._ib.managedAccounts()
            if not accounts:
                self._ib.disconnect()
                raise IBKRConnectionError("no managed accounts returned by TWS")

            account = self._select_account(accounts, s)

            if s.paper_trading_only and not account.startswith("DU"):
                self._ib.disconnect()
                raise IBKRSafetyError(
                    f"selected account {account} is NOT a paper account "
                    f"(expected DU* prefix), but PAPER_TRADING_ONLY=true. "
                    "Refusing to continue."
                )

            self._ib.reqMarketDataType(s.ibkr_market_data_type_code)
            self._connected = True
            self._account = account
            self._bind_error_event()
            logger.info(
                "IBKR connected: account=%s mdt=%s client_id=%s",
                account,
                s.ibkr_market_data_type,
                s.ibkr_client_id,
            )
            return account

    def _select_account(self, accounts: list[str], s: Settings) -> str:
        """Pick the right account out of the list IBKR returned.

        Rules:
          - If IBKR_TARGET_ACCOUNT is set, it MUST be present in `accounts`.
            We never silently fall back to "the only one available" when the
            user has expressed a preference, because that's how live trades
            end up in the wrong account.
          - If it's unset, exactly one account is required. Multiple accounts
            without an explicit target are an error - we refuse to guess.
        """
        target = s.ibkr_target_account.strip()
        if target:
            if target not in accounts:
                self._ib.disconnect()
                raise IBKRSafetyError(
                    f"IBKR_TARGET_ACCOUNT={target!r} not found in managed accounts "
                    f"{accounts}. Check the account code or remove the env var."
                )
            return target
        if len(accounts) == 1:
            return accounts[0]
        self._ib.disconnect()
        raise IBKRSafetyError(
            f"multiple managed accounts returned ({accounts}) and IBKR_TARGET_ACCOUNT "
            "is not set. Set IBKR_TARGET_ACCOUNT to the account code you want trades "
            "routed to (e.g. the trust account)."
        )

    async def disconnect(self) -> None:
        if self._ib.isConnected():
            self._ib.disconnect()
        self._connected = False
        self._account = None

    # --- contracts ---

    async def qualify(self, contract: Contract) -> Contract:
        result = await self._ib.qualifyContractsAsync(contract)
        if not result:
            raise IBKRConnectionError(f"could not qualify contract {contract!r}")
        return result[0]

    # --- market data ---

    def subscribe_realtime_bars(
        self,
        contract: Contract,
        what_to_show: str,
        on_bar: Callable[[RealTimeBarList, bool], None],
        use_rth: bool = False,
    ) -> RealTimeBarList:
        """Start a 5-second real-time bar subscription. The provided callback
        is invoked by ib_async whenever a new bar arrives. Caller is
        responsible for calling `cancel_realtime_bars` on shutdown."""
        rt = self._ib.reqRealTimeBars(contract, 5, what_to_show, useRTH=use_rth)
        rt.updateEvent += on_bar
        return rt

    def cancel_realtime_bars(self, rt: RealTimeBarList) -> None:
        try:
            self._ib.cancelRealTimeBars(rt)
        except Exception:
            logger.exception("error cancelling real-time bars")

    async def fetch_historical_1m_bars(
        self,
        contract: Contract,
        what_to_show: str,
        duration_str: str,
        use_rth: bool = False,
    ) -> list:
        """Pull recent historical 1-minute bars for indicator warm-up at
        engine start. Returns ib_async BarData objects (which expose `.date`,
        `.open`, `.high`, `.low`, `.close`, `.volume`). Caller is responsible
        for converting to the engine's internal Bar type.

        `duration_str` is the IBKR-format lookback window. Examples:
            "5400 S"  -> 90 minutes (1m MACD warm-up only)
            "4 H"     -> 4 hours
            "1 D"     -> 1 trading day
            "2 D"     -> 2 trading days (recommended for 5m MACD warm-up:
                         IBKR fills in prior-day's session for hot-start
                         on fresh movers, mirroring TradingView's MACD
                         carry-through behaviour across session boundaries)

        For 1m barSizeSetting, IBKR caps the "S" form at ~2000 bars (~33h);
        prefer "D" or longer for multi-session warm-up.
        """
        return await self._ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",
            durationStr=duration_str,
            barSizeSetting="1 min",
            whatToShow=what_to_show,
            useRTH=use_rth,
            formatDate=1,
        )

    # --- L2 / T&S ---
    #
    # We expose `subscribe_depth` and `subscribe_tape` that take the per-symbol
    # MarketState the engine owns. The IBKR Ticker emits updateEvents which
    # we translate into MarketState mutations (and into a caller-supplied
    # async tick callback the engine uses to publish features to the broker).
    #
    # Subscriptions are reference-counted by symbol: the same symbol on two
    # engine starts shares one IBKR-side subscription. The engine cancels on
    # stop. If multiple engines ever exist we'll need true refcounting; for
    # the POC the singleton runner enforces "one engine at a time" so this
    # is sufficient.

    def subscribe_depth(
        self,
        contract: Contract,
        market_state: MarketState,
        *,
        num_rows: int = 10,
        is_smart_depth: bool = True,
        on_update: Callable[[], None] | None = None,
    ) -> Ticker:
        """Start a Level-2 depth subscription. Updates `market_state.depth`
        in place. The optional `on_update` callback is invoked sync from the
        ib_async event loop after each update; use to push features to a
        broker etc.

        Raises IBKRSubscriptionError if the contract returns error 309/2150
        ("no depth entitlements") via the ib.error event - that's caught
        and surfaced to the caller so the engine can fall back to no-L2 mode.
        """
        ticker = self._ib.reqMktDepth(contract, numRows=num_rows, isSmartDepth=is_smart_depth)
        market_state.has_depth_subscription = True

        def _on_update(t: Ticker) -> None:
            try:
                _update_depth_book(market_state, t)
                if on_update is not None:
                    on_update()
            except Exception:
                logger.exception("depth update handler raised")

        ticker.updateEvent += _on_update
        return ticker

    def cancel_depth(self, ticker: Ticker, market_state: MarketState) -> None:
        try:
            self._ib.cancelMktDepth(ticker.contract)
        except Exception:
            logger.exception("error cancelling depth subscription")
        market_state.has_depth_subscription = False

    def subscribe_tape(
        self,
        contract: Contract,
        market_state: MarketState,
        *,
        tick_type: Literal["AllLast", "BidAsk", "Last", "MidPoint"] = "AllLast",
        ignore_size: bool = False,
        on_update: Callable[[], None] | None = None,
    ) -> Ticker:
        """Start a tick-by-tick T&S subscription. Updates `market_state.tape`
        in place as ticks arrive.

        - "AllLast" gives all prints including dark / out-of-sequence.
        - "BidAsk" gives NBBO changes only.

        For Ross-style tape reading we want "AllLast". For depth-only modes
        we don't subscribe to tape at all.
        """
        ticker = self._ib.reqTickByTickData(
            contract, tickType=tick_type, numberOfTicks=0, ignoreSize=ignore_size
        )
        market_state.has_tape_subscription = True
        ref_book = market_state.depth

        def _on_update(t: Ticker) -> None:
            try:
                _update_tape(market_state, t, ref_book)
                if on_update is not None:
                    on_update()
            except Exception:
                logger.exception("tape update handler raised")

        ticker.updateEvent += _on_update
        return ticker

    def cancel_tape(self, ticker: Ticker, market_state: MarketState) -> None:
        try:
            self._ib.cancelTickByTickData(ticker.contract, "AllLast")
        except Exception:
            logger.exception("error cancelling tape subscription")
        market_state.has_tape_subscription = False

    # --- quotes (for LMT pricing) ---

    def subscribe_quote(self, contract: Contract) -> Ticker:
        """Subscribe to a streaming NBBO ticker. Used by the executor to read
        the current ask/bid before submitting a marketable-LMT order."""
        ticker = self._ib.reqMktData(contract, "", False, False)
        return ticker

    def cancel_quote(self, ticker: Ticker) -> None:
        try:
            self._ib.cancelMktData(ticker.contract)
        except Exception:
            logger.exception("error cancelling quote subscription")

    # --- orders ---

    def place_market_order(
        self,
        contract: Contract,
        side: str,
        quantity: int,
        account: str | None = None,
    ) -> Trade:
        """Submit a market order and return the Trade handle. The caller
        attaches handlers to trade.fillEvent / trade.statusEvent."""
        side_norm = side.upper()
        if side_norm not in ("BUY", "SELL"):
            raise ValueError(f"side must be BUY or SELL, got {side!r}")
        order = MarketOrder(side_norm, quantity)
        order.tif = "DAY"
        if account:
            order.account = account
        trade = self._ib.placeOrder(contract, order)
        return trade

    def place_limit_order(
        self,
        contract: Contract,
        side: str,
        quantity: int,
        limit_price: float,
        *,
        tif: str = "DAY",
        outside_rth: bool = True,
        account: str | None = None,
    ) -> Trade:
        """Submit a limit order. Used by the executor for marketable-limit
        entries (LMT @ ask + offset for BUY)."""
        side_norm = side.upper()
        if side_norm not in ("BUY", "SELL"):
            raise ValueError(f"side must be BUY or SELL, got {side!r}")
        if limit_price <= 0:
            raise ValueError(f"limit_price must be positive, got {limit_price}")
        # Round limit price to 4 decimal places for forex, 2 for stocks. We
        # don't know which here, so use 4 (IBKR accepts 4-dp on stocks too,
        # it just rounds to tick size on the way in).
        order = LimitOrder(side_norm, quantity, round(limit_price, 4))
        order.tif = tif
        order.outsideRth = outside_rth
        if account:
            order.account = account
        trade = self._ib.placeOrder(contract, order)
        return trade

    def cancel_order(self, trade: Trade) -> None:
        try:
            self._ib.cancelOrder(trade.order)
        except Exception:
            logger.exception("error cancelling order id=%s", trade.order.orderId)


# --- helpers ---


def _update_depth_book(state: MarketState, ticker: Ticker) -> None:
    """Translate ib_async's domBids / domAsks (lists of DOMLevel) into our
    DepthBook representation. Each list is already sorted by the IBKR side."""
    bids: list[DepthLevel] = []
    for lvl in ticker.domBids or []:
        bids.append(
            DepthLevel(
                side="bid",
                price=float(lvl.price),
                size=float(lvl.size),
                market_maker=getattr(lvl, "marketMaker", None) or None,
            )
        )
    asks: list[DepthLevel] = []
    for lvl in ticker.domAsks or []:
        asks.append(
            DepthLevel(
                side="ask",
                price=float(lvl.price),
                size=float(lvl.size),
                market_maker=getattr(lvl, "marketMaker", None) or None,
            )
        )
    state.depth.bids = sorted(bids, key=lambda x: -x.price)
    state.depth.asks = sorted(asks, key=lambda x: x.price)
    state.depth.updated_at = dt.datetime.now(dt.timezone.utc)


def _classify_print(price: float, book) -> Literal["buy", "sell", "unknown"]:
    """Classify a print as buyer- or seller-aggressed.

    Uses the simple tick-rule against the current NBBO from `book`:
      - print >= ask     -> buy
      - print <= bid     -> sell
      - in-between / no book -> unknown

    Reasonable default; we can swap in Lee-Ready or BVC later.
    """
    if book is None:
        return "unknown"
    bb = book.best_bid
    ba = book.best_ask
    if ba is not None and price >= ba.price:
        return "buy"
    if bb is not None and price <= bb.price:
        return "sell"
    return "unknown"


def _update_tape(state: MarketState, ticker: Ticker, book) -> None:
    """Translate ib_async's tickByTicks deque into our TapeWindow ticks.

    We only consume the LATEST tick on each update event (the deque accumulates
    if we don't read it, but that's not relevant here - we want one push per
    update event).
    """
    ticks = getattr(ticker, "tickByTicks", None)
    if not ticks:
        return
    raw = ticks[-1]
    raw_type = type(raw).__name__
    ts = getattr(raw, "time", None) or dt.datetime.now(dt.timezone.utc)
    if isinstance(ts, dt.datetime) and ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    if "Last" in raw_type:
        price = float(getattr(raw, "price", 0) or 0)
        size = float(getattr(raw, "size", 0) or 0)
        if price <= 0 or size <= 0:
            return
        side = _classify_print(price, book)
        state.tape.push(TapeTick(ts=ts, price=price, size=size, side=side, raw_type=raw_type))
    elif "BidAsk" in raw_type:
        # NBBO updates aren't trades; we don't push them to the tape window,
        # but we DO use them to refresh the book's best bid/ask snapshot when
        # depth isn't subscribed.
        bid = float(getattr(raw, "bidPrice", 0) or 0)
        ask = float(getattr(raw, "askPrice", 0) or 0)
        bid_size = float(getattr(raw, "bidSize", 0) or 0)
        ask_size = float(getattr(raw, "askSize", 0) or 0)
        if bid > 0 and ask > 0 and not state.has_depth_subscription:
            state.depth.bids = [DepthLevel(side="bid", price=bid, size=bid_size)]
            state.depth.asks = [DepthLevel(side="ask", price=ask, size=ask_size)]
            state.depth.updated_at = dt.datetime.now(dt.timezone.utc)


_CLIENT: IBKRClient | None = None


def get_ibkr_client() -> IBKRClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = IBKRClient()
    return _CLIENT
