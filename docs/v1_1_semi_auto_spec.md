# v1.1 Semi-Automated FirstPullback Spec

Authoritative description of what the engine v1.1 build does. Every threshold
here is also exposed in code (`strategies/first_pullback_long.py`, `exits.py`,
`backside.py`) and surfaced on the `/engine` page so you can see it live.

## TL;DR

You arm a symbol (with DTD context). The engine watches a stack of gates and
either auto-fires entries or asks you to approve each one. While in a
position, an independent exit-trigger framework watches for distress and
exits the first time anything fires. The engine auto re-arms after each exit
so you can ride multiple legs in one session. Paper-only by hard config.

## Run lifecycle

1. **Arm** — you fill the form on `/engine`:
   - Symbol (e.g. `SPY`, `EUR.USD`)
   - Strategy: `first_pullback_long` (v1.1) or legacy `macd_crossover_long`
   - **Entry trigger** (first_pullback only): `pullback_break` (default,
     Ross-style structural breakout) or `macd_cross` (1m MACD histogram
     cross-up)
   - Quantity, risk caps
   - Order routing: `LMT` (default) with `offset_cents`, `sell_anchor`
     (`bid` aggressive / `ask` passive), and `cancel_after_seconds`; or `MKT`
   - Subscriptions: `enable_depth` (reqMktDepth 10 levels),
     `enable_tape` (reqTickByTickData AllLast)
   - DTD context (alert type, setup type, gap%, float, rel vol, news,
     premarket high, $-volume, notes) — persisted with the run, NOT used as
     gates
   - `autonomous` toggle (auto-fire vs require Approve on every entry)

2. **Watch** — strategy receives:
   - Closed 1m bars (existing `BarFeed`)
   - Closed 5m bars (new `HigherTimeframeAggregator` on top of the 1m stream)
   - Live L2 book (if enabled) and tape (if enabled)

3. **Entry attempt** — every 1m bar close:
   - Strategy evaluates the entry gate stack (below)
   - If passed, engine evaluates the microstructure last-look gate against the
     latest L2/T&S snapshot
   - Risk gate checks paper-only, caps, position state
   - If `autonomous`: order submitted immediately. If not: parked, waits for
     Approve / Reject

4. **In position** — the `ExitTriggerSet` evaluates every 1m bar; the FIRST
   trigger that fires wins, the engine submits the exit, and the strategy
   auto re-arms.

5. **Stop** — you click Stop. The engine cancels subscriptions and writes a
   final `engine_stop` event.

## Entry gate stack

Evaluated in order on every closed 1m bar. Any failure = no entry.

| # | Gate | What it checks |
|---|------|----------------|
| 1 | **5m MACD positive** | `macd_5m.histogram > 0` |
| 2 | **5m MACD not falling** | `macd_5m.histogram >= prev_macd_5m.histogram` |
| 3 | **1m MACD context** | `macd_1m.histogram > 0` (positive momentum context) — only when `trigger_mode='pullback_break'` |
| 4 | **VWAP** | price above session VWAP (skipped if no volume / forex) |
| 5 | **Backside hard vetoes** | 5m MACD neg+falling / 1m already crossed down today / VWAP-loss latch / late-day no-HOD |
| 6 | **Backside soft score** | weighted score < threshold (default 60) |
| 7 | **Entry trigger** | pullback_break (default) OR macd_cross — see below |
| 8 | **Microstructure last-look** (engine) | spread, bid:ask imbalance, tape buy% — only if L2/T&S available |
| 9 | **Risk gate** | paper-only invariant, position cap, daily-loss cap |

Failed gates are surfaced live in the Strategy state panel.

### Entry triggers (`engine/triggers.py`)

`trigger_mode` is a strategy param exposed in the Arm form.

