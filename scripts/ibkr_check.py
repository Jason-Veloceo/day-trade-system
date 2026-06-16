"""IBKR TWS connectivity + market data sanity check.

Run this BEFORE running the trading engine. Confirms that:
  - TWS API is reachable on the configured port
  - The connected account is a paper account (DU* prefix)
  - Market data subscription returns real-time data (not delayed)
  - 1-minute historical bars come back for SPY
  - 5-second real-time bars stream in for SPY

One-time TWS configuration (already done per screenshot):
    File -> Global Configuration -> API -> Settings
        [x] Enable ActiveX and Socket Clients
        [ ] Read-Only API
        Socket port: 7497
        Master API client ID: 101    (TWS-side; our client_id must be different)
        [x] Allow connections from localhost only
        Trusted IPs: 127.0.0.1

Run:
    cd backend && uv run python ../scripts/ibkr_check.py
    cd backend && uv run python ../scripts/ibkr_check.py UPC SOFI   # probe extra symbols
    cd backend && uv run python ../scripts/ibkr_check.py UPC --only # ONLY probe given symbols

Extra symbols are treated as US equities on SMART routing with USD currency.
For forex use "XXX.YYY" (e.g. EUR.USD) and we'll route to IDEALPRO.
"""

from __future__ import annotations

import asyncio
import math
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))

from ib_async import IB, Forex, Stock  # noqa: E402

from day_trade.config import get_settings  # noqa: E402

# Market data type codes per IB API.
MDT_LIVE = 1
MDT_FROZEN = 2
MDT_DELAYED = 3
MDT_DELAYED_FROZEN = 4
MDT_LABEL = {
    MDT_LIVE: "LIVE (real-time)",
    MDT_FROZEN: "FROZEN (last live snapshot)",
    MDT_DELAYED: "DELAYED (~15 min)",
    MDT_DELAYED_FROZEN: "DELAYED-FROZEN",
}


def fail(msg: str, code: int = 2) -> None:
    print(f"\nFAIL: {msg}")
    sys.exit(code)


