# Monday smoke test plan (v1.1 / v1.2 semi-auto engine)

When you sit down Monday and the markets are awake again, this is the
shortest path from "everything is built" to "I've seen the engine make a
paper trade end-to-end".

**Where you are right now (Sun 14 Jun 2026, Perth):**
- v1.1 engine: built, all 68 backend tests pass.
- v1.2 sell anchor + pullback-break trigger: built, migration applied
  (head = `4e1f0a82c9b1`).
- Knowledge base (`strategy_sources/principles.md`, `scenarios.yaml`):
  ingested but documentation-only — engine does NOT consume it yet.

**Timing reference (Perth UTC+8):**
- Forex IDEALPRO re-opens Sun ~05:00 Mon Perth (Sun 17:00 ET).
- US RTH opens Mon 21:30 Perth (09:30 ET, EDT-aware).
- US RTH closes Tue 04:00 Perth (16:00 ET).

## Phase 0 — pre-flight (Sunday night / Monday morning)

1. Restart TWS into the paper account `DUM733674`. Make sure no other
   `jasonpaizes` session is active (close browser Client Portal, mobile
   app, IB Gateway).
2. Accept the paper trading disclaimer if TWS prompts.
3. From the repo root:
   ```bash
   cd backend && uv run python ../scripts/ibkr_check.py
   ```
4. Expect:
   - clean connect, account `DUM733674`, balance fine
   - SPY: still `Error 420` if you haven't activated NYSE Network A —
     fine for now, Phase 1 runs on EUR.USD
   - EUR.USD: once forex is open, `marketDataType=1` and
     `realtime_bars_in_10s` ≥ 1
5. Confirm the DB is on the latest migration:
   ```bash
   cd backend && uv run alembic current
   ```
   Expect: `4e1f0a82c9b1 (head)` — that's the `sell_anchor` column.

## Phase 1 — forex smoke (Monday early morning Perth)

The point of this phase is to prove the *engine plumbing* works
end-to-end without depending on any equities subscription. Forex on
IDEALPRO has no stock-style L2 so the L2/T&S gates will be N/A (that's
expected — they degrade gracefully).

