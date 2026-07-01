# day-trade

Local Mac-only AI-assisted day-momentum trading copilot for Interactive
Brokers paper accounts. Ross-Cameron-style first-pullback / MACD long
strategy on small-cap US equities, with forex as a 24-hour smoke-test rail.

> Personal research and paper-trading project. Not financial advice. Not a
> public product. **Live trading is hard-disabled** (`LIVE_TRADING_ENABLED=false`,
> `PAPER_TRADING_ONLY=true`); the system refuses to submit orders outside
> paper.

## Current state — June 26, 2026

**v1.3 multi-engine dashboard** is the active surface. Visit `/engine`.
You arm up to 4 symbols simultaneously, each runs in its own
`TradingEngine` with independent bars / indicators / gates / exits, and
a portfolio-wide execution mutex (`PortfolioRiskGate`) guarantees only
**one position is open at a time across the entire dashboard**. The
other engines keep evaluating gates and journal
`entry_blocked_by_portfolio_mutex` events whenever they would have
fired — giving you the calibration data ("what setups did I miss while
holding something else") for the multi-engine workflow.

**First end-to-end paper trade landed Fri 26 Jun** on ILLR (run #25):
BUY 20 @ $5.15 → SELL 20 @ $5.29 = **+$2.80 (+2.7%) in one minute**.
Full pipeline validated: mutex acquire → marketable LMT submit → fill →
exit-framework evaluation → mutex release.

**🎯 Major unblock on Wed 24 Jun**: full L1 + L2 (NASDAQ TotalView) + T&S
+ historical + real-time bars are now **all flowing via the API on paper
account `DUM733674`**. The wrong-account-routing hypothesis was correct;
once the Trust account `U23755393` cleared the USD 500 funding minimum,
IBKR propagated entitlements user-wide and the original paper account
inherited live equity data overnight. We confirmed end-to-end on FRTT
(a +70% small-cap mover with multiple volatility halts that day):

- **L2** via `reqMktDepth(numRows=10, isSmartDepth=True)`:
  ~21 updates/sec, real ladder with DRCTEDGE, MEMX, PEARL, EDGEA, CHX,
  BYX market makers visible.
- **T&S** via `reqTickByTickData('AllLast')`: ~18 prints/sec, multi-exchange
  tape (FINRA, ARCA, NASDAQ).
- **BBO** via `reqTickByTickData('BidAsk')`: ~18 updates/sec.
- **Marketable LMT round-trip via the API**:
  `scripts/manual_trade_test.py FRTT 10` filled BUY @ 3.18 and SELL @ 3.17
  with price improvement on both sides; total cycle 13s, P&L ~-$0.10
  (spread).

Previous successful round-trip on Mon 22 Jun (SKYQ 10 shares pre-market)
remains the original proof that order plumbing works.

The engine's `engine/orderbook.py` + `engine/features.py` layers, which
were scaffolded but never exercised, are now unblocked. The natural next
build is the Bookmap-style L2/T&S feature layer (resting wall detection,
aggressor imbalance, absorption, sweep / spoof detection) — see the
"Open follow-ups" section below.

EUR.USD on IDEALPRO is still the overnight smoke rail and streams bars
cleanly under the historical-bar bootstrap (MACD/VWAP warm up
immediately, no 30-minute wait).

The earlier DTD scanner/funnel work (`/candidates`, `/rules`, `/rejected`,
`scripts/dtd_*.py`) is **still in the repo but on hold** since the pivot
to the core engine. It works if you log in, but it isn't wired to the
engine yet.

### What you can actually test now

1. **Forex smoke (Phase 1)** — IDEALPRO is open ~24/5, so any time you
   can arm e.g. `EUR.USD` with the `macd_cross` trigger and see bars,
   MACD, and (paper) orders flow. See
   [`docs/monday_smoke_test.md`](docs/monday_smoke_test.md) Phase 1.
2. **US RTH smoke (Phase 2)** — Monday during US RTH, arm a real small
   cap (e.g. a name from the Warrior scanner) with the `pullback_break`
   trigger and `sell_anchor=bid`. Same doc, Phase 2.
3. **Sell-anchor A/B (Phase 3, optional)** — re-run the same setup
   with `sell_anchor=ask` to compare aggressive vs passive sell fills.

### Recently landed

- **v1.3 multi-engine substrate** (Fri 26 Jun, commits `e8adfce` →
  `9c907c4`):
  - `EngineRegistry` replaces the `EngineRunner` singleton — up to
    `max_concurrent_engines` (default 4) `TradingEngine` instances
    run concurrently, each fully independent (own BarFeed,
    strategy, gate stack, exits).
  - `PortfolioRiskGate` (new) holds a portfolio-wide execution
    mutex with `asyncio.Lock`-serialised `try_acquire_for_entry` /
    `release`. While the mutex is held by one engine, others
    journal `entry_blocked_by_portfolio_mutex` instead of
    submitting. Released on position-flat (after the exit fill
    confirms `position.qty == 0`).
  - Portfolio-level risk caps separate from per-engine caps:
    `max_daily_loss_usd` ($200 default), `max_concurrent_engines`
    (4), `max_total_trades_per_day` (10). Daily kill switch trips
    on loss limit; rolls at UTC midnight; `POST
    /engine/portfolio/reset_kill_switch` for manual reset.
  - API: `POST /engine/start` allows up to 4; `POST
    /engine/stop?symbol=X`; `POST /engine/stop_all`; `POST
    /engine/approve?run_id=…`; `GET /engine/status` returns
    `{engines: [...], portfolio: {...}, slots: {...}}`; `GET
    /engine/portfolio` for just the mutex state.
  - Multi-card dashboard UI: sidebar of active engines + main
    detail panel + portfolio top bar (mutex holder, daily P&L,
    trade count, slots used, kill switch). "+ Add engine"
    button to spin up new slots; "Drop & replace" on each card to
    swap the symbol inline. Per-engine "Non-tradable" banner when
    mutex is held elsewhere.
  - 31 new tests covering registry lifecycle, portfolio risk
    mutex, mutex+engine integration (acquire-before-submit,
    release-on-flat, release-on-stop, denied-by-kill-switch).
- **Bug fixes Fri 26 Jun PM (committed `3ab93a0`, `80afc0f`,
  `9c907c4`):**
  - `+ Add engine` button no longer flickers when the form's
    default symbol matches an already-running engine.
  - Engine in-memory `status` was permanently stuck on `"starting"`
    (dead `_running_task` field, never assigned anywhere) — now
    derived from `self.feed is not None`.
  - Live event log's `indicator` summary always rendered `5m=—
    vwap=—`, even on `macd_crossover_long` engines (a 1m-MACD-only
    strategy whose snapshot intentionally omits those fields).
    Looked like 5m MACD was broken / unwarmed when it wasn't even
    being computed. Now branches on `strategy.name`:
    macd_crossover_long renders `1m=<value>  (1m MACD only)`.
  - Right-panel `StrategyStatePanel` had the same issue: misleading
    `—` tiles for macd_crossover_long. Now shows 1m hist + MACD
    line + signal line for that strategy.
  - Live event log was missing filter pills for `indicator`,
    `ready_for_approval`, and `order_submit` — those events were
    hidden inside "all" only. Added with friendly labels
    (`approval`, `order`).
  - **Phantom-position bug** (HKIT incident, Fri 26 Jun ~22:57):
    the engine optimistically called `risk.record_open(qty)` and
    `exits.open(...)` at order-SUBMIT time, BEFORE waiting for the
    BUY to actually fill. On a wide-spread micro-cap (HKIT,
    bid 0.18 / ask 0.36), the BUY cancel-on-timeout fired with 0
    fills — but the engine still thought it owned 20 shares.
    Next bar's `low` dipped below the % stop, `hard_stop` exit
    fired, and the engine submitted a SELL to close a position
    that never existed. Now `_handle_enter` only submits + latches
    the strategy; new `_on_entry_fill` / `_on_entry_status`
    callbacks promote pending → in-position on the first IBKR fill
    (using the actual avg fill price for `entry_price`, not the
    signal price), or roll back cleanly on cancel-with-zero-fills.
    Tests: `test_entry_cancelled_without_fill_does_not_open_position`,
    `test_entry_first_fill_opens_exits_with_actual_fill_price`.
  - **Spread-aware limit offset** (same HKIT incident): a fixed
    `limit_offset_cents=10` produced a LMT BUY at 0.37 on a stock
    trading around 0.27 (37% of price). The executor now caps the
    effective offset at `max(1c, 2% of mid)`, so cheap names get a
    sane offset while normally-priced stocks ($5–$50) are unchanged.
    Also journaled as `effective_offset_cents` for audit.
- **2-day bootstrap + `require_5m_macd` toggle** (Wed 24 Jun PM): the
  engine now requests `durationStr="2 D"` of 1m historical bars on
  Arm (previously 4 hours), so 5m MACD warms instantly via
  TradingView-style cross-session carry-through. The earlier 4-hour
  window broke on fresh Ross-scanner movers: e.g. FRTT had only 78
  1m bars at arm time (~16 5m bars), well short of the ~26 5m bars
  needed for MACD(12/26/9), so `macd_5m_hist` returned `None` and the
  trend gate failed with "5m MACD not warmed up yet". The 2-day
  window includes yesterday's session, giving 600+ 1m bars / 120+ 5m
  bars on any actively-traded name. New `require_5m_macd: bool`
  toggle on the Arm form (under "Gate stack") lets the user opt out
  of the 5m gate entirely for fast-pivot scenarios on brand-new
  movers — the gate then evaluates 1m MACD + VWAP + backside +
  trigger only. Default ON (safer, Ross-style broader-trend filter).
  Persisted on the `engine_start` audit event and surfaced on
  `/engine/status`.
- **Historical-bar bootstrap** (`183f5da`): on every Arm, the engine
  pulls historical 1m bars from IBKR and replays them through the
  strategy + the 5m aggregator so 1m MACD, 5m MACD, VWAP and the
  pullback history are warm immediately. No more 30-minute warm-up
  before the gate stack is meaningful. Replayed signals are discarded;
  a single `bootstrap` audit event records what was preloaded.
- **Live forming candle** (in flight): the `BarFeed` now publishes
  `engine.bar_tick` updates every 5 seconds with the in-progress 1m
  bar's running OHLC; the chart updates the rightmost candle in real
  time. Strategy decisions remain strictly bar-close driven (no change
  to entry/exit behaviour). Visual-only — not journaled.
- **Orphan engine_run sweep on backend startup**: any `engine_runs`
  rows left in `starting` / `running` / `stopping` (e.g. because
  uvicorn `--reload` killed the previous process before `stop()` could
  journal) are swept to `stopped` with `stop_reason =
  'backend_restart_orphaned'` on the next backend boot. Keeps Recent
  Runs honest.

### Wed 24 Jun PM session — engine validated on live US equities

First two real engine runs on US small-caps, both behaving exactly
to spec:

- **Run 14: LHSW** — engine armed cleanly, bootstrap pulled 241 1m
  bars + 48 5m bars from the original 4-hour window, but `macd_5m_hist`
  returned a stable value. Gate stack evaluated normally. No setup
  fired (quiet market). Run stopped clean.
- **Run 15-16: FRTT** — engine armed; on run 15 the gate stack failed
  with "5m MACD not warmed up yet" (only 78 1m / 16 5m bars from the
  4-hour window — the trigger that motivated the 2-day bootstrap fix).
  After the fix landed and we re-armed (run 16), 5m MACD was warm
  immediately. Gate stack THEN correctly refused FRTT on the **backside
  veto** ("price below VWAP for 55 consecutive bars (threshold 3)"):

  ```
  last_entry_gate.passed = False
  last_entry_gate.failures = [
    "price below VWAP (3.0400 vs 3.5617)",
    "backside: price below VWAP for 55 consecutive bars (threshold 3)",
    "trigger (pullback_break): current bar is not green (close <= open)"
  ]
  backside.block = True
  backside.hard_vetoes = ["price below VWAP for 55 consecutive bars (threshold 3)"]
  ```

  This is **the most important behavioural validation we could have
  asked for** — an "inexperienced trader" would have bought FRTT
  ("look how cheap it is now") and chopped to death; the engine saw
  the backside structure and refused. The Ross-style discipline
  encoded in `backside.py` worked on real US equity data on day one.
- **IBKR Warning 2152** fired on both LHSW and FRTT depth
  subscriptions: "Need additional market data permissions - Depth:
  NASDAQ; BATS; ARCA; BEX; NYSE". This is informational, not
  blocking. With `isSmartDepth=True` (engine default) IBKR returns
  IEX full depth + top-of-book from ~14 other exchanges
  (BYX/AMEX/PEARL/T24X/MEMX/EDGEA/etc.) aggregated as "smart depth".
  Our `ibkr_l2_check.py` smoke probe sees the same picture: real
  multi-level book with DRCTEDGE/MEMX/PEARL/EDGEA/CHX/BYX market
  makers — sufficient for Bookmap-style features (resting walls,
  aggressor imbalance, absorption, sweeps). True multi-level NASDAQ
  TotalView depth would require `isSmartDepth=False` with explicit
  `exchange="ISLAND"` routing, but is not required for our use case.

### Fri 26 Jun PM session — v1.3 multi-engine shipped + first paper trade

Pure paper-trading session during US RTH. Four-day multi-engine slice
(Phase 1) shipped end-to-end across `e8adfce` → `9c907c4`. Listed under
"Recently landed" above; this section is the live-testing narrative
that surfaced the open follow-ups.

**Multi-engine live test (4 concurrent engines on US equities):**
SDOT, CANE, ILLR, REA were armed simultaneously on
`first_pullback_long`. All 4 bootstrapped 2 days of historical bars
successfully and indicators were warm from the first live bar (1m
MACD, 5m MACD, VWAP, HoD all populated). Confirmed via
`GET /engine/status`. The mutex stayed idle (`holder=None`) the entire
time the user watched — gate stack consistently refused all 4
candidates on a mix of:

- "5m MACD histogram is falling"
- "backside: 1m MACD has already crossed down today"
- "trigger (pullback_break): current bar is not green"

**ILLR's `pullback_break` trigger ACTUALLY FIRED** ("green bar broke
last-red-pullback high 4.86, current high 5.06") but the **backside
veto stopped the entry** because 5m MACD was negative and falling.
This is the second time on US equities the engine has shown discipline
in refusing a fired trigger on broader-trend grounds. Working as
designed.

**First end-to-end paper trade (ILLR run #25):** to validate the
execution pipeline without waiting for a pullback_break setup, three
slots were re-armed on the looser `macd_crossover_long` strategy with
`autonomous=true`:

| Time | Event | Detail |
|---|---|---|
| 14:33:00 | `signal: enter_long` | 1m MACD histogram crossed positive (−0.002 → +0.001) |
| 14:33:00 | `decision: auto_execute_enter` | Autonomous mode bypasses approval |
| 14:33:00 | `order_submit` | BUY 20 LMT @ 5.17 (`ask + 10c`) |
| 14:33:09 | `fill` | BUY 20 @ **5.15** (1.5c price improvement) |
| 14:34:00 | `decision: exit_trigger=second_target` | bar high 5.36 ≥ 2R target 5.2683 |
| 14:34:00 | `order_submit` | SELL 20 LMT (`bid - 10c`, `exit_trigger=second_target`) |
| 14:34:05 | `fill` | SELL 20 @ **5.29** (2.99c price improvement) |

Net: **+$2.80 (+2.7%) on $103 position in 66 seconds**, mutex cleanly
released, portfolio realized P&L updated. The full pipeline works.

**Design mismatch surfaced — exit framework is NOT Ross-style.** The
trade closed on the `SECOND_TARGET` trigger (fixed 2R take-profit),
firing the moment `bar.high >= entry + 2*R`. MACD was still strongly
positive at the time. Ross Cameron does NOT set price targets — he
reads L2 (ask wall formation, level absorption) and tape (buy/sell
flow imbalance, speed decay) in real time to find resistance and exit
dynamically. Our existing `L2_DISTRESS` and `TAPE_FLIP` triggers are
defensive (fire when the move is failing), not profit-taking. See the
**"Ross-style exit redesign"** item in Open follow-ups for the
proposed Saturday-morning work.

**Stop-loss provenance (worth knowing):** `macd_crossover_long` is a
"legacy" strategy and does not expose `suggest_stop_price`, so the
engine fell back to `entry * 0.99` in `_handle_enter`. That set the
stop at 5.1134 and the 2R target at 5.2683. For the redesigned exit
framework we should also revisit the legacy 1% fallback — Ross stops
are structural (last swing low / VWAP / wall), not percentage-based.

### Tue 30 Jun PM session — 10s cadence, auto-arm v1, DTD observer UI, live-channel mystery

Multi-hour session focused on closing the gap between "engine evaluates
every 1 minute, exits on fixed R-multiples" and Ross-Cameron-style
high-frequency tape-reading. Three concrete pieces shipped, one deep
unsolved bug surfaced.

Ports moved to **8010 (backend) / 3010 (frontend)** for this session
because another local app on the laptop was holding 8000/3000. The
.env.local update is in place; revert if/when 8000/3000 free up.

**1. Item 1 — 10-second tick cadence (✅ shipped, verified live on AIRJ).**

`TradingEngine` now drives sub-bar evaluations every `eval_tick_seconds`
(default 10s) off the existing `_on_partial_bar` callback, in addition
to the existing 1m bar-close path.

- New `Strategy.on_tick(partial: Bar)` default no-op; `FirstPullbackLong`
  overrides to evaluate the pullback-break trigger mid-candle. It
  optimistically sets `_in_position` to prevent double-fires within
  the same candle, but is otherwise **read-only on indicator/latch
  state** (no MACD/VWAP mutation off ticks).
- New `ExitTriggerSet.on_tick(partial: Bar)` evaluates the price-driven
  exits (`hard_stop`, `first_target`, `second_target`) and `l2_distress`
  using the in-progress bar. **Does NOT touch `ExitState` counters**
  (`bars_since_entry`, `bars_below_vwap_since_entry`) — those only
  advance on full bar closes. MACD flip, VWAP loss, tape flip and
  time stop remain bar-close only.
- New rate-limited `_on_tick(partial)` on the engine; journals only on
  signals or decisions (no spam).

Tests: `backend/tests/test_engine_tick.py` covers mid-candle entry,
no-double-fire-within-candle, exit triggers on partial bars, counter
non-mutation, and the `eval_tick_seconds` rate limit.

**2. Backside latch pollution fix (✅ shipped + tested).**

Two-day historical bootstrap was unintentionally setting session-level
latches in `BacksideState` (e.g. `macd_1m_has_crossed_down_today`)
AND the optimistic `_in_position` flag during replay, causing
"1m MACD has already crossed down today" rejections on freshly armed
engines and intermittent "phantom in-position" symptoms. Fixed via:

- New `Strategy.finalize_bootstrap(*, pmhod, pdhod)` called by the
  engine immediately after the replay loop. Resets session-specific
  latches and `_in_position` while preserving warmed indicator values.
- New `_compute_session_levels(bars_1m)` helper: derives Premarket
  HoD (today 04:00–09:30 ET high) and Previous-day HoD (last prior
  RTH high) from the replayed history. Both stored on
  `BacksideState.pmhod` / `pdhod` and surfaced in the UI as new
  Tile cards on the engine page (next to live HoD).
- `bootstrap` audit event now includes `pmhod` / `pdhod`.

Tests: `backend/tests/test_engine_bootstrap.py`.

**3. Typed gate failures (✅ shipped + tested).**

`EntryGateResult.failures` and microstructure-gate failures are now
`list[GateFailure]` with a `GateFailureCategory` enum
(`WARMUP | INDICATOR | VWAP | BACKSIDE | TRIGGER | MICROSTRUCTURE`),
not free-text strings. The engine page groups them by category in a
new `GateFailureList` component so "5m MACD warming" doesn't look the
same as "backside latched". JSON serialiser updated to walk
dataclasses + enums; regression test added in
`test_engine_journal.py`.

**4. Item 2 — Scanner-driven auto-arm worker MVP (✅ shipped + tested,
~partly verified live).**

Goal from the user: "He hits the small-cap HoD momentum scanner →
clicks on it → waits for a clean entry. I want THAT, automated." So:

- New `backend/src/day_trade/auto_arm/` module:
  `policy.py` (pure decision logic), `worker.py` (background polling
  task), `__init__.py` re-export.
- New `api/auto_arm.py`: `GET /auto_arm/status`, `POST
  /auto_arm/enable`, `POST /auto_arm/disable`. The runtime enable/
  disable mutates the in-memory `Settings.auto_arm_enabled` so the
  next poll-tick observes the change without restart.
- New settings (config.py): `AUTO_ARM_*` knobs incl. widgets,
  strategy, quantity, order type, depth/tape toggles, autonomous
  mode, daily/hourly/concurrent limits, ET trading window
  (`04:00–11:30` default), re-arm cooldown (30min), staleness
  threshold (5min), poll cadence (2s).
- Worker loop integrated into the FastAPI lifespan; runs whether
  enabled or not so the toggle is responsive.
- Frontend: violet `AUTO` badge on sidebar cards for auto-armed
  engines, with hover-tooltip showing which widgets fired.

Tests: 26 tests in `test_auto_arm_policy.py` covering every gate
in `decide()` and every branch in `is_engine_stale()`.

**4a. Auto-arm "armed-and-killed-in-12s" bug (✅ fixed, root cause
analysed and regression-tested).**

First live attempt armed EEIQ and the staleness watcher killed it
12 seconds later. Root cause: circular logic — the arm path's
lookback window (5 min) and the staleness threshold (5 min) were
the same, so a candidate whose `last_alert_at` was 4:48 ago could
be armed and then immediately stalened out.

Fixes (both shipped):
- **Tighter arm lookback** — only consider candidates with
  `last_alert_at` newer than `auto_arm_lookback_seconds` (default
  **90s**). Must be strictly tighter than the staleness threshold.
- **Staleness grace period** — `is_engine_stale` will not kill an
  engine younger than `auto_arm_grace_period_seconds` (default
  **120s**). Belt-and-braces against the same bug class.

Net effect: even if we arm on a candidate at the very edge of the
lookback (89s old, alert at T-89s), the engine has the full grace
period (120s) plus the remaining staleness budget (5min − 89s ≈ 3:31)
of guaranteed runway. 4 new regression tests in
`test_auto_arm_policy.py` (now 26 tests passing).

**5. Item 2b — DTD observer subprocess control from the UI (✅
shipped + tested).**

User pain: "I shouldn't have to run two terminals and remember
`dtd_login.py` then `dtd_run.py`". Now there's a Start/Stop bar on
the engine page, with a colour-coded health pill (green/sky/amber/
rose/neutral) driven by `last_event_age_seconds`.

- New `backend/src/day_trade/dtd_control/controller.py`:
  `DtdObserverController.start()/stop()/status()`, spawns
  `scripts/dtd_run.py` via `subprocess.Popen` with `start_new_session=True`
  so it survives `uvicorn --reload`. PID persists to
  `var/dtd_observer.pid`; logs to `var/dtd_observer.log`; `waitpid`
  loop drains zombies after SIGTERM/SIGKILL.
- New `backend/src/day_trade/api/dtd.py`: `GET /dtd/observer/status`,
  `POST /dtd/observer/start`, `POST /dtd/observer/stop`.
- Frontend: new `DtdObserverBar` component on the engine page, polls
  status every 3s via SWR.
- `.gitignore` updated to ignore `var/`.

Tests: 9 tests in `test_dtd_controller.py` covering pidfile
lifecycle, idempotent start, SIGTERM-then-SIGKILL escalation,
zombie reaping via `os.waitpid(WNOHANG)`, stale-pidfile cleanup,
missing-script error path. **Stand-in sleep script** is used to
avoid Playwright/Chromium in the test path.

**Backend test summary at session end: 177 / 177 passing.**

**6. 🔴 UNSOLVED — DTD observer "silent failure" bug (live-channel
mystery).**

This is the blocker. The user's screenshot during the session
showed the WT Small-Cap HoD Momo scanner widget firing alerts every
few seconds (AKTX 11:01:30, FDMT 11:01:21, SOTK x3, TDTH, XHLD, etc),
but our `scanner_events` table stopped receiving anything ~12+
minutes earlier. The observer **process is alive** (Playwright
context responsive) but no new rows land in the DB.

Diagnosis run via `scripts/dtd_diagnose_ws.py` (new this session,
60-second capture of all HTTP + WS traffic in the persistent
profile):

- **Hosts seen:** chatroom.warriortrading.com (mostly assets),
  api-prod.warriortrading.com, scan-prod.warriortrading.com,
  www.warriortrading.com.
- **`/alert?widget=X` HTTP responses:** 3 in 60 seconds — ONE per
  widget (`Running_Up`, `Momo`, custom `E30AE4F9`). Each returns a
  huge JSON snapshot (`{"count":1666,"data":[...]}` ≈ 1.3 MB for
  Momo) — i.e. **the full backlog from 04:00 ET to now**, in a
  single page-load fetch. **Never polled again.**
- **WebSocket connections to anything WT-related:** ZERO. The only
  WS seen is `wss://ws.hotjar.com/...` (analytics, unrelated).
- **No streaming / SSE / chunked endpoint identified** in the 60s
  window. No `/v2/alert?since=…` polling. `/toplist` was called
  6 times but appears to drive the "Top Gainers" panel, not the
  Momo widget.

**This means our `DtdObserver` (which only listens to
`/alert?widget=` HTTP responses) is architecturally correct for the
*initial backlog* and architecturally blind to the *live updates*.**
That's why restarting the observer briefly "works" (reload page →
re-fetch backlog) then goes silent forever.

What we don't yet know (the diagnostic to re-run tomorrow during
active scanner activity):

