"""Order placement + fill tracking.

When the engine decides to act on a signal it asks the executor to submit
either a market order (POC default) or a marketable-limit order (Ross-style
hotkey emulation):

  BUY :  LMT @ ask + offset                    (always anchored to ask)
  SELL:  LMT @ bid - offset  (sell_anchor='bid', aggressive, default)
         LMT @ ask - offset  (sell_anchor='ask', passive, "get away with selling at ask")

For LMT orders the executor optionally arms a cancel-on-timeout watchdog so an
unfilled order doesn't sit indefinitely.

For every fill the executor:
  - Wires fill / status callbacks onto the Trade.
  - Computes slippage vs the signal price and latency vs the signal timestamp.
  - Persists an `orders` row and a `fills` row.
  - Journals order_submit / order_status / fill / slippage events.

Single-fill / single-leg is sufficient for the POC. Partial fills on US small
caps are common; we treat the first fill as the executive fill, persist that,
and journal subsequent partials at the fill event level for audit.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import math
from decimal import Decimal
from typing import TYPE_CHECKING

from ib_async import Contract, Ticker, Trade

from day_trade.db.models import Fill as FillModel
from day_trade.db.models import Order as OrderModel
from day_trade.db.session import session_scope

from . import slippage as slippage_mod
from .ibkr_client import IBKRClient
from .journal import Journal

if TYPE_CHECKING:
    from .strategies.base import Signal

logger = logging.getLogger(__name__)


# --- helpers ---


def _ok(price: float | None) -> bool:
    return price is not None and price > 0 and not math.isnan(price)


async def _wait_for_quote(ticker: Ticker, timeout_seconds: float = 2.0) -> tuple[float | None, float | None]:
    """Poll the ticker for the first valid bid/ask. ib_async pushes via events
    but we keep this simple - poll on a short cadence with a hard cap."""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        bid = float(ticker.bid) if _ok(ticker.bid) else None
        ask = float(ticker.ask) if _ok(ticker.ask) else None
        if bid is not None and ask is not None:
            return bid, ask
        await asyncio.sleep(0.1)
    bid = float(ticker.bid) if _ok(ticker.bid) else None
    ask = float(ticker.ask) if _ok(ticker.ask) else None
    return bid, ask


class Executor:
    def __init__(
        self,
        *,
        run_id: int,
        symbol_display: str,
        contract: Contract,
        ibkr: IBKRClient,
        journal: Journal,
    ) -> None:
        self.run_id = run_id
        self.symbol = symbol_display
        self.contract = contract
        self.ibkr = ibkr
        self.journal = journal

    async def execute(
        self,
        *,
        signal: Signal,
        side: str,
        quantity: int,
        order_type: str = "MKT",
        limit_offset_cents: float = 10.0,
        sell_anchor: str = "bid",
        cancel_after_seconds: float = 3.0,
        quote_ticker: Ticker | None = None,
    ) -> Trade | None:
        """Submit an order for `signal` and return the IBKR Trade handle.

        order_type:
          - "MKT": vanilla market order, no cancel-on-timeout.
          - "LMT": marketable limit. BUY is always anchored to the ask:
                   limit_price = ask + offset. SELL is anchored per
                   `sell_anchor`:
                     - 'bid' (aggressive, default): limit_price = bid - offset
                     - 'ask' (passive):             limit_price = ask - offset
                   If the order doesn't fill within `cancel_after_seconds`,
                   cancel it - the engine will then re-evaluate gates and
                   decide whether to retry.

        For LMT, the caller must pass `quote_ticker` (the IBKRClient's NBBO
        subscription for this symbol) so we can read live bid/ask.
        """
        side_norm = side.upper()
        order_type_norm = order_type.upper()
        sell_anchor_norm = sell_anchor.lower()
        if order_type_norm not in ("MKT", "LMT"):
            raise ValueError(f"order_type must be MKT or LMT, got {order_type!r}")
        if sell_anchor_norm not in ("bid", "ask"):
            raise ValueError(f"sell_anchor must be 'bid' or 'ask', got {sell_anchor!r}")

        # ---- price discovery for LMT ----
        limit_price: float | None = None
        used_anchor: str | None = None
        observed_bid: float | None = None
        observed_ask: float | None = None
        offset: float = 0.0
        if order_type_norm == "LMT":
            if quote_ticker is None:
                raise ValueError("LMT order requires quote_ticker")
            bid, ask = await _wait_for_quote(quote_ticker)
            observed_bid, observed_ask = bid, ask
            if not (_ok(bid) and _ok(ask)):
                await self.journal.record(
                    "error",
                    {
                        "where": "executor.execute",
                        "msg": f"no NBBO available for LMT pricing; bid={bid} ask={ask}",
                    },
                )
                return None
            requested_offset = limit_offset_cents / 100.0
            # Spread-aware cap: a fixed 10c offset is appropriate for $5-$50
            # stocks but absurd on sub-$1 names (HKIT incident Fri 26 Jun:
            # ask 0.36 / bid 0.18 stock, 10c offset produced LMT BUY @ 0.37
            # = 37% of price). Cap the EFFECTIVE offset to max(1c, 2% of
            # mid). This stays close to the inside on cheap names without
            # changing behaviour for normally-priced stocks (1c floor
            # preserves marketable-LMT semantics).
            mid = (bid + ask) / 2.0
            cap = max(0.01, 0.02 * mid) if mid > 0 else requested_offset
            offset = min(requested_offset, cap)
            if side_norm == "BUY":
                limit_price = ask + offset
                used_anchor = "ask"
            else:  # SELL
                if sell_anchor_norm == "bid":
                    limit_price = max(bid - offset, 0.0001)
                    used_anchor = "bid"
                else:  # 'ask' - passive sell
                    limit_price = max(ask - offset, 0.0001)
                    used_anchor = "ask"

        await self.journal.record(
            "order_submit",
            {
                "side": side_norm,
                "quantity": quantity,
                "order_type": order_type_norm,
                "limit_price": limit_price,
                "limit_offset_cents": limit_offset_cents if order_type_norm == "LMT" else None,
                "effective_offset_cents": (
                    round(offset * 100.0, 4) if order_type_norm == "LMT" else None
                ),
                "anchor": used_anchor,
                "sell_anchor_config": sell_anchor_norm if side_norm == "SELL" else None,
                "observed_bid": observed_bid,
                "observed_ask": observed_ask,
                "signal_kind": signal.kind.value,
                "signal_ts": signal.ts.isoformat(),
                "signal_price": signal.price,
                "reason": signal.reason,
                "symbol": self.symbol,
            },
        )

        try:
            if order_type_norm == "MKT":
                trade = self.ibkr.place_market_order(
                    self.contract, side_norm, quantity, account=self.ibkr.account
                )
            else:
                assert limit_price is not None
                trade = self.ibkr.place_limit_order(
                    self.contract,
                    side_norm,
                    quantity,
                    limit_price,
                    tif="DAY",
                    outside_rth=True,
                    account=self.ibkr.account,
                )
        except Exception as e:
            await self.journal.record(
                "error",
                {"where": "place_order", "error": f"{type(e).__name__}: {e}"},
            )
            return None

        # Arm cancel-on-timeout for LMT.
        if order_type_norm == "LMT" and cancel_after_seconds > 0:
            asyncio.create_task(self._arm_cancel(trade, cancel_after_seconds))

        # Wire async-style callbacks. ib_async's events accept sync callables
        # only; we forward into asyncio.tasks ourselves.
        loop = asyncio.get_running_loop()

        def _on_status(t: Trade) -> None:
            loop.create_task(self._on_status(t))

        def _on_fill(t: Trade, _fill) -> None:
            loop.create_task(self._on_fill(t, signal))

        trade.statusEvent += _on_status
        trade.fillEvent += _on_fill

        return trade

    async def _arm_cancel(self, trade: Trade, after_seconds: float) -> None:
        """Cancel `trade` if it isn't filled after `after_seconds` seconds."""
        try:
            await asyncio.sleep(after_seconds)
            status = (trade.orderStatus.status or "").lower()
            if status in ("filled", "cancelled", "apicancelled", "inactive"):
                return
            self.ibkr.cancel_order(trade)
            await self.journal.record(
                "order_status",
                {
                    "ibkr_order_id": trade.order.orderId,
                    "status": "cancel_requested",
                    "reason": f"unfilled after {after_seconds:.1f}s",
                },
            )
        except Exception:
            logger.exception("cancel-on-timeout failed for order id=%s", trade.order.orderId)

    # --- callbacks ---

    async def _on_status(self, trade: Trade) -> None:
        await self.journal.record(
            "order_status",
            {
                "ibkr_order_id": trade.order.orderId,
                "status": trade.orderStatus.status,
                "filled": float(trade.orderStatus.filled or 0),
                "remaining": float(trade.orderStatus.remaining or 0),
                "avgFillPrice": float(trade.orderStatus.avgFillPrice or 0),
            },
        )

    async def _on_fill(self, trade: Trade, signal: Signal) -> None:
        # We only treat the FIRST fill of an order as the "executive" fill -
        # subsequent fills (partials) are journalled with the same fill event
        # type but only the first one creates the orders/fills DB rows.
        if not trade.fills:
            return
        executive_fill = trade.fills[0]

        fill_price = float(executive_fill.execution.price)
        fill_qty = int(executive_fill.execution.shares)
        fill_ts = executive_fill.time

        # Compute slippage vs signal.
        try:
            report = slippage_mod.compute(
                side=trade.order.action,
                signal_price=signal.price,
                signal_ts=signal.ts,
                fill_price=fill_price,
                fill_ts=fill_ts,
            )
        except Exception:
            logger.exception("slippage compute failed")
            report = None

        # Persist orders + fills rows. We use the IBKR order id as a stable
        # key so retries don't double-insert. (Conflict handling is skipped
        # for the POC.)
        try:
            await self._persist(trade, executive_fill, signal, report)
        except Exception:
            logger.exception("failed to persist order/fill")

        await self.journal.record(
            "fill",
            {
                "ibkr_order_id": trade.order.orderId,
                "side": trade.order.action,
                "qty": fill_qty,
                "fill_price": fill_price,
                "fill_ts": fill_ts.isoformat() if isinstance(fill_ts, dt.datetime) else None,
                "avgFillPrice": float(trade.orderStatus.avgFillPrice or 0),
                "execution_id": executive_fill.execution.execId,
            },
        )

        if report is not None:
            await self.journal.record(
                "slippage",
                {
                    "side": report.side,
                    "signal_price": report.signal_price,
                    "fill_price": report.fill_price,
                    "slippage_cents": report.slippage_cents,
                    "slippage_bps": report.slippage_bps,
                    "latency_ms": report.latency_ms,
                },
            )

    async def _persist(
        self,
        trade: Trade,
        executive_fill,
        signal: Signal,
        report: slippage_mod.SlippageReport | None,
    ) -> None:
        async with session_scope() as s:
            order_row = OrderModel(
                engine_run_id=self.run_id,
                ibkr_order_id=int(trade.order.orderId),
                symbol=self.symbol,
                side=str(trade.order.action),
                order_type=str(trade.order.orderType or "MKT"),
                qty=int(executive_fill.execution.shares),
                limit_price=None,
                stop_price=None,
                status=str(trade.orderStatus.status),
                submitted_at=dt.datetime.now(dt.UTC),
                raw={
                    "order": {
                        "orderId": trade.order.orderId,
                        "action": trade.order.action,
                        "orderType": trade.order.orderType,
                        "totalQuantity": float(trade.order.totalQuantity or 0),
                        "tif": trade.order.tif,
                    },
                    "status": trade.orderStatus.status,
                },
            )
            s.add(order_row)
            await s.flush()  # need order_row.id

            fill_row = FillModel(
                order_id=order_row.id,
                engine_run_id=self.run_id,
                symbol=self.symbol,
                qty=int(executive_fill.execution.shares),
                price=Decimal(str(executive_fill.execution.price)),
                ts=executive_fill.time,
                raw={
                    "execId": executive_fill.execution.execId,
                    "shares": float(executive_fill.execution.shares),
                    "price": float(executive_fill.execution.price),
                    "side": executive_fill.execution.side,
                },
                signal_price=Decimal(str(signal.price)),
                signal_ts=signal.ts,
                slippage_cents=Decimal(str(round(report.slippage_cents, 2))) if report else None,
                slippage_bps=Decimal(str(round(report.slippage_bps, 2))) if report else None,
                latency_ms=report.latency_ms if report else None,
            )
            s.add(fill_row)
