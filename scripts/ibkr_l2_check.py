"""IBKR L2 (market depth) + T&S (tick-by-tick) API smoke test.

Confirms that the **API channel** can actually stream Level 2 order book
and Time & Sales data, separate from the L1 quotes + bars exercised by
`ibkr_check.py`. These two API surfaces are the foundation our engine's
`engine/orderbook.py` + `engine/features.py` were scaffolded for; the
Bookmap-style derived features (resting walls, aggressor imbalance,
absorption, sweeps) consume them downstream.

Distinction we are validating here:
  - `reqMktDepth()`  -> Level 2 order book (10 levels each side via
                       NASDAQ TotalView for NASDAQ tickers).
  - `reqTickByTickData(contract, "AllLast")`
                     -> every trade print (price, size, exchange,
                        conditions) -- the "tape" Ross watches.
  - `reqTickByTickData(contract, "BidAsk")`
                     -> every NBBO change. Cheaper than full L2 for
                        certain pressure features.

Run:
    cd backend && uv run python ../scripts/ibkr_l2_check.py FRTT
    cd backend && uv run python ../scripts/ibkr_l2_check.py SKYQ 30   # 30s listen window

Args:
    symbol   - US equity ticker (routes via SMART, primaryExchange=NASDAQ)
    seconds  - listen duration in seconds (default 20)

Constraints:
  - TWS limits ~3 concurrent reqMktDepth subscriptions per client. If
    TWS GUI already has a depth window open on the same symbol, this
    script may collide; close the GUI depth window first.
  - reqTickByTickData has a per-account rate limit (you can have ~5
    active streams at once). We use 2 streams here (AllLast + BidAsk),
    so we're well under.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))

from ib_async import IB, Stock  # noqa: E402

from day_trade.config import get_settings  # noqa: E402


def fail(msg: str, code: int = 2) -> None:
    print(f"\nFAIL: {msg}")
    sys.exit(code)


def _fmt_depth(side: str, dom: list, top_n: int = 5) -> list[str]:
    """Render the top N levels of one side of the book as text rows.

    ib-async's `DOMLevel` doesn't carry a `.position` field; the level
    index is implicit in the list (domBids[0] is best bid, [1] next, ...).
    """
    rows = []
    for i, lvl in enumerate(dom[:top_n]):
        mm = getattr(lvl, "marketMaker", "") or ""
        price = float(getattr(lvl, "price", 0.0))
        size = int(getattr(lvl, "size", 0))
        rows.append(
            f"    {side} L{i:>2d}  "
            f"price={price:>8.4f}  size={size:>6d}  mm={mm}"
        )
    return rows


async def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    symbol = sys.argv[1].upper()
    listen_seconds = float(sys.argv[2]) if len(sys.argv) >= 3 else 20.0

    s = get_settings()
    print(f"=== L2 + T&S API smoke test: {symbol} for {listen_seconds:.0f}s ===")
    print(f"  TWS {s.ibkr_host}:{s.ibkr_port} client={s.ibkr_client_id} mode={s.ibkr_trading_mode}")

    ib = IB()
    await ib.connectAsync(s.ibkr_host, s.ibkr_port, clientId=s.ibkr_client_id, timeout=10)
    print(f"  connected, accounts={ib.managedAccounts()}")

    counts = {"depth_updates": 0, "trades": 0, "bbo_updates": 0}
    last_trade_print: tuple[float, int, str] | None = None
    last_bbo: tuple[float, int, float, int] | None = None

    try:
        contract = Stock(symbol, "SMART", "USD", primaryExchange="NASDAQ")
        qualified = await ib.qualifyContractsAsync(contract)
        if not qualified:
            fail(f"could not qualify {symbol}")
        contract = qualified[0]
        print(
            f"  qualified: conId={contract.conId} primaryExchange={getattr(contract, 'primaryExchange', '?')} "
            f"localSymbol={contract.localSymbol} tradingClass={getattr(contract, 'tradingClass', '?')}"
        )

        ib.reqMarketDataType(s.ibkr_market_data_type_code)

        # --- reqMktDepth: 10 levels each side, NASDAQ TotalView for NASDAQ stocks
        # `isSmartDepth=True` aggregates across exchanges (recommended for SMART-routed
        # symbols); set False if you specifically want per-MM rows from a single
        # exchange. We start with True and check the output.
        print("\n=== Subscribing to reqMktDepth(numRows=10, isSmartDepth=True) ===")
        depth_ticker = ib.reqMktDepth(contract, numRows=10, isSmartDepth=True)

        def _on_depth_update(_t):
            counts["depth_updates"] += 1

        depth_ticker.updateEvent += _on_depth_update

        # --- reqTickByTickData: trade prints (every fill, every exchange)
        print("=== Subscribing to reqTickByTickData('AllLast') ===")
        trades_ticker = ib.reqTickByTickData(contract, "AllLast", numberOfTicks=0, ignoreSize=False)

        def _on_trades_update(_t):
            if not _t.tickByTicks:
                return
            counts["trades"] += 1
            tick = _t.tickByTicks[-1]
            nonlocal last_trade_print
            last_trade_print = (float(tick.price), int(tick.size), str(tick.exchange or ""))

        trades_ticker.updateEvent += _on_trades_update

        # --- reqTickByTickData: NBBO changes (every bid/ask update)
        print("=== Subscribing to reqTickByTickData('BidAsk') ===")
        bbo_ticker = ib.reqTickByTickData(contract, "BidAsk", numberOfTicks=0, ignoreSize=False)

        def _on_bbo_update(_t):
            if not _t.tickByTicks:
                return
            counts["bbo_updates"] += 1
            tick = _t.tickByTicks[-1]
            nonlocal last_bbo
            last_bbo = (
                float(tick.bidPrice),
                int(tick.bidSize),
                float(tick.askPrice),
                int(tick.askSize),
            )

        bbo_ticker.updateEvent += _on_bbo_update

        # Listen window: emit a status line every 2 seconds.
        print(f"\n=== Listening for {listen_seconds:.0f}s ===\n")
        deadline = asyncio.get_event_loop().time() + listen_seconds
        next_status_at = asyncio.get_event_loop().time()
        snapshot_at = asyncio.get_event_loop().time() + max(2.0, listen_seconds / 2)
        snapshot_taken = False
        prev_counts = {"depth_updates": 0, "trades": 0, "bbo_updates": 0}

        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.5)
            now = asyncio.get_event_loop().time()
            if now >= next_status_at:
                d_depth = counts["depth_updates"] - prev_counts["depth_updates"]
                d_trade = counts["trades"] - prev_counts["trades"]
                d_bbo = counts["bbo_updates"] - prev_counts["bbo_updates"]
                trade_str = (
                    f"last_trade={last_trade_print[0]:.4f}x{last_trade_print[1]}@{last_trade_print[2]}"
                    if last_trade_print
                    else "last_trade=-"
                )
                bbo_str = (
                    f"last_bbo={last_bbo[0]:.4f}x{last_bbo[1]}/{last_bbo[2]:.4f}x{last_bbo[3]}"
                    if last_bbo
                    else "last_bbo=-"
                )
                print(
                    f"  +2s  depth+={d_depth:>4d}  trades+={d_trade:>4d}  bbo+={d_bbo:>4d}   "
                    f"{trade_str}   {bbo_str}"
                )
                prev_counts = dict(counts)
                next_status_at = now + 2.0

            # Take a one-shot snapshot of the L2 ladder ~halfway through
            # the listen window. Use depth_ticker.domBids/domAsks which
            # ib-async maintains as the current state.
            if not snapshot_taken and now >= snapshot_at:
                snapshot_taken = True
                print("\n  -- L2 ladder snapshot (top 5 each side) --")
                bid_rows = _fmt_depth("BID", depth_ticker.domBids, top_n=5)
                ask_rows = _fmt_depth("ASK", depth_ticker.domAsks, top_n=5)
                for row in bid_rows:
                    print(row)
                for row in ask_rows:
                    print(row)
                if not bid_rows and not ask_rows:
                    print("    (book empty -- L2 not flowing?)")
                print()

        # Clean up subscriptions before disconnect.
        ib.cancelMktDepth(contract, isSmartDepth=True)
        ib.cancelTickByTickData(contract, "AllLast")
        ib.cancelTickByTickData(contract, "BidAsk")

    finally:
        if ib.isConnected():
            ib.disconnect()

    print("\n=== Summary ===")
    print(f"  depth_updates : {counts['depth_updates']}")
    print(f"  trades (T&S)  : {counts['trades']}")
    print(f"  bbo updates   : {counts['bbo_updates']}")
    print()

    l2_ok = counts["depth_updates"] > 0
    ts_ok = counts["trades"] > 0
    bbo_ok = counts["bbo_updates"] > 0

    print(f"  L2 (reqMktDepth)              : {'WORKING' if l2_ok else 'NOT WORKING'}")
    print(f"  T&S (reqTickByTickData AllLast): {'WORKING' if ts_ok else 'NOT WORKING'}")
    print(f"  BBO (reqTickByTickData BidAsk) : {'WORKING' if bbo_ok else 'NOT WORKING'}")
    print()

    if l2_ok and ts_ok and bbo_ok:
        print("RESULT: GREEN. Full L2 + T&S API stream confirmed. The engine's")
        print("        orderbook.py + features.py layer can now be exercised end-to-end.")
    elif (l2_ok or bbo_ok) and not ts_ok:
        print("RESULT: AMBER. L2/BBO flow but no trades printed in the window. Either")
        print("        the symbol is currently halted / illiquid, or AllLast permissions")
        print("        are missing. Retry on a more active symbol or during a print burst.")
    elif ts_ok and not l2_ok:
        print("RESULT: AMBER. Trades flow but L2 ladder is empty. Likely cause: TWS")
        print("        depth subscription cap or a stale GUI depth window holding the")
        print("        same symbol. Close TWS depth window for this symbol and re-run.")
    else:
        print("RESULT: RED. Neither L2 nor T&S flowed. Either entitlements have not")
        print("        propagated to the API channel, or the symbol has no activity in")
        print("        this window. Re-run during RTH on an active mover (e.g. FRTT).")


if __name__ == "__main__":
    asyncio.run(main())