1. Does `/alert?widget=Momo` actually contain events from the last
   minute when called? (The full-body capture is now wired in
   `scripts/dtd_diagnose_ws.py` to `playwright_profile/_inspect/alert_bodies/`
   — inspect the tail of the saved file.)
2. Is there a **long-polling** request that's pending at capture-end?
   Diagnostic v2 now also logs `request`-start events, not just
   completed responses — pending requests will be visible as
   `http_request` lines without a matching `http` (response) line.
3. Is the **scanner widget a popup window** that our diagnostic
   didn't navigate to? The diagnostic just opens the dashboard
   URL — if the actual scanner is a popup the user clicks through
   to, our context might be capturing the chatroom shell but not
   the scanner page's traffic.
   - Mitigation in v2: capture window is 120s, with a print
     instruction telling the user to click through to the scanner.
4. Is the user-visible "live" data actually being computed
   **client-side** from the backlog (server periodically pushes a
   small diff over chatroom socket.io) rather than fetched from
   scan-prod?

**Working hypothesis:** scenario (3) is most likely. The Warrior
Trading dashboard opens scanner widgets as popups (the user's
earlier comment "I had to click through to the scanners again"
strongly implies this). Our `open_dtd_page` just navigates the main
context; popups are tracked but the user may not actually open the
Momo popup during the diagnostic window, so the scanner-specific
endpoint is never hit. Easy to validate tomorrow: re-run the
diagnostic and explicitly click through to the Momo widget while
it's running.

