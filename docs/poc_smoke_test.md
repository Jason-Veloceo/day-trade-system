# POC trading engine - smoke test playbook

The goal of the smoke test is to prove end-to-end that the engine can:

1. Connect to IBKR TWS on the paper account.
2. Stream live bars for a real instrument.
3. Compute MACD on a 1m timeframe.
4. Emit a signal, park it for manual approval (first run) or auto-execute it
   (second run).
5. Submit a market order on the paper account.
6. Receive a fill from IBKR.
7. Persist orders, fills, slippage, latency.
8. Render every event live in the `/engine` UI.

If any of the above fails, the engine is not ready for the Ross strategy
swap-in. Capture the failure mode in the run notes.

This playbook expects you to know which instrument you want to trade. The
default choice for first-time validation is **EUR.USD** because IBKR usually
allows live forex data on paper accounts without a subscription.

## 0. Pre-flight (one-time setup, all should already be done)

- TWS installed and logged into the PAPER account (`DUM*` prefix).
- TWS API enabled (Global Configuration -> API -> Settings):
  - [x] Enable ActiveX and Socket Clients
  - [ ] Read-Only API
  - Socket port: **7497** (paper)
  - Master API client ID: **101** (we use 37 for trading, NOT 101)
  - [x] Allow connections from localhost only
  - Trusted IPs include **127.0.0.1**
