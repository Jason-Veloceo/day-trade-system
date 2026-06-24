"""One-off manual paper trade test.

Submits a small marketable LMT buy, waits a few seconds, then a marketable
LMT sell. Designed to validate that IBKR paper order submission + fills
work end-to-end on a US equity in extended hours.

This does NOT go through the engine. It's a direct IBKR script so we can
isolate "does paper trading work?" from "does the engine work?".

Usage:
    cd backend && uv run python ../scripts/manual_trade_test.py SKYQ 10

Args:
    symbol   - US equity ticker (routed via SMART, primaryExchange=NASDAQ)
    quantity - integer share count

Behaviour:
    - Connects, qualifies the contract
    - Pulls last 1m historical bar to anchor LMT prices (since we have no
      live quote on this paper account)
    - Submits a BUY LMT at last_close * 1.005 (50 bps above) outsideRth=True
    - Polls for up to 5 seconds for fill; cancels if unfilled
    - Sleeps 10 seconds
    - Submits a SELL LMT at last_close * 0.995 (50 bps below) outsideRth=True
    - Polls for up to 5 seconds for fill; cancels if unfilled
    - Prints every state transition
"""

from __future__ import annotations

import asyncio
import datetime as dt
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))

from ib_async import IB, LimitOrder, Stock  # noqa: E402

from day_trade.config import get_settings  # noqa: E402


def _tick_round(price: float, ref_price: float) -> float:
    """Round `price` to a tick the exchange will accept.

    US equity tick rules: stocks >= $1.00 trade in $0.01 increments;
    stocks < $1.00 trade in $0.0001 increments. We use the reference
    price (last close) to decide which regime we're in, since the
    submitted price may straddle the boundary.
    """
    if ref_price >= 1.00:
        return round(price, 2)
    return round(price, 4)


async def submit_and_wait(
    ib: IB,
    contract: Stock,
    side: str,
    qty: int,
    limit_price: float,
    wait_seconds: float,
) -> tuple[str, float | None]:
    """Submit a LMT order, wait up to `wait_seconds` for it to fill.

    Returns (status, avg_fill_price). status is one of 'Filled',
    'Cancelled', 'PartiallyFilled', or 'Submitted' (if timed out).
    """
    order = LimitOrder(side, qty, limit_price, outsideRth=True, tif="DAY")
    print(f"  -> submitting {side} {qty} @ LMT {limit_price} outsideRth=True tif=DAY")
    trade = ib.placeOrder(contract, order)

    deadline = asyncio.get_event_loop().time() + wait_seconds
    last_status = ""
    while asyncio.get_event_loop().time() < deadline:
        status = trade.orderStatus.status
        if status != last_status:
            print(
                f"     status={status:<16s} "
                f"filled={trade.orderStatus.filled} "
                f"remaining={trade.orderStatus.remaining} "
                f"avgFill={trade.orderStatus.avgFillPrice or 0:.4f}"
            )
            last_status = status
        if status in ("Filled", "Cancelled", "ApiCancelled", "Inactive"):
            break
        await asyncio.sleep(0.1)

    final_status = trade.orderStatus.status
    avg = trade.orderStatus.avgFillPrice if trade.orderStatus.filled else None

    if final_status not in ("Filled", "Cancelled", "ApiCancelled", "Inactive"):
        print(f"     timed out at status={final_status}; cancelling")
        ib.cancelOrder(order)
        await asyncio.sleep(0.5)
        final_status = trade.orderStatus.status
        print(f"     post-cancel status={final_status}")

    return final_status, avg


async def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    symbol = sys.argv[1].upper()
    qty = int(sys.argv[2])

    s = get_settings()
    print(f"=== Manual paper trade test: {qty} x {symbol} ===")
    print(f"  TWS {s.ibkr_host}:{s.ibkr_port} client={s.ibkr_client_id} mode={s.ibkr_trading_mode}")

    ib = IB()
    await ib.connectAsync(s.ibkr_host, s.ibkr_port, clientId=s.ibkr_client_id, timeout=10)
    print(f"  connected, accounts={ib.managedAccounts()}")

    try:
        contract = Stock(symbol, "SMART", "USD", primaryExchange="NASDAQ")
        qualified = await ib.qualifyContractsAsync(contract)
        if not qualified:
            print(f"FAIL: could not qualify {symbol}")
            sys.exit(2)
        contract = qualified[0]
        print(
            f"  qualified: conId={contract.conId} primaryExchange={getattr(contract, 'primaryExchange', '?')} "
            f"localSymbol={contract.localSymbol}"
        )

        # Anchor LMT prices off last 1m historical bar's close (we have no
        # live quote on this paper account due to subscription routing).
        bars = await ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",
            durationStr="1800 S",
            barSizeSetting="1 min",
            whatToShow="TRADES",
            useRTH=False,
            formatDate=1,
        )
        if not bars:
            print(f"FAIL: no historical bars for {symbol}; cannot anchor LMT price")
            sys.exit(3)
        last_close = float(bars[-1].close)
        print(
            f"  last historical 1m bar: ts={bars[-1].date} close={last_close:.4f} "
            f"volume={bars[-1].volume}"
        )

        # Aggressive offsets - we're trying to validate plumbing, not price
        # discovery. Pre-market spreads on small-caps can be 1-2% wide so a
        # marketable LMT needs real headroom. We're price-takers here.
        buy_lmt = _tick_round(last_close * 1.02, last_close)
        sell_lmt = _tick_round(last_close * 0.98, last_close)
        print(f"  buy  LMT = last_close * 1.02 (tick-rounded) = {buy_lmt}")
        print(f"  sell LMT = last_close * 0.98 (tick-rounded) = {sell_lmt}")

        print(f"\n--- BUY {qty} {symbol} ---")
        buy_status, buy_avg = await submit_and_wait(ib, contract, "BUY", qty, buy_lmt, 15.0)

        if buy_status != "Filled":
            print(f"\nBUY did not fill cleanly (final status={buy_status}). Aborting sell to avoid stuck position.")
            sys.exit(4)

        print(f"\n--- Holding for 10s (bought @ {buy_avg:.4f}) ---")
        await asyncio.sleep(10)

        print(f"\n--- SELL {qty} {symbol} ---")
        sell_status, sell_avg = await submit_and_wait(ib, contract, "SELL", qty, sell_lmt, 15.0)

        print("\n=== Trade Summary ===")
        print(f"  symbol      : {symbol}")
        print(f"  quantity    : {qty}")
        print(f"  buy  status : {buy_status:<10s} avg fill = {buy_avg if buy_avg is not None else 'n/a'}")
        print(f"  sell status : {sell_status:<10s} avg fill = {sell_avg if sell_avg is not None else 'n/a'}")
        if buy_avg and sell_avg:
            pnl_per_share = sell_avg - buy_avg
            pnl_total = pnl_per_share * qty
            print(f"  P&L/share   : {pnl_per_share:+.4f} USD")
            print(f"  P&L total   : {pnl_total:+.2f} USD")
        elapsed = dt.datetime.now()
        print(f"  finished at : {elapsed}")

    finally:
        ib.disconnect()
        print("  disconnected")


if __name__ == "__main__":
    asyncio.run(main())