**Operational impact:** auto-arm is dependent on this. Until the
live-channel ingestion is fixed, the worker has nothing fresh to
arm on (the candidates table only refills from the
once-per-page-load `/alert` backlog dump). The end-to-end loop
*ran cleanly when tested with a manually-restarted observer that
happened to have fresh enough backlog* (EEIQ was armed correctly,
then killed by the bug we then fixed in #4a), but it's not yet a
stable scanner-driven auto-arm loop in steady state.

### Tomorrow's plan (priority order)

1. **Pin the live channel.** Premarket / open. Stop any orphan
   Chromium first (`ps aux | grep -i chromium | grep day-trade`,
   kill if any). Then:
   ```
   cd backend && uv run python ../scripts/dtd_diagnose_ws.py
   ```
   When Chromium opens, click through to the actual Momo scanner
   widget popup and leave it visible. The 120s capture will record
   every HTTP request (incl. pending long-polls), every WS frame,
   and save full `/alert` bodies to
   `playwright_profile/_inspect/alert_bodies/`. Inspect the tail of
   the Momo body to confirm whether it contains recent (last-60s)
   events — that decides whether `/alert` is the live channel
   (just polled rarely) or whether we need a new endpoint.
2. **Patch `DtdObserver`** to listen on whatever the real live
   channel turns out to be (long-poll URL, popup-specific endpoint,
   or a `/v2/alerts/stream` style SSE we haven't seen yet).
3. **Add a watchdog** to the observer: if `last_event_age_seconds >
   90` while the process is alive, log a `WARN` and (optional)
   auto-restart the Playwright context. Prevents the silent-failure
   mode we hit twice today.
4. **Resume original pending list** (below) once the live-channel
   fix is in.

### Open follow-ups (pick up here)

Last session ended Tue 30 Jun ~23:20 Perth.
Servers stopped, repo pushed. **Auto-arm v1, 10s tick cadence,
typed gate failures, observer UI control all shipped.** Live-channel
mystery for the DTD observer is the immediate blocker (see "Tomorrow's
plan" above).

**Carry-overs (still open from previous sessions + this one):**

- **Capture qualified-contract metadata** — `qualifyContracts()`
  returns company name / primary exchange / secType but we don't
  persist these. User asked for this so the engine header shows
  "AIRJ — Air Industries Group (NASDAQ)" instead of just `AIRJ`,
  to confirm we're not accidentally on the wrong instrument.
  Estimated 1-2 hours.
- **DECISION NEEDED — relax microstructure thresholds.** Default
  `max_spread_bps=50`, `imbalance_floor=0.55`, `tape_buy_pct_floor=0.55`
  are too tight for wide-spread small-caps like AIRJ. Either lower
  the defaults or make them per-symbol overrideable from the Arm
  form. User-side decision pending. Estimated 0.5 day.
- **Item 3 — Scaled entry (3-rung LMT ladder + scale-in).** Ross
  scales in on confirmation, not all-in on the trigger. Replace the
  current single-LMT submission with a 3-rung ladder
  (entry, +N cents, +2N cents) and a scale-in path when structure
  holds. Estimated 1-2 days.
- **Item 4 — Verify the tick-level L2 monitor.** The 10s cadence
  already routes `partial` bars through the exit framework
  (including `l2_distress`), but we haven't end-to-end-verified
  that continuous-depth updates from `reqMktDepth` actually drive
  evaluation at every snapshot, not just at 10s ticks. ~1 day to
  validate + fix if needed.

Earlier session entry below (kept for context).

---

### (Previous session) Open follow-ups — 26 Jun

Last session ended Fri 26 Jun ~22:50 Perth (Fri US RTH).
Servers stopped, repo pushed. **v1.3 multi-engine shipped + first
end-to-end paper trade executed** (ILLR run #25, +$2.80 in 66s). The
session surfaced an important design mismatch in the exit framework —
see the "next big slice" below.

Account routing: still using **original paper `DUM733674`** — once
the Trust funding cleared, entitlements propagated user-wide and
DUM733674 inherited them. The new paper `DUQ861843` (username
`flingwing007`) was created as a fallback but is not needed for
data access. Either account works.

History (kept for context):
| IBKR account | Subscriptions? | Linked paper? | Funded? |
|---|---|---|---|
| U21585867 Individual | None | `DUM733674` (primary) | N/A |
| U23755393 Trust | ✅ ~AUD 47/mo (NASDAQ L1+L2, NYSE A/B, Snapshot) | `DUQ861843` (username `flingwing007`, spare) | AUD 4,000 funded |

**MULTI-ENGINE SLICE — ✅ DONE (Fri 26 Jun).** `EngineRegistry`,
`PortfolioRiskGate`, multi-card dashboard, "Drop & replace", and
portfolio-level risk caps all shipped. See "Recently landed". The
five design questions (non-tradable semantics, mutex release timing,
mutex leak recovery, portfolio risk caps, UI layout) were resolved
in [`docs/multi_engine_design.md`](docs/multi_engine_design.md)
before coding.

**THE NEXT BIG SLICE — Ross-style dynamic exit framework redesign.**

Surfaced by the ILLR run #25 trade. The exit closed on a fixed 2R
take-profit (`SECOND_TARGET`), not on any L2/tape signal — MACD was
still strongly positive at exit. **Ross does NOT set price targets.**
He reads the order book and tape in real time to find resistance and
sell into it. Our current `exits.py` has the wrong primitives at the
top of the priority chain (fixed-R targets fire before any dynamic
trigger gets a chance).

What we have today (`backend/src/day_trade/engine/exits.py`):

| # | Trigger | Type | Issue |
|---|---|---|---|
| 1 | `HARD_STOP` | Price | OK — keep |
| 2 | `SECOND_TARGET` | Fixed 2R | **Wrong primitive — fires before dynamic exits** |
| 3 | `FIRST_TARGET` | Fixed 1R partial | **Wrong primitive — same** |
| 4 | `MACD_FLIP` | Indicator | OK as one of many exits |
| 5 | `VWAP_LOSS` | Indicator | OK |
| 6 | `L2_DISTRESS` | L2 | Defensive only — fires when bid imbalance or wall is bad |
| 7 | `TAPE_FLIP` | Tape | Defensive only — fires when buys < 40% |
| 8 | `TIME_STOP` | Time | OK |

What's missing:

- **L2 PROFIT-TAKING exit** (new) — detect ask wall forming /
  thickening in front of price *while we're in profit*. Different
  from L2_DISTRESS (which is about the move actively failing). Look
  for resting size growing within ~20-30 bps of mid that wasn't
  there N bars ago, AND we're +N cents above entry.
- **"First red sell" rule** (new) — Ross's classic: once we're in
  profit (e.g. +0.5R), exit on the close of the first red 1m bar.
  Locks in trend trades cleanly without staring at L2.
- **Structural stop, not %-based** — `_handle_enter` falls back to
  `entry * 0.99` for legacy strategies (incl. macd_crossover_long
  which is the test bench). Should use last swing low / VWAP / next
  visible bid instead. The 1% fallback is what set R=5c on ILLR,
  making 2R = 10c — far too tight for any real momentum read.

Open design questions to resolve BEFORE coding:

1. **Disable 1R/2R outright, or keep them off-by-default and let
   strategies opt in?** Recommendation: off by default, opt-in via
   `ExitConfig.enable_first_target=False, enable_second_target=False`.
   The data structures stay (cheap), but the default profile becomes
   Ross-style.
2. **Where does the L2-profit-take live — `exits.py` or a new
   `exits_dynamic.py`?** Lean toward extending `exits.py` to keep
   first-wins arbitration in one place.
3. **What constitutes "ask wall forming"?** Need to define
   quantitatively — current `ask_wall_size_multiple=5.0` and
   `ask_wall_distance_bps=20.0` are tuned for distress, not
   absorption. Likely need rolling N-bar baseline of ask size at
   wall-level to detect *growth*.
4. **Structural stop for legacy strategies.** Either (a) every
   strategy must implement `suggest_stop_price` (clean but breaks
   `macd_crossover_long` until updated), or (b) the engine computes
   a structural stop from recent bars (last swing low) when the
   strategy doesn't provide one.
5. **How do we test L2-driven exits in CI?** No live IBKR depth in
   tests. Need a `FakeOrderBook` fixture that can be scripted to
   produce wall-formation / wall-absorption / wall-pull sequences.

Scope estimate: **2-3 days of focused work**, plus 1 day on a
structural-stop refactor if we go that route.

**Other follow-ups (still open, lower priority than the exit redesign):**

- **Phase 2 — DTD scanner auto-feed.** Revive the on-hold Playwright
  DTD observer, route scanner alerts to auto-populate free engine
  slots, define slot-eviction policy (stale by how many bars without
  momentum?), respect manual-drop precedence. ~2-3 days.
- **Snapshot-on-arm evaluation.** When an engine is armed mid-move,
  immediately replay the current partial bar's state and ask "would
  this fire if I treated the partial as closed?" — gives a chance to
  catch a move already in progress instead of waiting for the next
  bar close. ~1 day. Becomes more important once DTD auto-feed lands
  (alerts arrive mid-bar, and the next bar close could be 50 seconds
  away).
- **Plan the L2/T&S feature layer (Bookmap-style)** — partially
  unblocked by the exit redesign above (the L2 profit-take primitive
  will exercise much of this). The engine has scaffolded
  `orderbook.py` + `features.py`; we need to validate against Ross's
  actual decision-making patterns before writing code. Plan first;
  code after.
- **Manual force-entry button** ("Buy Now") for cases where the user
  has personal conviction on a setup that the engine's trigger
  hasn't fired on (e.g. user joining a hot mover late). Submits the
  configured LMT@ask+offset order with all risk caps applied; engine
  then manages the position with normal exit triggers.
- **10-second chart visualization** — aggregate IBKR 5s real-time
  bars into 10s candles for fast tape-reading-style chart view.
  UX upgrade only; strategy decisions still 1m bar-close driven.
- **Live forming candle not visibly updating in browser** despite
  backend code in place (`9083618`). Backend publishes
  `engine.bar_tick` every 5s; frontend doesn't appear to redraw.
  Diagnose:
  - DevTools → Network → WS frames; filter `engine.bar_tick`. If
    arriving: bug in `EngineChart.tsx` `bar_tick` branch or in
    `ENGINE_TOPICS` filter on `engine/page.tsx`.
  - If not arriving: bug backend-side; verify
    `BarFeed._on_partial_bar` is firing and
    `TradingEngine._on_partial_bar` is publishing.
- **`test_bars.py` unit test** for the minute-aggregation + the
  `on_partial_bar` callback. We don't currently exercise `BarFeed`
  in tests.
- **Verify orphan sweep ran clean** on next backend startup; grep
  log for `swept N orphaned engine_run row(s)`.

**Code-side: no .env changes needed.** Host/port/client_id/trading
mode all stay the same. `IBKR_TARGET_ACCOUNT` is intentionally
blank — the paper login exposes one DU* account
(`DUM733674` currently) and the engine picks that up automatically.

### Earlier fix worth knowing about

`backend/src/day_trade/db/session.py` was missing
`@asynccontextmanager`, which meant **every DB-writing endpoint —
including `POST /engine/start` — returned 500**. The browser surfaced
this as "Failed to fetch" because the error response was missing CORS
headers on the cross-origin call. Fixed in commit `9d468bd`. Before
that commit, no engine run had ever actually started in this codebase —
the 68 unit tests don't exercise the live `session_scope` → DB path,
which is why CI was green.

### Known limitations / not yet built

Captured in [`docs/v1_1_semi_auto_spec.md`](docs/v1_1_semi_auto_spec.md)
and the `parked:` section of
[`strategy_sources/strategy_rules.yaml`](strategy_sources/strategy_rules.yaml):

- **Multi-leg starter** ("starter then scale up then manage out" Ross
  pattern) — engine currently does single-shot entries.
- **Psychological-level proximity gate** (half-dollar / whole-dollar /
  PMH / PDH magnets) — scaffolded in YAML, not enforced by the engine.
- **L2-aware stop** ("next visible bid is the stop") — stop modes today
  are `pullback_low` and `fixed_pct`.
- **Consecutive-loss counter** — tracked-only, not yet a circuit breaker.
- **5-minute setups + 5m 9-EMA hold** — explicitly parked.
- **LLM reasoner** — the layered decision pipeline in `ross_notes.md`
  envisages an LLM that can VETO or SIZE-DOWN (never upgrade) based on
  `principles.md` + `scenarios.yaml`. The knowledge base exists, the
  reasoner does not.

## Where the design lives

- [`docs/v1_1_semi_auto_spec.md`](docs/v1_1_semi_auto_spec.md) — full
  engine spec: run lifecycle, entry gate stack (5m MACD context, VWAP,
  backside, 1m MACD context, entry trigger), exit triggers (8
  independent, first-wins arbitration), order routing.
- [`docs/monday_smoke_test.md`](docs/monday_smoke_test.md) —
  step-by-step test plan for the next live session, with a printable
  checklist at the bottom.
- [`docs/data_feeds_moomoo_vs_ibkr.md`](docs/data_feeds_moomoo_vs_ibkr.md)
  — research note on Moomoo OpenAPI vs IBKR for L2/T&S.
- [`strategy_sources/ross_notes.md`](strategy_sources/ross_notes.md) —
  Ross methodology source of truth + the 4-layer decision pipeline
  (deterministic gates → soft score → principles/scenarios → optional
  LLM reasoner).
- [`strategy_sources/strategy_rules.yaml`](strategy_sources/strategy_rules.yaml)
  (`v0.2.0`) — deterministic rules + scaffolded sections for
  `psychological_levels`, `entry_legs`, `l2_guidelines`, `ts_guidelines`,
  `parked` items.
- [`strategy_sources/principles.md`](strategy_sources/principles.md) —
  17 stable `PRINCIPLE_ID`s with `status` + `source`. The narrative
  Ross heuristics that don't fit into the rigid YAML.
- [`strategy_sources/scenarios.yaml`](strategy_sources/scenarios.yaml) —
  16 Warrior course Q&A scenarios as structured stimulus → response
  records.
- [`strategy_sources/assumptions_register.md`](strategy_sources/assumptions_register.md)
  — status row per rule (`placeholder` / `assumption` /
  `needs_validation` / `validated` / `tracked_only` / `parked`).

**Important**: the engine does NOT read `strategy_sources/*` at runtime
today. They're the source of truth for future layers and for documenting
intent; deterministic rules in there mirror what's hard-coded in the
strategy modules.

## Prerequisites

- macOS, Python 3.12+, Node 22+, Docker Desktop.
- `uv` installed (`brew install uv`).
- An **existing local Postgres container** running on `localhost:5434`
  with user `postgres`/`postgres` (this repo uses your `prism-local-db`
  container; the `daytrade` database is created inside it).
- IBKR TWS or IB Gateway running locally on `localhost:7497` (paper).
  Required for any engine run.
- Day Trade Dash account (only if you want to play with the on-hold
  DTD ingestion path).

## Setup

```bash
cd day-trade-system

cp .env.example .env
# Defaults assume postgres at localhost:5434 user=postgres pw=postgres
# and IBKR paper at 127.0.0.1:7497. Adjust if needed.

cd backend && uv sync --extra dev && uv pip install -e . && cd ..

docker start prism-local-db
PGPASSWORD=postgres psql -h localhost -p 5434 -U postgres -c "CREATE DATABASE daytrade;" || true

cd backend && uv run alembic upgrade head

uv run python ../scripts/init_db.py
cd ..

# Only if you plan to use DTD ingestion (on hold):
cd backend && uv run playwright install chromium && cd ..

cd frontend && npm install && cd ..
```

## Running

```bash
# Terminal 1 - backend API + WebSocket on :8000
cd backend && uv run uvicorn day_trade.app:app --host 127.0.0.1 --port 8000 --reload

# Terminal 2 - frontend dashboard on :3000
cd frontend && npm run dev -- --port 3000
```

Open <http://localhost:3000/engine>. Make sure TWS paper is running and
logged in before clicking **Arm engine**.

DTD ingestion (on hold — only if you want to revive it):

```bash
# Terminal 3 - one-time interactive DTD login; cookies persist
cd backend && uv run python ../scripts/dtd_login.py

# Terminal 4 - long-running headless DTD observer
cd backend && uv run python ../scripts/dtd_run.py
```

## Developing with the market closed

Two options:

1. **Forex** — `EUR.USD` on IDEALPRO is open ~24/5 and exercises the
   full engine path (bars, MACD, VWAP, executor, journal). Best Sunday
   smoke. See [`docs/monday_smoke_test.md`](docs/monday_smoke_test.md)
   Phase 1.
2. **DTD fixture replay** — replays a captured DTD response through the
   ingestion pipeline so `/candidates` behaves as if the scanner is
   live. Doesn't touch the engine.

```bash
cd backend && uv run python ../scripts/replay_fixture.py --fast
uv run python ../scripts/replay_fixture.py --speed 60   # 1 minute -> 1 second
uv run python ../scripts/replay_fixture.py --speed 1    # real-time
```

To start fresh between runs:

```bash
PGPASSWORD=postgres psql -h localhost -p 5434 -U postgres -d daytrade \
  -c "TRUNCATE candidates, scanner_events, news, filter_evaluations, symbols, trade_plans, orders, fills, engine_runs, engine_events, bar_aggregates RESTART IDENTITY CASCADE;"
cd backend && uv run python ../scripts/init_db.py
```

## Testing

```bash
cd backend && uv run pytest -q          # 68 tests
cd backend && uv run ruff check src tests
```

Note: tests do **not** exercise the live `session_scope` → DB path or
the live IBKR path. The Monday smoke (`docs/monday_smoke_test.md`) is
the integration test.

Quick standalone IBKR sanity check (no engine, just qualify-and-quote):

```bash
cd backend && uv run python ../scripts/ibkr_check.py
cd backend && uv run python ../scripts/ibkr_check.py UPC SKYQ --only   # probe specific symbols only
```

L2 (market depth) + T&S (tick-by-tick) API smoke test. Confirms that
`reqMktDepth` + `reqTickByTickData` are flowing — the data surfaces our
Bookmap-style feature layer consumes:

```bash
cd backend && uv run python ../scripts/ibkr_l2_check.py FRTT 20
```

Direct paper-trade plumbing test (places a real LMT BUY+SELL on the paper
account, no engine involvement, validates IBKR order submission and
fills end-to-end). Use with a small qty during pre-market for liquid
NASDAQ tickers:

```bash
cd backend && uv run python ../scripts/manual_trade_test.py SKYQ 10
```

## Layout

```
backend/
  src/day_trade/
    config.py                 # pydantic-settings (.env -> Settings)
    app.py                    # FastAPI factory + lifespan + CORS
    api/                      # REST + WS routers
      engine.py               # POST /engine/start, /stop, /approve, /reject;
                              # GET /engine/status, /runs, /strategies, ...
      candidates.py           # DTD funnel (on hold)
      rules.py                # filter rules editor (on hold)
    db/
      models.py               # EngineRun, EngineEvent, BarAggregate, ...
      session.py              # async_sessionmaker + session_scope
      ...
    engine/                   # >>> the v1.3 trading engine <<<
      registry.py             # EngineRegistry — up to 4 concurrent engines
      portfolio_risk.py       # PortfolioRiskGate — execution mutex + daily caps
      engine.py               # bar loop, gate stack, exit arbitration
      strategies/
        base.py
        first_pullback_long.py
        macd_crossover_long.py   # legacy POC
      triggers.py             # detect_pullback_break, detect_macd_cross_up
      exits.py                # 8 exit triggers, first-wins
      backside.py             # don't-trade-the-backside gate (hybrid)
      executor.py             # marketable LMT orders, sell_anchor, cancel-on-timeout
      features.py             # L2 + T&S derived features
      orderbook.py            # 10-level book + T&S buffer
      ibkr_client.py          # ib-async wrapper: connect, qualify,
                              # subscribe_realtime_bars, reqMktDepth, reqTickByTick
      instruments.py          # ticker -> Stock(SMART,USD) | Forex(IDEALPRO)
      vwap.py                 # session-anchored VWAP
      multitf.py              # 1m -> 5m bar aggregation
      risk.py                 # daily / per-run caps
      journal.py              # event journaling + broker publish
    filters/                  # Stage-1 funnel rules (on hold)
    ingest/dtd/               # Playwright DTD observer (on hold)
    normalize/                # candidate rollup (on hold)
    ws/                       # in-process pub/sub broker
  alembic/                    # migrations
  tests/                      # 68 unit tests
frontend/
  src/
    app/
      engine/page.tsx         # >>> the primary surface <<<
      inspect/page.tsx        # generic TradingView inspector (incl. ASX)
      candidates/             # DTD funnel UI (on hold)
      rules/                  # filter rules editor (on hold)
      rejected/               # DTD rejected feed (on hold)
    components/
      EngineChart.tsx         # entry/exit overlay on lightweight-charts
      ...
    lib/                      # api client, ws hook, types
docs/
  v1_1_semi_auto_spec.md      # engine spec
  monday_smoke_test.md        # next live-session test plan
  data_feeds_moomoo_vs_ibkr.md
strategy_sources/             # knowledge base (doc-only today)
  ross_notes.md
  strategy_rules.yaml         # v0.2.0
  principles.md
  scenarios.yaml
  assumptions_register.md
scripts/
  ibkr_check.py               # standalone IBKR sanity check (L1 quote + bars)
  ibkr_l2_check.py            # L2 (reqMktDepth) + T&S (reqTickByTickData)
                              # API smoke; confirms Bookmap-style data layer
  manual_trade_test.py        # one-off direct paper BUY+SELL via ib-async
                              # (validates order plumbing without the engine)
  init_db.py                  # seed default DTD rule set
  replay_fixture.py           # DTD fixture replay
  dtd_login.py                # interactive DTD login (one-shot)
  dtd_run.py                  # headless DTD observer
.env.example                  # all config keys, no secrets
```

## Architecture decisions of note

### Engine

- **Up to 4 concurrent engines, single-position execution mutex**
  ([`engine/registry.py`](backend/src/day_trade/engine/registry.py) +
  [`engine/portfolio_risk.py`](backend/src/day_trade/engine/portfolio_risk.py)).
  Each engine runs independently (own BarFeed, indicators, gate
  stack, exits). When one engine acquires the mutex for an entry,
  the others continue evaluating gates but journal
  `entry_blocked_by_portfolio_mutex` instead of submitting orders.
  The mutex releases when the holder's position goes flat. Portfolio
  daily caps (`max_daily_loss_usd`, `max_total_trades_per_day`) trip
  a hard kill switch that resets at UTC midnight.
- **Configurable entry trigger** ([`engine/triggers.py`](backend/src/day_trade/engine/triggers.py)).
  `pullback_break` is the Ross-style structural pattern ("first 1m
  green candle whose high breaks the last red of a 1-3 bar pullback");
  `macd_cross` is the legacy indicator-only trigger.
- **Hybrid backside gate** ([`engine/backside.py`](backend/src/day_trade/engine/backside.py)).
  Hard vetoes for the worst conditions; soft score otherwise.
- **First-wins exit arbitration** ([`engine/exits.py`](backend/src/day_trade/engine/exits.py)).
  Hard stop, two targets, MACD flip, VWAP loss, L2 distress, tape flip,
  time stop. All evaluated in parallel; the first to fire wins.
- **Marketable LMT orders, not market orders**
  ([`engine/executor.py`](backend/src/day_trade/engine/executor.py)).
  BUY = `LMT @ ask + offset`. SELL with `sell_anchor=bid` (default,
  aggressive, mirrors Ross's "Sell at Bid" hotkey) = `LMT @ bid -
  offset`. SELL with `sell_anchor=ask` (passive) = `LMT @ ask - offset`.
  Cancel-on-timeout after `cancel_lmt_after_seconds`.
- **Auditable**: every gate evaluation, trigger evaluation, order
  submit, fill, exit reason, and config knob is journaled to
  `engine_events` and surfaced on `/engine` in the Live event log.

### Knowledge base layering

The system separates rigid rules from narrative ones:

1. **Deterministic gates (FLOOR)** — hard-coded in the strategy
   modules. Mirror the rigid sections of `strategy_rules.yaml`.
2. **Soft score** — currently only used by the backside gate.
3. **Principles + scenarios** — `principles.md` + `scenarios.yaml`
   capture Ross's heuristics with stable IDs. Not consumed by code yet.
4. **Optional LLM reasoner** — future. Can VETO or SIZE-DOWN, never
   UPGRADE. Reads principles + scenarios + the current market context.

### DTD funnel (on hold)

- **Path B (browser observation), not Path A (direct API)**: we attach
  a Playwright persistent-context browser to the DTD chatroom and
  parse the JSON the page receives. No requests we initiate ourselves.
- **Rules are data**: edit in the UI, persist as `filter_rule_sets` +
  `filter_rules` rows. Activating a new version is a new row, not an
  update.
- **Cooldown anchored at first alert**: a 10-min cooldown means "first
  alert ts + 10min", not "extended each time."
- **News piggybacks on DTD**: DTD's alert payload joins news server-side;
  we persist it once per `newsid`. No separate provider in v1.
