# Multi-Engine Design — Phase 1

Authoritative design for the multi-engine refactor. Captures the decisions
made Fri 26 Jun PM so we don't drift during implementation.

## TL;DR

We're moving from "one `TradingEngine` at a time, enforced via
`EngineBusyError`" to "up to 4 independent `TradingEngine`s monitoring
different symbols concurrently, with a shared `PortfolioRiskGate` that
enforces 'only one position open at a time' across all of them".

End-state goal (Phase 3): scanner alerts auto-populate engine slots, the
engine evaluates each, and autonomously fires the first clean entry that
the gate stack accepts. Phase 1 builds the multi-engine substrate with
manual ticker entry; Phase 2 wires DTD; Phase 3 adds auto-cold-drop +
calibration UI.

Hard invariant: **at most ONE open position across the entire registry at
any time.** Paper trading still cares about clean accounting.

## Why this exists

Ross typically has 3-5 names alerting on his DTD scanners simultaneously.
The current single-engine constraint forces the user to manually rotate
between candidates — which loses attention and risks missing the actual
setup. The user has tried recreating Ross's scanners on IBKR and the
results are nowhere close, so DTD is the strategic data source. Multi-
engine is the prerequisite to that workflow.

## Scope

### In scope (Phase 1)

- Run up to N=4 `TradingEngine` instances concurrently on one process,
  each on its own symbol, each fully owning its own bars / indicators /
  gates / exits / journal.
- New `EngineRegistry` replaces the `EngineRunner` singleton.
- New `PortfolioRiskGate` enforces single-position-at-a-time across all
  engines (the execution mutex).
- API multi-tenancy: `/engine/start` accepts repeated calls; `/engine/stop`
  takes a symbol; `/engine/status` returns a list.
- Frontend dashboard: sidebar list of engines + main inspection panel.
- Manual `Drop and Replace` action per engine card.
- Portfolio-level risk caps (daily loss, concurrent engines, total
  trades-per-day).
- All blocked-by-mutex entries journaled as "would-have-fired" events for
  calibration.

### Out of scope (Phase 1)

- DTD ingestion revival (Phase 2).
- Auto-fill engine slots from scanner alerts (Phase 2).
- Auto-drop cold engines (Phase 3).
- Trade journal review UI / aggregate analytics (Phase 3).
- Multi-account or per-engine separate `clientId` isolation (deferred —
  one shared IBKR connection is fine for our scale).

## Architecture

### High-level component diagram

```
              ┌────────────────────────────────────────────────┐
              │  EngineRegistry  (singleton, replaces Runner)  │
              │                                                │
              │   _engines: dict[str (symbol), TradingEngine]  │
              │   _risk:    PortfolioRiskGate                  │
              │                                                │
              │   start(symbol, ...) -> int (run_id)           │
              │   stop(symbol) -> bool                         │
              │   stop_all() -> int                            │
              │   active() -> list[EngineSnapshot]             │
              │   approve(run_id) -> bool                      │
              │   reject(run_id) -> bool                       │
              └──────┬────────────┬────────────┬───────────────┘
                     │            │            │
              ┌──────┴───┐  ┌─────┴────┐  ┌────┴─────┐
              │ Engine A │  │ Engine B │  │ Engine C │   ... up to 4
              │ (SKYQ)   │  │ (FRTT)   │  │ (LHSW)   │
              │          │  │          │  │          │
              │ own:     │  │ own:     │  │ own:     │
              │  bars,   │  │  bars,   │  │  bars,   │
              │  strat,  │  │  strat,  │  │  strat,  │
              │  gates,  │  │  gates,  │  │  gates,  │
              │  exits,  │  │  exits,  │  │  exits,  │
              │  journal │  │  journal │  │  journal │
              └────┬─────┘  └────┬─────┘  └────┬─────┘
                   │             │             │
              all consult before submitting any order:
                   │             │             │
                   └─────────────┼─────────────┘
                                 ▼
                  ┌────────────────────────────────┐
                  │      PortfolioRiskGate         │
                  │                                │
                  │   _mutex: asyncio.Lock         │
                  │   _holder: str | None          │
                  │   _portfolio_caps: ...         │
                  │   _daily_state: ...            │
                  │                                │
                  │   try_acquire_for_entry(       │
                  │       symbol, intended_qty)    │
                  │       -> AcquireResult         │
                  │                                │
                  │   release(symbol, fill_pnl)    │
                  │                                │
                  │   is_holding() -> bool         │
                  │   holder() -> str | None       │
                  └────────────────────────────────┘

  Shared dependencies (one each, shared across all engines):
    IBKRClient (one TWS connection, one clientId, multi-symbol subs)
    MessageBroker (WebSocket pub/sub, namespaced by run_id)
    DB session pool
    Settings
```