- `.env` populated with:
  - `IBKR_HOST=127.0.0.1`
  - `IBKR_PORT=7497`
  - `IBKR_CLIENT_ID=37`
  - `IBKR_TRADING_MODE=paper`
  - `IBKR_MARKET_DATA_TYPE=delayed` (or `live` once you've subscribed)
  - `PAPER_TRADING_ONLY=true`
  - `LIVE_TRADING_ENABLED=false`
  - `MANUAL_APPROVAL_REQUIRED=true`
- Docker postgres container up: `cd infra && docker compose up -d postgres`
- Migrations applied: `cd backend && uv run alembic upgrade head`

## 1. Pre-flight smoke (no engine yet)

Confirm the wiring before starting the engine.

```bash
cd backend && uv run python ../scripts/ibkr_check.py
```

Expected output:

- `connected.` to TWS.
- `Managed accounts: ['DUM...']`.
- Account summary lists `NetLiquidation`, `BuyingPower`, etc.
- For at least one probe (SPY or EUR.USD), `has_quote? YES` is reported.
- Final result line says `GREEN` or `PARTIAL`.

Common failures and what they mean:

| symptom | cause | fix |
| --- | --- | --- |
| `connection refused` | TWS not running, or API not enabled | start TWS, enable API |
| `Error 326 / 502` | client ID collision (running another script) | kill the other client; change `IBKR_CLIENT_ID` |
| `Error 10089` on quotes | no market data subscription for that instrument | enable free delayed data in Client Portal, or pay for a real-time feed |
| All `has_quote? NO` | market closed OR no subscriptions | re-run during RTH / forex open OR check subscriptions |

## 2. Start backend + frontend

```bash
# terminal A: backend
cd backend && uv run uvicorn day_trade.app:app --port 8000 --reload

# terminal B: frontend (must be on port 3000)
cd frontend && npm run dev -- --port 3000
```

Open <http://localhost:3000/engine>. You should see:

- The connection pill says "WS connected".
- The "Start a run" form is visible.
- "Recent runs" panel says "No runs yet" (first time) or lists prior runs.

## 3. Manual-approval run (DO THIS FIRST)

1. Choose an instrument with currently-streaming live data. For weekday US RTH
   pick `SPY`; for any other time pick `EUR.USD` during forex hours
   (Sun 5pm ET through Fri 5pm ET).
2. On `/engine`, fill in the form:
   - **Symbol**: `EUR.USD` (or `SPY`)
   - **Quantity**: `25000` (EUR.USD min) or `5` (SPY)
   - **Strategy**: `MACD crossover (long-only)`
   - **MACD params**: 12 / 26 / 9 (defaults)
   - **Risk caps**: leave defaults
   - **Run autonomously**: leave UNCHECKED
3. Click **Start engine**.

Expected:

- The form is replaced by the "Active run" panel showing run ID, IBKR
  account, quantity, status `running`, mode `Manual approval`, etc.
- The event log immediately shows: `engine_start`, `ibkr_connected`.
- Within ~5 seconds: `bar` events start arriving (one every minute).
- After ~30 minutes (MACD needs to warm up): `indicator` events start
  showing non-null MACD values.
- Eventually: a `signal` event followed by `ready_for_approval`. The amber
  "Signal awaiting approval" banner appears at the top of the page.

While waiting, observe:

- The Strategy state panel updates MACD line / signal / histogram every bar.
- The histogram tile flips from rose (negative) to emerald (positive) as
  the cross happens.

On the first approval banner:

1. Click **Approve**.
2. Watch the event log for: `approval_granted` -> `order_submit` ->
   `order_status` (Submitted -> Filled) -> `fill` -> `slippage`.
3. **Note the numbers**: the `slippage` row reports `signal_price`,
   `fill_price`, slippage in cents and bps, and latency in ms. Screenshot it.

When you want to stop the run, click **Stop engine**. The active panel
disappears and the run shows up in "Recent runs" with `status=stopped`.

## 4. Autonomous run

Repeat step 3 with:

- **Run autonomously** CHECKED.
- Everything else the same.

Expected differences:

- No approval banner ever appears.
- Signals immediately turn into orders. The event log shows
  `signal` -> `decision` -> `order_submit` (no `ready_for_approval` in
  between).
- Multiple round-trips can happen within a run. The risk gate limits
  them to `max_trades_per_run` (default 5).

While the autonomous run is in flight, eyeball the slippage column in the
event log. If `slippage_cents` is consistently 100+ for an instrument
trading around $1.16, the engine is feeding on delayed data (expected
when `IBKR_MARKET_DATA_TYPE=delayed`). Re-run on live data to get a real
slippage number.

## 5. Audit the run from the DB

```sql
-- summary
SELECT id, symbol, autonomous, status, trades_count, realized_pnl,
       started_at, stopped_at, stop_reason
FROM engine_runs
ORDER BY id DESC
LIMIT 5;

-- event timeline of the latest run
SELECT id, ts, event_type, payload
FROM engine_events
WHERE run_id = (SELECT MAX(id) FROM engine_runs)
ORDER BY id;

-- fills with slippage attribution
SELECT f.id, f.symbol, f.qty, f.price AS fill_price,
       f.signal_price, f.slippage_cents, f.slippage_bps, f.latency_ms,
       o.ibkr_order_id, o.side
FROM fills f
JOIN orders o ON o.id = f.order_id
WHERE f.engine_run_id = (SELECT MAX(id) FROM engine_runs)
ORDER BY f.id;
```

For a healthy run, the event timeline should have a strict ordering:
`engine_start` -> `ibkr_connected` -> many `bar`/`indicator` -> `signal`
-> (`ready_for_approval` -> `approval_granted` OR `decision`) ->
`order_submit` -> `order_status` -> `fill` -> `slippage` -> ... ->
`engine_stop`.

## 6. Failure modes to deliberately exercise

To prove the safety rails work, run these once you're confident in the
happy path:

1. **Wrong account**: set `PAPER_TRADING_ONLY=true` and try to connect to a
   live account (`U*` prefix). Expected: the engine refuses to start with
   `IBKRSafetyError`.
2. **Kill switch via daily loss**: set `max_daily_loss_usd=0.01` and run
   autonomously. After the first losing exit the kill switch engages and
   no further entries fire. Confirm via `risk_block` events.
3. **Already-running engine**: try to start a second run while one is
   active. Expected: HTTP 409 with `EngineBusyError`.
4. **TWS disconnect mid-run**: pull the TWS plug. Expected: the engine
   reports an `error` event and (currently) does not auto-reconnect;
   you must click Stop and start a fresh run.

## 7. Go / no-go criteria for moving on to Ross

- All manual-approval and autonomous flows worked end-to-end.
- Slippage numbers were collected for at least 10 fills on live data.
- The event log contained no `error` events that we couldn't explain.
- The DB has consistent `orders` + `fills` rows for every engine fill.

When all four are green, the engine plumbing is proven and we can replace
the MACD strategy with a Strategy-ABC implementation of the Ross rules
described in `strategy_sources/ross_notes.md`.