async def main() -> None:
    s = get_settings()
    print("=== IBKR settings ===")
    print(f"  host                     : {s.ibkr_host}")
    print(f"  port                     : {s.ibkr_port}")
    print(f"  client_id                : {s.ibkr_client_id}")
    print(f"  trading_mode             : {s.ibkr_trading_mode}")
    print(f"  market_data_type         : {s.ibkr_market_data_type} (code {s.ibkr_market_data_type_code})")
    print(f"  paper_trading_only       : {s.paper_trading_only}")
    print(f"  live_trading_enabled     : {s.live_trading_enabled}")
    print(f"  manual_approval_required : {s.manual_approval_required}")

    if s.ibkr_client_id == 101:
        print()
        print("WARN: IBKR_CLIENT_ID=101 matches the TWS Master API client ID. Expect")
        print("      order-echo bugs and orphan order reconciliation issues. Use a")
        print("      different client_id (e.g. 37) for the trading client.")

    ib = IB()
    print(f"\n=== Connecting to {s.ibkr_host}:{s.ibkr_port} as client {s.ibkr_client_id} ===")
    try:
        await ib.connectAsync(s.ibkr_host, s.ibkr_port, clientId=s.ibkr_client_id, timeout=10)
    except Exception as e:
        fail(
            f"could not connect to TWS: {type(e).__name__}: {e}\n"
            "  - Is TWS running and logged into the PAPER account?\n"
            "  - Is API enabled in Global Configuration -> API -> Settings?\n"
            "  - Is the socket port 7497 (paper) and 127.0.0.1 in Trusted IPs?\n"
            "  - Is another client (e.g. previous run) already connected on this client_id?"
        )
        return
    print("  connected.")

    try:
        accounts = ib.managedAccounts()
        print(f"\n=== Managed accounts ===\n  {accounts}")
        if not accounts:
            fail("no managed accounts returned by TWS")
        primary = accounts[0]

        if s.paper_trading_only and not primary.startswith("DU"):
            fail(
                f"connected account {primary} is NOT a paper account (expected DU* prefix). "
                f"PAPER_TRADING_ONLY={s.paper_trading_only} is set, refusing to continue."
            )
        if not primary.startswith("DU"):
            print(f"\nWARN: account {primary} is LIVE. Refusing to do anything order-related.")

        print(f"\n=== Account summary ({primary}) ===")
        # NOTE: must use accountSummaryAsync inside an async function; the sync
        # accountSummary() wrapper internally calls loop.run_until_complete and
        # crashes with "This event loop is already running" when invoked from
        # asyncio.run(main()).
        summary = await ib.accountSummaryAsync(primary)
        wanted = {
            "NetLiquidation",
            "BuyingPower",
            "TotalCashValue",
            "AvailableFunds",
            "RealizedPnL",
            "UnrealizedPnL",
        }
        for row in summary:
            if row.tag in wanted:
                print(f"  {row.tag:18s} {row.value:>20s} {row.currency or ''}")

        ib.reqMarketDataType(s.ibkr_market_data_type_code)
        results: dict[str, dict] = {}

        # CLI args: positional symbols to probe in addition to (or instead of)
        # the defaults. --only restricts to user-supplied symbols.
        argv = [a for a in sys.argv[1:] if a]
        only = "--only" in argv
        user_symbols = [a for a in argv if not a.startswith("-")]

        defaults = [
            ("SPY (US equity ETF, AMEX-listed)", Stock("SPY", "SMART", "USD"), "TRADES"),
            ("EUR.USD (forex, IDEALPRO, ~24h Sun-Fri)", Forex("EURUSD"), "MIDPOINT"),
        ]
        probes = [] if only else list(defaults)
        for sym in user_symbols:
            s_norm = sym.strip().upper()
            if "." in s_norm:
                parts = s_norm.split(".")
                if len(parts) == 2 and all(len(p) == 3 for p in parts):
                    probes.append((
                        f"{s_norm} (forex, IDEALPRO)",
                        Forex(parts[0] + parts[1]),
                        "MIDPOINT",
                    ))
                else:
                    print(f"WARN: '{sym}' looks like forex but isn't XXX.YYY; skipping")
            else:
                # US equity via SMART. Pinning primaryExchange='NASDAQ' helps
                # IBKR disambiguate tickers that exist on multiple exchanges
                # (UPC and similar). If the actual primary is e.g. NYSE,
                # qualifyContractsAsync will rewrite it for us.
                contract = Stock(s_norm, "SMART", "USD", primaryExchange="NASDAQ")
                probes.append((f"{s_norm} (US equity, SMART/NASDAQ primary)", contract, "TRADES"))

        for label, raw_contract, what_to_show in probes:
            print(f"\n=== Probe: {label} ===")
            qualified = await ib.qualifyContractsAsync(raw_contract)
            if not qualified:
                print("  qualify failed - skipping")
                results[label] = {"status": "qualify_failed"}
                continue
            contract = qualified[0]
            # Show what IBKR actually resolved the contract to (helps catch
            # ambiguous symbols routed to the wrong exchange).
            print(
                f"  qualified      : conId={contract.conId} "
                f"symbol={contract.symbol} "
                f"primaryExchange={getattr(contract, 'primaryExchange', '?')} "
                f"exchange={contract.exchange} "
                f"localSymbol={contract.localSymbol}"
            )

            ticker = ib.reqMktData(contract, "", False, False)
            mdt = 0
            for _ in range(20):
                await asyncio.sleep(0.5)
                if ticker.marketDataType:
                    mdt = ticker.marketDataType
                    break

            def _ok(v: object) -> bool:
                return v is not None and not (isinstance(v, float) and math.isnan(v))

            has_quote = _ok(ticker.bid) or _ok(ticker.ask) or _ok(ticker.last) or _ok(ticker.close)

            print(f"  marketDataType : {mdt} ({MDT_LABEL.get(mdt, 'unknown')})")
            print(f"  bid / ask      : {ticker.bid} / {ticker.ask}")
            print(f"  last           : {ticker.last}")
            print(f"  close          : {ticker.close}")
            print(f"  any quote data?: {'YES' if has_quote else 'NO (likely no data subscription)'}")
            ib.cancelMktData(contract)

            print(f"  -- 1m historical bars (last 30 min) --")
            try:
                bars = await ib.reqHistoricalDataAsync(
                    contract,
                    endDateTime="",
                    durationStr="1800 S",
                    barSizeSetting="1 min",
                    whatToShow=what_to_show,
                    useRTH=False,
                    formatDate=1,
                )
            except Exception as e:
                bars = []
                print(f"  historical bars failed: {type(e).__name__}: {e}")
            if not bars:
                print("    no bars returned")
            else:
                print(f"    {len(bars)} bars received. Last 3:")
                for b in bars[-3:]:
                    print(
                        f"      {b.date}  O={b.open}  H={b.high}  L={b.low}  C={b.close}  V={b.volume}"
                    )

            print(f"  -- 5s real-time bars (10s window) --")
            seen: list = []

            def _on_bar(rt_bars, has_new_bar):
                if has_new_bar and rt_bars:
                    b = rt_bars[-1]
                    seen.append(b)
                    print(
                        f"      {b.time}  O={b.open}  H={b.high}  L={b.low}  C={b.close}  V={b.volume}"
                    )

            try:
                rt = ib.reqRealTimeBars(contract, 5, what_to_show, useRTH=False)
                rt.updateEvent += _on_bar
                await asyncio.sleep(10)
                ib.cancelRealTimeBars(rt)
            except Exception as e:
                print(f"  real-time bars failed: {type(e).__name__}: {e}")
            print(f"    received {len(seen)} bar(s) in 10s")

            results[label] = {
                "marketDataType": mdt,
                "has_quote": has_quote,
                "historical_bars": len(bars),
                "realtime_bars_in_10s": len(seen),
            }

    finally:
        if ib.isConnected():
            ib.disconnect()

    print("\n=== Disconnected. Summary ===")
    print(f"{'probe':50s} {'mdt':4s} {'quote?':6s} {'hist':5s} {'rt/10s':6s}")
    any_streamable = False
    for label, r in results.items():
        if r.get("status") == "qualify_failed":
            print(f"  {label:48s} qualify_failed")
            continue
        mdt = r["marketDataType"]
        print(
            f"  {label:48s} {mdt:>4d} {('YES' if r['has_quote'] else 'NO'):>6s} "
            f"{r['historical_bars']:>5d} {r['realtime_bars_in_10s']:>6d}"
        )
        if r["has_quote"] or r["realtime_bars_in_10s"] > 0:
            any_streamable = True

    print()
    if any_streamable:
        print("RESULT: GREEN. At least one instrument is streaming. POC can proceed using")
        print("        the working instrument. Subscribe to US equities later if you want")
        print("        to trade stocks; forex / futures may be enough for engine validation.")
    else:
        print("RESULT: AMBER. Connected but no real-time data streamed for any probe. Either")
        print("        markets are closed (re-run during RTH or Sun 5pm ET forex open) or")
        print("        no market data subscriptions are active. POC paper execution can")
        print("        still be tested using historical bars only - no live signals.")


if __name__ == "__main__":
    asyncio.run(main())