### File-by-file change list

| File | Change |
|------|--------|
| `backend/src/day_trade/engine/runner.py` | Rename `EngineRunner` → `EngineRegistry`. Replace `_engine: TradingEngine \| None` with `_engines: dict[str, TradingEngine]`. Lifecycle methods take `symbol`. Remove `EngineBusyError`; add `EngineSlotFullError` (raised when N engines already active). Add `_risk: PortfolioRiskGate`. |
| `backend/src/day_trade/engine/portfolio_risk.py` (NEW) | `PortfolioRiskGate` class. Holds the execution mutex + portfolio-level caps + daily-aggregate state. asyncio.Lock-serialised `try_acquire_for_entry` returning `AcquireResult(granted: bool, reason: str)`. `release` called on confirmed position-flat. |
| `backend/src/day_trade/engine/engine.py` | In `_handle_entry_signal`, before submitting any order, call `registry.portfolio_risk.try_acquire_for_entry(self.config.symbol, intended_qty)`. If denied, journal `entry_blocked_by_portfolio_mutex` with full gate state and continue monitoring. After exit fills to position-flat, call `release` with realized P&L. |
| `backend/src/day_trade/engine/ibkr_client.py` | No structural change. Already multi-symbol-safe — each engine's subscriptions are tracked by ticker handle. |
| `backend/src/day_trade/api/engine.py` | `POST /engine/start` allows multiple calls (rejected only when slot full). `POST /engine/stop` takes `?symbol=X`. New `POST /engine/stop_all`. `GET /engine/status` returns `{"engines": [...], "portfolio": {...}}` instead of single-engine fields. `POST /engine/approve` and `/reject` take `?run_id=X`. New `GET /engine/portfolio` returns the mutex state. |
| `backend/src/day_trade/db/models.py` | No schema change for Phase 1. The `engine_run` row keeps its existing fields per-engine. (Portfolio-level state lives in memory; Phase 3 may persist.) |
| `frontend/src/app/engine/page.tsx` | Major refactor: sidebar list + main panel layout. Each sidebar entry = one engine summary. Main panel = the selected engine's full detail (existing single-engine view, mostly preserved). New "Add Engine" form. "Drop and Replace" per engine. Visual lock indicator when portfolio mutex is held elsewhere. |
| `frontend/src/lib/types.ts` | New `EngineStatusList`, `PortfolioStatus`. Mark existing single-engine `EngineStatus` legacy. |
| `frontend/src/lib/api.ts` | New client methods: `startEngine`, `stopEngine(symbol)`, `getEngines()`, `getPortfolio()`. |
| `backend/tests/` | New `test_portfolio_risk.py` for the mutex (acquire-release cycles, concurrent acquire serialisation, deny on cap breach, leak recovery). |

## Locked-in decisions

These are the answers to the 5 open questions surfaced in `README.md`'s
"NEXT BIG SLICE" section, captured Fri 26 Jun PM.

### Decision 1 — Mutex release timing: **on confirmed position-flat**

The mutex is released only after the exit order fills and the engine
observes `position.qty == 0` from IBKR's position update.