**`pullback_break`** (default — Ross "first 1m candle to make a new high after
the micro pullback"):

1. Walk back through the recent 1m bar history.
2. Find the most recent contiguous **red-candle run** (1–3 bars by default).
3. Up to 3 green bars are allowed between the pullback end and the
   current bar (so the trigger can fire on the *second* green attempt,
   matching Ross's typical pattern).
4. Require at least 1 green **impulse bar** preceding the pullback.
5. Define `pullback_test_high` = the high of the **last** (most recent,
   "smaller") red bar in the pullback.
6. Define `pullback_low` = min low of the pullback bars.
7. **Fire** when the current bar is green AND `current.high > pullback_test_high`.

When this trigger fires, `pullback_low` is used as the suggested stop
(stop = `pullback_low − stop_buffer_cents`, default buffer 2¢).

**`macd_cross`** (alternative — indicator-only):

Fires when the 1m MACD histogram either crosses ≥0 from <0 or is already
positive and strictly increasing.

The pullback config (`PullbackBreakConfig`):
`min_pullback_bars=1`, `max_pullback_bars=3`,
`max_bars_since_pullback_end=3`, `min_impulse_bars=1`, `strict_break=True`.

## Exit trigger framework

Independent triggers. First one to fire wins.

| Trigger | Default condition |
|---------|-------------------|
| `hard_stop` | bar `low <= stop_price` (stop = recent 3-bar low − 2c) |
| `first_target` | bar `high >= entry + 1R` → scale 50%, move stop to BE |
| `second_target` | bar `high >= entry + 2R` → full exit |
| `macd_flip` | 1m histogram crosses from positive to non-positive |
| `vwap_loss` | price below VWAP for N consecutive bars after entry (default 2) |
| `l2_distress` | bid:ask imbalance < 0.30 OR ask wall ≥ 5x top-of-book within 20bps of mid |
| `tape_flip` | buy% ≤ 0.40 AND speed-decay ≤ −0.30 for N consecutive bars (default 2) |
| `time_stop` | no progress (within ±5c of entry) after N bars (default 8) |

Every fire is journaled as a `decision` event with `stage=exit_trigger`,
including the trigger kind, the reason string, and the observed values.

## Backside gate (hybrid)

`engine/backside.py::BacksideGate.evaluate()`.

**Hard vetoes** (any one fires → entry blocked):

- 5m MACD histogram is negative AND falling vs previous 5m close
- 1m MACD has already crossed down at least once today
- Price below VWAP for `vwap_loss_bars_required` consecutive bars (default 3)
- Post 10:30 ET (14:30 UTC) AND no new HOD in the last
  `late_day_grace_bars` bars (default 5)

**Soft score** (0..100; blocks when ≥ `score_block_threshold`, default 60):

| Component | Weight | Zero at | Full at |
|-----------|--------|---------|---------|
| `lower_highs` | 25 | 2 | 6 |
| `tape_buy_pct` | 25 | 0.55 | 0.40 |
| `tape_speed_decay` | 20 | −0.20 | −0.50 |
| `failed_setups` | 20 | 0 | 3 |
| `volume_decay` | 10 | −0.20 | −0.50 |

All weights and thresholds are constructor args on `BacksideConfig`; future
work loads them from `strategy_sources/strategy_rules.yaml`.

## L2 / T&S features

`engine/features.py::compute_snapshot()` materialises:

- **Depth**: best_bid / best_ask / spread (bps) / mid /
  bid_ask_imbalance (bid share, top of book) /
  ask_wall_price + size + distance_bps (largest ask within 50bps of mid)
- **Tape**: tape_buy_pct (last 60s) / tape_speed (prints per sec, last 30s) /
  tape_speed_decay (30s vs 60s, normalised) / tape_count (last 60s) /
  tape_buy_volume / tape_sell_volume

The Live features card on `/engine` shows all of these in real time once
a run is armed, color-coded against the gate thresholds.

Graceful degradation: if `enable_depth=false` or `enable_tape=false`, all the
corresponding features return `None` and any gate using them is **skipped**,
not failed. On forex IDEALPRO the L2 book is sparse to non-existent; the
strategy still works using just 5m+1m MACD + VWAP (if it has volume) +
backside-time gate.

## Order routing

The executor supports two order types via `EngineConfig.order_type`:

- **`MKT`** — vanilla market order. Used for legacy MACD crossover and as a
  fallback when no NBBO is available.
- **`LMT`** — marketable-limit emulating your DAS hotkeys:
  - BUY (always anchored to the ask): `limit_price = best_ask + offset_cents`
  - SELL (per `sell_anchor`):
    - `sell_anchor='bid'` (aggressive, default): `limit_price = best_bid − offset_cents`
      — matches your "Sell at Bid" hotkeys (`Ctrl+F`/`X`/`C`/`Z`).
    - `sell_anchor='ask'` (passive): `limit_price = best_ask − offset_cents`
      — matches your "Sell at Ask" hotkeys (`Ctrl+K`/`L`/`J`).
  - `tif=DAY`, `outsideRth=True`
  - If not filled in `cancel_after_seconds` (default 3s), the executor
    cancels the order. The engine then re-evaluates gates on the next bar.

`sell_anchor` applies to **all** exit legs (hard stop, targets, distress,
time-stop). Aggressive (bid-anchored) is the safe default because it
prioritises getting out over fill quality; pick `ask` when you'd rather
sit on the offer side and let buyers come up.

This solves the "I usually buy on the ask, sell on the bid (but sometimes
get away with selling at ask)" requirement while bounding slippage and
avoiding stuck-order risk.

## DTD context

Persisted on `engine_runs.dtd_context` (JSONB). Fields:

- `alert_type` (free text from DTD widget)
- `setup_type` (`first_pullback`, `micro_pullback`, `bull_flag`,
  `hod_break`, `flat_top_breakout`, `abcd_continuation`)
- `gap_pct`, `float_shares_millions`, `rel_vol`,
  `dollar_volume_millions`, `premarket_high`
- `has_news`, `news_headline`
- `notes` (free text)

These are NOT used as gates today — they're a record of the trader's
mental state at arm-time, available for post-mortem and future scoring.

## What we explicitly did NOT build in v1.1

- moomoo / Futu OpenAPI integration. Research is in `data_feeds_moomoo_vs_ibkr.md`.
- True "statistical probability" of continuation. Needs a real backtest /
  trade history; today the gates are deterministic checklist rules.
- Multi-position pyramiding. Single open position at a time.
- Bootstrap of historical 5m bars at engine start. The 5m MACD warms up
  live (takes ~30+ minutes of bars before the 5m trend gate is meaningful).
- Multi-symbol arming. One engine, one symbol.
