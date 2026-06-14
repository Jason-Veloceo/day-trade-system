# L2 / T&S data feed: moomoo OpenAPI vs IBKR NASDAQ TotalView

## Question

You asked whether moomoo's L2 / T&S could be used to feed the engine instead
of (or alongside) buying IBKR's NASDAQ TotalView subscription.

## Short answer

**moomoo OpenAPI is real, well-documented, and offers free L2 (NASDAQ
TotalView + NYSE ArcaBook) for US equities** via a local gateway daemon
(`OpenD`) and a Python SDK (`moomoo-api`). It is a viable alternative.

But adopting it requires meaningful integration work and introduces new
operational complexity. My recommendation: **stay IBKR-only for v1.1**;
revisit moomoo only after we've proven the gate stack works on a US small-cap
with real data.

## Capability comparison

| Capability | IBKR (`ib-async`) | moomoo (`moomoo-api`) |
|------------|-------------------|------------------------|
| Order execution (paper + live) | ✅ this is what the engine uses | ⚠ moomoo supports trading, but mixing brokers would mean orders go to one and data comes from another |
| Real-time 1m / 5s bars | ✅ (already wired) | ✅ |
| Level 2 depth (NASDAQ) | ✅ via NASDAQ TotalView (~USD $15.50/mo non-pro) | ✅ **free for US** per moomoo docs |
| Level 2 depth (NYSE/ARCA) | ✅ via NYSE OpenBook (~USD $15/mo non-pro) | ✅ **free for US** per moomoo docs |
| T&S (tick-by-tick) | ✅ via `reqTickByTickData("AllLast")` | ✅ via `subscribe(SubType.RT_DATA / TICKER)` |
| Async SDK | ✅ `ib-async` is asyncio-native | ⚠ `moomoo-api` is sync; would need a worker thread or `asyncio.to_thread` wrapper |
| Auth model | Local TWS / IB Gateway login | Local OpenD daemon + API key (separate enrolment) |
| ToS | Reading data we've subscribed to | Need to confirm OpenAPI use is allowed by Moomoo AU; Moomoo's TOS varies by region |

## What "free L2 on moomoo" actually means

From moomoo's OpenAPI docs (verified June 2026):

> US stock depth data (Nasdaq TotalView and NYSE ArcaBook) is currently
> available for free.

This is a real economic difference. NASDAQ TotalView alone is USD $15.50/mo
on IBKR for a non-pro account; the same data is free on moomoo, provided
you have a moomoo brokerage account in a supported region (AU / US / SG / HK).

## Integration cost honestly

If we go moomoo-only for data (IBKR-only for execution):

1. Install OpenD on the user's Mac (separate daemon, separate auth)
2. Add `moomoo-api` Python dependency
3. Wrap the sync SDK in an async layer (`asyncio.to_thread` per call, or a
   dedicated worker thread that pushes events onto an asyncio queue)
4. Build a translation layer from moomoo's symbol format
   (`US.SPY`, `HK.00700`) to IBKR's (`SPY` on SMART) and back
5. Handle dual sessions — moomoo for data, IBKR for orders, with clock
   synchronisation so signal_price and fill_price are comparable
6. Account for moomoo's rate limits (per-symbol subscription caps)
7. Confirm moomoo AU TOS allows OpenAPI use for personal algo trading

This is roughly a 1–2 day workstream. Done carefully it works; done quickly
it produces subtle bugs (e.g. depth book from one feed and prints from
another out of sync).

## Recommendation

For Monday's smoke test:

- **Forex (EUR.USD)**: doesn't have stock-style L2. Run with
  `enable_depth=false, enable_tape=false`. Strategy uses 5m + 1m MACD + VWAP
  (if any volume) + the time-of-day backside gate. Validates the full
  pipeline end-to-end without any subscription dependency.
- **US small-cap (your DTD pick)**: run with `enable_depth=true,
  enable_tape=true` on IBKR. If your current "Share Market Data with Paper
  Trading Account" entitlement does NOT include NASDAQ TotalView + NYSE
  Network A, the L2 gates and tape features will report `has_depth=false`
  and `has_tape=false`, but the engine still runs (degrades gracefully).

If after Monday you find:

- The engine works well end-to-end on simple gates, AND
- You consistently want fuller L2/T&S than what IBKR's freebies give

then we evaluate adding moomoo as the L2/T&S source. The engine is
already structured so that swap is localised to the `ibkr_client.py`
subscription functions — the `MarketState` and `compute_snapshot()`
abstractions don't care which feed populates them.

## What I'm explicitly NOT doing in v1.1

- Building the moomoo integration. The IBKR client supports both modes
  (depth on/off, tape on/off), so we can pivot to moomoo later without
  rewriting the strategy or features layer.
- Subscribing to NASDAQ TotalView on your behalf. You said "if this is the
  only source of T&S then yes, I will add that after you have validated."
  The answer to "is this the only source": no — but it's the simplest one.

## What I would recommend you buy if you want IBKR-only

- **NYSE Network A (Non-Pro)** — covers NYSE + ARCA + AMEX listed stocks.
  Required for SPY and a lot of small-caps. ~USD $1.50/mo.
- **NASDAQ TotalView** — full NASDAQ depth book. Required for NASDAQ-listed
  small-caps. ~USD $15.50/mo non-pro.
- (Already free) US Securities Snapshot Bundle gives non-streaming snapshots
  that aren't useful for the engine's live gates.