Rationale: paper trading cares about clean accounting. Releasing on
exit-trigger-fire (before fill) creates a race window where two engines
could both submit entries; this is unacceptable even on paper. The
few-seconds delay is irrelevant for our timeframe.

Edge case: if the exit order fails to fill (limit not hit, cancelled),
the engine is still in position and the mutex stays held. That's correct
behaviour — we still hold one position. The exit trigger framework will
re-evaluate on the next bar.

### Decision 2 — Non-tradable semantics: **"would-have-fired" audit**

When the mutex is held by Engine A, Engines B/C/D continue running
normally — they evaluate bars, update indicators, run gate stacks, and
emit entry signals to the journal. The ONLY difference is at the
order-submit step: they check `try_acquire_for_entry`, see it denied,
and log an `entry_blocked_by_portfolio_mutex` event with the full gate
state and intended order details. They do NOT submit the order.

Rationale: this is essential for calibration. The whole point of running
multiple engines is to understand the alternatives — "did Engine B see a
better setup at 10:32 while Engine A was holding a losing FRTT trade?"
Without audit logs of would-have-fires, that question is unanswerable.
CPU cost is negligible at 4 engines.

### Decision 3 — Portfolio-level risk caps

| Cap | Scope | Phase 1 default | Notes |
|-----|-------|------------------|-------|
| `quantity` | per-engine (per-trade) | from arm form | unchanged |
| `max_position_value_usd` | per-engine (per-trade) | 30,000 | unchanged |
| `max_trades_per_run` | per-engine (per-symbol attempts) | 5 | unchanged |
| `max_daily_loss_usd` (per-engine) | **DEPRECATED in multi-engine** | — | superseded by portfolio cap |
| `portfolio.max_daily_loss_usd` | **portfolio-wide** | 200 | hard kill switch trip; all engines stop, no new entries |
| `portfolio.max_concurrent_engines` | registry-wide | 4 | start request beyond cap = `EngineSlotFullError` (409) |
| `portfolio.max_total_trades_per_day` | portfolio-wide | 10 | once reached, no new entries; existing positions allowed to exit naturally |

The daily-aggregate state (realized P&L sum, trade count) is held in
`PortfolioRiskGate`. On backend restart we reconstruct from today's
`engine_events` table — simpler than persisting separately. Day boundary
is UTC midnight (matches the existing engine convention).

Defaults are tweakable on the UI but live behind a "Portfolio settings"
panel rather than the per-engine arm form.

### Decision 4 — UI layout: **sidebar list + main detail panel**

- **Sidebar (left, ~280px wide)**: ordered list of active engines. Each
  row shows symbol, current status (running / in-position / stopped /
  blocked), 1m MACD value, last gate result (passed / failed / fired),
  realized P&L for that run. Click to select; selected row highlights.
  Add "+ Add engine" button at the top, "Stop all" at the bottom.
- **Main panel (right, fills remainder)**: full detail of the selected
  engine. This is essentially today's single-engine `/engine` page —
  chart, gate stack, strategy state, live event log, recent fills.
- **Top bar**: portfolio summary strip — current holder of the mutex
  (or "idle"), portfolio daily P&L, trades today, max-loss progress bar.

Rationale: in autonomous mode the human's job is to glance + drill in,
not constantly watch all 4 panels. Sidebar+detail is the standard
trading-watchlist pattern (TradingView, DAS Trader). 2x2 grid would
compress each card too much at 24" monitor sizes.