1. Boot the stack (backend + frontend) per the existing dev workflow.
2. Open `http://localhost:3000/engine`.
3. Fill the Arm form:
   - **Symbol**: `EUR.USD`
   - **Strategy**: `first_pullback_long`
   - **Entry trigger**: `macd_cross` for Phase 1 (the structural
     `pullback_break` trigger needs cleanly-coloured 1m candles —
     forex midpoint bars have very compressed bodies and won't form
     Ross-style red/green pullback sequences reliably; MACD cross-up
     is the better trigger on forex)
   - **MACD params**: defaults (12 / 26 / 9)
   - **Quantity**: 25000 (forex base-currency units; EUR.USD min is 25k)
   - **Order routing**:
     - Order type: `LMT`
     - Offset (cents): 1 (forex pip ≈ 0.0001 so 1c = 100 pips; you may
       prefer `MKT` here because the bid/ask spread is so tight)
     - Sell anchor: `bid` (default; on forex the spread is sub-pip so
       both anchors fill almost identically — don't overthink it)
     - Cancel-after (sec): 3
   - **Market data subscriptions**:
     - L2 depth: OFF (IDEALPRO doesn't have stock-style depth)
     - T&S: OFF
   - **DTD context**:
     - setup_type: `first_pullback`
     - leave the rest blank or fill anything you like — DTD context is
       recorded, not gated
   - **Risk caps**: defaults
   - **Mode**: `Manual approval` (you'll Approve / Reject each entry)
4. Click **Arm engine**.
5. Watch the **Strategy state** panel:
   - 1m MACD populates within ~1–2 minutes
   - 5m MACD takes longer (~30+ min — the engine does NOT bootstrap
     historical 5m bars; it builds them live)
   - VWAP shows `na` (forex midpoint bars have no volume)
   - "Last entry gate" shows the reason each bar is being rejected
     (most commonly "5m MACD not warmed up yet" early on)
   - "Last trigger" tile shows whether the trigger fired and why not
6. Watch the **Live event log**:
   - `bar` events every minute
   - `indicator` events every minute (and `tf=5m` every 5 minutes)
   - When all gates pass: `decision` + an approval banner
7. **Approve** to send a paper LMT order. Watch:
   - `order_submit` → `order_status` → `fill` → `slippage` events
   - The exit-trigger framework starts watching as soon as the entry fills
8. If an exit trigger fires (e.g. MACD flip), it submits a SELL and
   journals an `exit_trigger` decision. The strategy **auto re-arms**
   for the next entry.
9. Click **Stop engine** when you're done.

**Success criteria for Phase 1:**
- engine started without IBKR error
- bars stream every minute
- at least one entry signal fires
- at least one fill is recorded with non-null `slippage_cents`
- exit trigger fires correctly when MACD flips
- engine auto re-arms after exit

## Phase 2 — US RTH smoke (Monday 21:30 Perth = 09:30 ET)

Now we exercise the full L2/T&S pipeline AND the new Ross-style
pullback trigger. Requires a US equity subscription that covers your
chosen symbol.

1. Confirm `ibkr_check.py` no longer reports Error 420 for your chosen
   symbol. If it does: (a) buy the missing subscription, (b) use a
   symbol your existing subs cover, or (c) accept L2/T&S will be empty
   and rely on 1m + 5m MACD only.
2. Same Arm form with these changes:
   - **Symbol**: your DTD pick (the ticker you've decided to trade)
   - **Entry trigger**: `pullback_break` ← **this is the new default
     and the whole point of v1.2**. Trigger fires on the first 1m green
     candle whose high exceeds the high of the most recent red candle
     in the last pullback (the "Ross-style" structural break, matching
     your NVFY example at 3.73).
   - **Order routing**:
     - Order type: `LMT`
     - Offset: 10 cents (your usual hotkey offset)
     - Sell anchor: `bid` (default — aggressive exits, hits the bid).
       Switch to `ask` for runs where you'd rather sit on the offer side
       and let buyers come up.
     - Cancel-after: 3 seconds
   - **Market data subscriptions**:
     - L2 depth: ON
     - T&S: ON
   - **DTD context**: fill the actual values you read off the DTD widget
     - alert_type, setup_type (`first_pullback` / `micro_pullback` / etc),
       gap_pct, float_shares_millions, rel_vol, has_news, news_headline,
       premarket_high, dollar_volume_millions, notes
3. Click Arm. Watch the **Live features (L2 / T&S / VWAP)** card:
   - Best bid / ask streaming
   - Spread (bps) — green when tight, red when wide
   - Bid-ask imbalance — green when buyers dominant, red when sellers
   - Ask-wall detection — populates when a big offer stacks up nearby
   - Tape buy% and tape-speed live
4. Watch the **Last trigger** tile in the Strategy state panel for
   `pullback_break` activity:
   - "Fired: no, Reason: 'no red bars in history within lookback'"
     (early in the session)
   - "Fired: no, Reason: 'current high X did not exceed last-red-pullback
     high Y'" (a pullback is forming but the break hasn't happened yet)
   - "Fired: yes" + Test high + Pullback low + bar counts (the entry)
5. Approve / reject flow same as Phase 1. The stop suggestion shown in
   the order_submit event should be `pullback_low − 2c` (not the chart
   recent_low fallback).

**Success criteria for Phase 2:**
- L2 features populate with real data
- T&S features populate with real prints
- `pullback_break` trigger fires at least once with sensible test-high /
  pullback-low values you can verify on the chart yourself
- At least one of:
  - microstructure last-look gate blocks an entry (great — that means
    the gate works), OR
  - the entry goes through and the exit framework correctly responds to
    L2 / tape changes

## Phase 3 — sell-anchor A/B (optional, only if Phase 2 fired)

If Phase 2 produced fills cleanly, run a second short session to compare
sell-anchor behaviours:

1. Arm a second run on a different symbol (or same symbol after stopping
   and restarting).
2. Change **Sell anchor** to `ask`. Everything else identical.
3. Compare the `order_submit` events between the two runs:
   - `bid`-anchor SELL: `limit_price ≈ bid − 10c`, `anchor: 'bid'`
   - `ask`-anchor SELL: `limit_price ≈ ask − 10c`, `anchor: 'ask'`
4. Compare the resulting `slippage_cents`:
   - `bid` anchor should fill faster (often within the first
     `cancel_after_seconds` window) with worse but predictable slippage
   - `ask` anchor may not fill at all on hard exits (the cancel-on-
     timeout will fire); when it does, the fill is closer to the ask

This isn't a hard pass/fail — it's the data you need to decide which
anchor you prefer per situation.

## What's expected to FAIL / be missing (known limitations)

These are documented and intentional:

- **5m MACD slow to warm**: no historical bootstrap; ~30 min of live
  data before the 5m trend gate is meaningful.
- **No real P&L attribution into risk gate**: realized P&L is journaled
  on every fill but the daily-loss counter uses trade count + per-trade
  caps, not realised loss. Hard cap (max_daily_loss) is enforced on
  *cumulative slippage from the per-trade max*, not on actual fill P&L.
- **Multi-leg starter / scale-up NOT IMPLEMENTED**: scaffolded in
  `strategy_rules.yaml::entry_legs` (off) and documented in
  `principles.md::STARTER_THEN_SCALE_UP_THEN_MANAGE_OUT`. Engine today
  is single-leg only.
- **Psychological levels NOT IMPLEMENTED**: scaffolded but engine
  doesn't compute or score them yet.
- **L2-aware stop placement NOT IMPLEMENTED**: `pullback_low − 2c` is
  used; the "drop to next visible bid" principle isn't consulted yet.
- **Consecutive-loss counter NOT TRACKED YET**: YAML is `tracked_only`
  but the counter itself isn't persisted.
- **5m setups parked**: per your guidance (Ross trades 1m primarily).
- **No backtest harness yet**: can't replay a historical day offline.
  All audit data IS being persisted (engine_runs / engine_events /
  bar_aggregates) so it's ready to feed the harness once built.
- **Halt detection**: backside gate's "halt = veto" rule is documented
  but not wired to a halt feed.
- **LLM reasoner layer NOT BUILT**: architecture documented in
  `principles.md`; no model is in the decision loop yet.

## After the smoke test

Whatever happens, the run is fully audited in:

- `engine_runs` row — full config (including the new `sell_anchor`,
  `order_type`, `dtd_context`, depth/tape flags) + final P&L counters
- `engine_events` rows — every bar, indicator update, signal, decision,
  fill, slippage, exit trigger, error
- `bar_aggregates` — the 1m bars with MACD values for chart replay

You can pull any run's full timeline via:

```
GET /engine/runs/{run_id}                  # config + summary
GET /engine/runs/{run_id}/events           # every event
GET /engine/runs/{run_id}/bars             # 1m bars
```

These three tables are the substrate for the future backtest harness
and for tuning the gate thresholds.

## Quick checklist (printable)

- [ ] TWS up, paper account logged in, disclaimer accepted
- [ ] `alembic current` → `4e1f0a82c9b1 (head)`
- [ ] `pytest` in `backend/` → 68 passed
- [ ] `scripts/ibkr_check.py` → connect + at least one symbol returning data
- [ ] Backend + frontend running, `/engine` page loads
- [ ] **Phase 1 (forex)**: arm EUR.USD with `macd_cross`, at least one fill, exit fires, auto re-arm works
- [ ] **Phase 2 (US RTH)**: arm your DTD pick with `pullback_break`, L2/T&S features stream, trigger fires on a real pullback break with sensible test-high / pullback-low
- [ ] **Phase 3 (optional)**: sell-anchor A/B comparison
- [ ] Stop engine; pull `/engine/runs/{run_id}/events` to confirm full audit trail
