# day-trade

Local Mac-only AI-assisted day-momentum trading copilot for Interactive
Brokers paper accounts. Ross-Cameron-style first-pullback / MACD long
strategy on small-cap US equities, with forex as a 24-hour smoke-test rail.

> Personal research and paper-trading project. Not financial advice. Not a
> public product. **Live trading is hard-disabled** (`LIVE_TRADING_ENABLED=false`,
> `PAPER_TRADING_ONLY=true`); the system refuses to submit orders outside
> paper.

## Current state — June 24, 2026

**v1.2 semi-automated FirstPullback engine** is the active surface. Visit
`/engine`. You arm a single symbol (the human picks it — currently no
auto-promotion from DTD), the engine watches the gate stack, and submits
paper orders via IBKR TWS.

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

### Open follow-ups (pick up here)

Last session ended Wed 24 Jun ~21:20 Perth (Wed pre-market US close-out).
Servers stopped, repo pushed. **The IBKR data block from Mon 22 Jun
is fully resolved** and the engine has now been validated end-to-end
on two live US small-caps (LHSW, FRTT) with the gate stack correctly
refusing both on the backside veto.

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

**THE NEXT BIG SLICE — Multi-engine concurrent monitoring with
execution mutex.**

Ross typically has 3-5 movers alerting on his scanners simultaneously,
and the current "one engine at a time" runner singleton forces the
user to manually rotate between candidates — which costs attention
and risks missing the actual setup. The user's stated requirement
(Wed 24 Jun PM):

> "I want to monitor up to 4 stocks at once. When one triggers and
>  I take the trade, the others become non-tradable until the
>  position is exited."

This is **multi-watch, mutex-execute** semantics. Sketch:

```
EngineRegistry (replaces EngineRunner singleton)
  dict[symbol, TradingEngine]
  start(symbol, ...) -> add engine, no global lock
  stop(symbol) -> stop specific engine
  active() -> list of all running engines
       |
       v
TradingEngine x N (one per symbol, fully independent)
  - own BarFeed, indicators, gate stack, exits
  - subscribes to its own L2/T&S/quote
       |
       v on entry signal
PortfolioRiskGate (NEW)
  - try_acquire_for_entry(run_id) -> bool, atomic
  - release(run_id)
  - holds the "any engine in a position?" mutex
  - blocks other engines' entries while one holds a position
  - releases on position-flat (any exit reason)
```

When the mutex blocks an entry, the engine still journals
`blocked_by_portfolio_mutex` with the gate state — so the user has
a paper trail of "this would have fired too". Cross-engine risk
caps (e.g. portfolio-wide `max_daily_loss_usd`) consult the same
shared state.

Scope estimate: **3-4 days of focused work.**

| Layer | Change | Effort |
|---|---|---|
| `engine/runner.py` | `EngineRunner` → `EngineRegistry`; methods take a `symbol` parameter; remove the `EngineBusyError` constraint | ~0.5 day |
| NEW `engine/portfolio_risk.py` | `PortfolioRiskGate` with asyncio.Lock-serialised try-acquire/release; mutex leakage recovery via IBKR position reconciliation | ~0.5 day |
| `engine/engine.py` | Gate every order submission via `PortfolioRiskGate.try_acquire`; release on position-flat; audit blocked attempts | ~0.5 day |
| `engine/ibkr_client.py` | No change — already supports multiple concurrent subscriptions on one connection. Keep single `clientId` for now (revisit if we ever need per-engine isolation) | 0 |
| `api/engine.py` | `/engine/start` allows multiple; `/engine/stop?symbol=X`; `/engine/status` returns `{engines: [...]}` ; new `/engine/portfolio` for mutex state | ~0.5 day |
| `frontend/src/app/engine/page.tsx` | Multi-card dashboard; "+ Add Engine" button; visual lockout indicator when mutex is held by another engine | ~1-1.5 days |
| Testing + audit | Mutex acquire/release lifecycle across partial fills, cancellations, distress exits; orphan-mutex recovery on backend restart | ~0.5 day |

Key open design questions to resolve BEFORE coding:

1. **What is "non-tradable" exactly?** Suggested: engine still
   evaluates gates and emits `entry_signal` events to the audit log,
   but on the order-submission step it sees the mutex held and
   journals `blocked_by_portfolio_mutex` without submitting. Continues
   monitoring normally. Alternative: full freeze (no gate evaluation
   either) — saves CPU but loses observability.
2. **Mutex release timing.** Release on `position.qty == 0` after a
   confirmed fill of the exit order? Or on the exit-trigger firing
   (before the exit-order fills)? The former is safer (no double-
   entry risk). The latter is faster but needs careful handling of
   partial-fill scenarios.
3. **Mutex leak recovery.** If the engine that held the mutex
   crashes mid-trade, the mutex stays held. On `EngineRegistry`
   restart, query IBKR for actual account positions and reconcile —
   if no open positions exist, release the mutex; if positions
   exist, identify which engine owns them and re-attach.
4. **Per-engine vs portfolio risk caps.** Today `max_daily_loss_usd`
   is per-engine. With multiple engines, almost certainly we want
   **portfolio-level** — total daily loss across all engines combined.
   Same for `max_position_value_usd` and trade-count caps. New
   `PortfolioRiskCaps` config, separate from per-engine `RiskCaps`.
5. **UI layout.** Vertical stack of 4 cards? Grid? Collapsible
   cards? My instinct: 2x2 grid with expand-to-inspect modal.

**Interim option (~2 hours) being considered**: a "Swap Symbol"
button on the single-engine dashboard that atomically stops the
current engine and arms a new one in a single click. Lower
complexity, lets the user rotate through Ross-scanner candidates
faster. NOT multi-engine — still one at a time. Decide tomorrow.

**Other follow-ups (still open, lower priority than multi-engine):**

- **Plan the L2/T&S feature layer (Bookmap-style)** — the infrastructure
  is open, the engine has scaffolded `orderbook.py` + `features.py`,
  and we need to validate against Ross's actual decision-making
  patterns before writing code. Plan first; code after. The smart-
  aggregated depth (IEX + top-of-book from ~14 exchanges) we get
  via `isSmartDepth=True` is sufficient for resting wall, aggressor
  imbalance, absorption, and sweep detection.
- **Manual force-entry button** ("Buy Now") for cases where the user
  has personal conviction on a setup that the engine's trigger
  hasn't fired on (e.g. user joining a hot mover late). Submits the
  configured LMT@ask+offset order with all risk caps applied; engine
  then manages the position with normal exit triggers. Discussed
  Wed 24 Jun PM, parked for post-multi-engine.
- **10-second chart visualization** — aggregate IBKR 5s real-time
  bars into 10s candles for fast tape-reading-style chart view.
  UX upgrade only; strategy decisions still 1m bar-close driven.
  Parked for post-multi-engine.
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
    engine/                   # >>> the v1.2 trading engine <<<
      runner.py               # process-wide singleton; one active engine
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

- **Single active engine, process-wide** ([`engine/runner.py`](backend/src/day_trade/engine/runner.py)).
  Arming a new symbol while another is running returns 409. Auto-rearms
  after each exit until you Stop.
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