Sidebar polling: status reads every 1s (already today's polling cadence).
Selected-engine detail uses WebSocket subscription for the live event
stream + indicators.

### Decision 5 — Manual Drop semantics: **Drop and Replace**

Per-engine card has a `Drop and Replace` button. Clicking:
1. Opens a small inline ticker-entry box (auto-focused).
2. User types new symbol + presses Enter.
3. Backend: atomic stop-then-start in the same slot. The old engine is
   stopped with `stop_reason="user_drop_replace"`, the new engine starts
   with the same risk caps + strategy params as the previous one (UI
   carry-forward).
4. If the slot was holding the portfolio mutex, the drop will fail with
   a 409 — you can't replace an engine that owns the current position.
   User must wait for exit or explicitly close the position first.

Separate `Stop` button on each card just stops without replacement —
slot frees up.

## Open questions deferred to later phases

- **Phase 2 (DTD auto-feed)**: when a 5th alert arrives with 4 slots full,
  which engine gets evicted? Coldest? Oldest? Worst score? Reject the
  new alert?
- **Phase 2**: DTD ingestion was on hold. What broke it last, and is it
  worth fixing in-place vs starting fresh? (Defer until we get there.)
- **Phase 3 (cold drop)**: what's "cold"? Time since last meaningful gate
  pass? Backside hard veto for N consecutive bars? User-tunable.
- **Phase 3 (calibration UI)**: what aggregate analytics matter for
  refining rules? Win rate by setup type, by time-of-day, by gap%, by
  float? Needs separate scoping.

## Implementation order (Phase 1 only)

Estimated 3-4 working days. Daily-ish slices:

```
Day 1   — Backend foundation
  ├── port_engine_runner_to_registry  (file move, dict-based store)
  ├── new portfolio_risk.py with PortfolioRiskGate + tests
  └── EngineSlotFullError, registry start/stop/active wiring

Day 2   — Engine integration
  ├── engine._handle_entry_signal consults the mutex
  ├── engine release on position-flat
  ├── engine_blocked_by_portfolio_mutex journal event
  └── portfolio daily-aggregate reconstruction on boot

Day 3   — API + types
  ├── /engine/start multi-tenant
  ├── /engine/stop?symbol=X, /engine/stop_all
  ├── /engine/status returns list, /engine/portfolio NEW
  ├── /engine/approve?run_id=X, /engine/reject?run_id=X
  └── frontend api client + types refresh

Day 4   — Frontend
  ├── Sidebar list + main panel layout
  ├── + Add Engine flow
  ├── Drop and Replace per card
  ├── Top bar portfolio summary
  ├── Visual lock indicator
  └── Smoke test the whole loop on EUR.USD (24/5)
```

## Safety hardening

Before flipping `autonomous=True` and turning the bot loose on paper:

1. **Mutex leak recovery on backend restart**: on `EngineRegistry`
   construction, query IBKR for actual open positions. If positions
   exist, identify which engine should resume holding them (by symbol)
   and acquire the mutex on its behalf. If no engine matches a held
   position, log a `mutex_recovery_orphaned_position` warning and
   refuse all new entries until a human resolves it.
2. **Hard daily kill switch**: when portfolio `max_daily_loss_usd` is
   reached, every engine stops, autonomous mode globally disabled until
   manual re-enable. Persisted to a `kill_switch_state` row so a backend
   restart honors it.
3. **Per-engine attempt counter**: after `max_trades_per_run` is hit on
   one symbol, that engine deactivates and frees its slot. Prevents one
   chop-happy ticker from monopolising a slot.
4. **Bootstrap-failure tolerance**: if bootstrap fails for one engine
   (e.g. delisted symbol, IBKR temporary error), that engine is marked
   `error` and the registry stays healthy. Other engines unaffected.
5. **Approval timeout for non-autonomous mode**: if `autonomous=False`
   and an approval is pending for >120s, auto-reject and journal
   `approval_timeout`. Prevents stale approvals from blocking the slot.

## Out-of-scope but worth noting

- **Multiple IBKR `clientId`s per engine**: would isolate engines at the
  TWS layer (each engine's events/orders fully separated). NOT needed at
  4 engines — TWS comfortably multiplexes 4 symbols on one connection.
  Reconsider if we scale beyond ~10 engines.
- **Account separation**: all engines run on the same paper account today.
  If you ever want to A/B different rule sets, you'd need per-engine
  account routing — deferred indefinitely.
- **Cross-engine learning**: if Engine A's recent fill informs Engine B's
  decision (e.g. "we're getting chopped today, tighten gates"), that's a
  Phase 3+ idea, not Phase 1.
