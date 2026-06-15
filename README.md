# day-trade

Local Mac-only AI-assisted day-momentum trading copilot for Interactive
Brokers paper accounts. Ross-Cameron-style first-pullback / MACD long
strategy on small-cap US equities, with forex as a 24-hour smoke-test rail.

> Personal research and paper-trading project. Not financial advice. Not a
> public product. **Live trading is hard-disabled** (`LIVE_TRADING_ENABLED=false`,
> `PAPER_TRADING_ONLY=true`); the system refuses to submit orders outside
> paper.

## Current state — June 15, 2026

**v1.2 semi-automated FirstPullback engine** is the active surface. Visit
`/engine`. You arm a single symbol (the human picks it — currently no
auto-promotion from DTD), the engine watches the gate stack, and submits
paper orders via IBKR TWS.

Verified end-to-end against `VSME` on paper account `DUM733674`: contract
qualified through SMART → NASDAQ, engine started, `engine_start` +
`ibkr_connected` events written, clean stop. EUR.USD on IDEALPRO is the
current overnight smoke rail and streams bars cleanly under the new
historical-bar bootstrap (MACD/VWAP warm up immediately, no 30-minute
wait).

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

- **Historical-bar bootstrap** (`183f5da`): on every Arm, the engine
  pulls 4 hours of 1m bars from IBKR and replays them through the
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
  ibkr_check.py               # standalone IBKR sanity check
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
