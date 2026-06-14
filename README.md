# day-trade

Local Mac-only AI-assisted day-momentum trading copilot. Built on top of [Day Trade Dash](https://chatroom.warriortrading.com/) (Warrior Trading) for discovery and IBKR for paper execution.

> Personal research and paper-trading project. Not financial advice. Not a public product.

## Mental model

The system is a **funnel**, not a trader:

```
alert (DTD) -> per-symbol cooldown -> Stage 1 filter rules -> watchlist -> (Stage 2 setup) -> paper trade -> (Stage 3 exit)
```

Alerts are **not** trade triggers. They are momentum candidates. The system culls them with configurable rules so we focus on the small subset that matches Ross-style setups.

See `.cursor/plans/day-trade-system-v1*.plan.md` (or wherever your plan lives) for the full design.

## What's in v1

- **DTD ingestion** via a Playwright persistent-context browser session (Path B - no direct API calls).
- **Per-symbol candidate dedupe** with configurable cooldown (default 10 min).
- **Stage-1 filter rules** (data, not code): price range, max float, min RVOL today + 5m, min % gain, news freshness. Editable from the dashboard.
- **Postgres-backed** scanner events, candidates, news, filter evaluations.
- **FastAPI** REST + WebSocket.
- **Next.js dashboard**: live Watchlist (passed), Rejected feed (failed with reason chips), Candidate detail with embedded TradingView chart + filter evaluation breakdown, Rules editor.
- **Fixture replay** so you can dev with the US market closed.

## What's not in v1

- No own scanner against a paid real-time feed - DTD is the source.
- No autonomous trading. v1.5 adds IBKR paper with manual submit. Auto-execute waits for Stage 2 entry detection in v2.
- No backtest engine, no L2, no ASX yet (interfaces present, implementations later).

## Prerequisites

- macOS, Python 3.12+, Node 22+, Docker Desktop.
- `uv` installed (`brew install uv`).
- An **existing local Postgres container** running on `localhost:5434` with user `postgres`/`postgres` (this repo uses your `prism-local-db` container; the `daytrade` database is created inside it).
- Day Trade Dash account (you need to be able to log in via chrome interactively once).
- IBKR account with TWS or IB Gateway running locally on `localhost:7497` (paper). Required for v1.5 only.

## Setup

```bash
# 1. clone and enter
cd day-trade-system

# 2. config
cp .env.example .env
# .env defaults assume postgres at localhost:5434 user=postgres pw=postgres. Adjust if needed.

# 3. backend deps
cd backend && uv sync --extra dev && uv pip install -e . && cd ..

# 4. make sure your postgres container is running, then create the DB if needed
docker start prism-local-db
PGPASSWORD=postgres psql -h localhost -p 5434 -U postgres -c "CREATE DATABASE daytrade;" || true

# 5. apply schema
cd backend && uv run alembic upgrade head

# 6. seed the default Stage-1 rule set
uv run python ../scripts/init_db.py
cd ..

# 7. install Playwright chromium (one-shot)
cd backend && uv run playwright install chromium && cd ..

# 8. frontend deps
cd frontend && npm install && cd ..
```

## Running

```bash
# Terminal 1 - backend API + WebSocket on :8000
cd backend && uv run uvicorn day_trade.app:app --host 127.0.0.1 --port 8000 --reload

# Terminal 2 - frontend dashboard on :3000
cd frontend && npm run dev -- --port 3000

# Terminal 3 - (one-time) log into DTD interactively; cookies persist
cd backend && uv run python ../scripts/dtd_login.py

# Terminal 4 - (when market is live) start the DTD observer
cd backend && uv run python ../scripts/dtd_run.py
```

Open <http://localhost:3000>.

## Developing with the market closed

`scripts/replay_fixture.py` replays a captured DTD response through the live pipeline so the dashboard behaves as if the market is open:

```bash
# Fast - dumps a full day in seconds (good for backfill)
cd backend && uv run python ../scripts/replay_fixture.py --fast

# Timed - paces events at the original cadence x speed multiplier
uv run python ../scripts/replay_fixture.py --speed 60   # 1 minute -> 1 second
uv run python ../scripts/replay_fixture.py --speed 1    # real-time
```

To start fresh between runs:

```bash
PGPASSWORD=postgres psql -h localhost -p 5434 -U postgres -d daytrade \
  -c "TRUNCATE candidates, scanner_events, news, filter_evaluations, symbols, trade_plans, orders, fills RESTART IDENTITY CASCADE;"
cd backend && uv run python ../scripts/init_db.py
```

## Testing

```bash
cd backend && uv run pytest -q          # 18 unit tests at the time of writing
cd backend && uv run ruff check src tests
```

## Layout

```
backend/
  src/day_trade/
    config.py                 # pydantic-settings
    app.py                    # FastAPI factory
    api/                      # REST + WS
    db/                       # SQLAlchemy models, session, repositories
    filters/                  # Stage-1 rules engine + defaults
    ingest/dtd/               # Playwright browser + observer + parser + replay
    normalize/                # ScannerEvent, candidate rollup with cooldown
    ws/                       # in-process pub/sub broker
  alembic/                    # migrations
  tests/
frontend/
  src/
    app/                      # Next.js App Router pages
    components/               # CandidateRow, RejectedRow, TradingViewChart, StatusBar
    lib/                      # api client, ws hook, types, formatters
scripts/
  init_db.py                  # seed default rule set
  replay_fixture.py           # replay captured DTD response through pipeline
  dtd_login.py                # one-shot interactive DTD login (headed Chromium)
  dtd_run.py                  # long-running headless DTD observer
.env.example                  # all config keys, no secrets
```

## Architecture decisions of note

- **Path B (browser observation), not Path A (direct API)**: we attach a Playwright persistent-context browser to the DTD chatroom; it polls DTD's own JSON endpoints just as a normal logged-in user would. We attach a response listener and parse the same JSON the page receives. No requests we initiate ourselves, no risk of being flagged for automated access.
- **Rules are data**: edit them in the UI, they persist as `filter_rule_sets` + `filter_rules` rows. Activating a new version is a new row, not an update, so we can attribute outcomes to specific rule versions later.
- **Cooldown anchored at first alert**: a 10-minute cooldown means "first alert ts + 10min", not "extended each time a new alert fires within the window." Keeps candidate boundaries predictable.
- **Soft vs hard rules**: hard rules kill candidates (`failed_filter`). Soft rules log a fail but don't kill. Default `require_news_within` is soft - Ross prefers but doesn't require fresh news.
- **News piggybacks on DTD**: the DTD alert payload includes a server-side joined news object. We persist it once per `newsid`. No separate news provider needed in v1.
- **TradingView free embedded widget**: no separate data feed required for charts. NASDAQ symbol prefix is hard-coded; symbol change in widget is allowed.
