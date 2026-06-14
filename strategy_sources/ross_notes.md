# Ross / Warrior Trading - methodology notes

This file is the authoritative narrative source of truth for the Ross-Cameron-inspired
small-cap momentum strategy this system is being built to execute. It is intentionally
**descriptive, not prescriptive**: it records what we believe Ross's methodology is,
what we have actually validated against source material, and what is still an
unverified assumption.

The runtime strategy engine reads its rules from
[`strategy_rules.yaml`](strategy_rules.yaml). Every numeric threshold or behavioural
rule in that YAML must trace back to either:

- a public Ross/Warrior video, course module, or article (cite the source in
  [`assumptions_register.md`](assumptions_register.md));
- a screenshot, transcript, or note captured here in this file;
- a live-trade observation that we have logged in the system journal;
- or an explicit, deliberate placeholder marked `assumption` until validated.

Anything not in one of those four buckets must be flagged for review before it can
influence trading decisions.

### Companion files

This narrative file owns the **methodology** at a high level. Two
companion files own the **Ross-style soft reasoning** that doesn't fit in
the deterministic YAML:

- [`principles.md`](principles.md) — narrative principles and heuristics
  with stable `PRINCIPLE_ID`s the engine references when surfacing
  "Ross would say X" reasoning to the user.
- [`scenarios.yaml`](scenarios.yaml) — structured stimulus → response
  catalogue, one record per Warrior course Q&A or live-trade
  observation, each pointing back to one or more `PRINCIPLE_ID`s.

The decision pipeline is layered:

1. **Deterministic gates** from `strategy_rules.yaml` (the FLOOR; can BLOCK).
2. **Soft score** computed from `scoring.weights` (can DOWNGRADE to manual approval).
3. **Principles + scenarios** (this layer; surfaces reasoning, feeds soft-score inputs).
4. **LLM reasoner** (optional, opt-in per run; can VETO or SIZE-DOWN; CANNOT UPGRADE a blocked trade).

The rule "ML/LLM output cannot make the final decision" (see *Determinism*
below) is preserved by layer 4's veto-only contract.

---

## Framing constraint

Treat this as a **Ross-inspired small-cap momentum system**, not a verified clone.
Build the trading engine around a configurable strategy spec, not hard-coded magic.

> Do not infer exact proprietary Warrior rules unless source material is provided.
> Any rule not backed by a note, video transcript, screenshot, live-trade
> observation, or explicit configuration must be marked as `assumption`,
> `placeholder`, or `needs_validation`.

The system must be:

- rules-first;
- auditable end-to-end;
- paper-trading-only by default until performance is statistically proven;
- protected by hard risk controls that cannot be disabled by the trading logic.

---

## Known public components of the Warrior methodology

These items are observable from public Warrior content and chat-room behaviour.
They form the high-level shape of the strategy.

- Scanner-driven stock selection.
- Focus on stocks making unusual moves / "stocks at extremes".
- Preference for high-momentum stocks that are already moving.
- Use of premarket gappers, momentum scanners, and reversal scanners.
- Small-cap / low-float style universe where intraday volatility can be high.
- Key scanner fields driving selection: price, float, relative volume, halt
  status, news / catalyst, volume, and alert type.
- Trading is concentrated in the morning session; afternoon participation is
  reduced when momentum cools.

### Primary chart patterns

- bull flag
- first pullback
- micro pullback
- flat-top breakout
- ABCD-style continuation
- high-of-day break / new high continuation

### Bull flag logic (publicly described shape)

- A strong impulse move up;
- A brief pullback or consolidation;
- Entry on the first candle to make a new high after the pullback;
- Stop near the pullback low or another nearby invalidation level.

### Micro pullback logic (publicly described shape)

- Strong active momentum already in progress;
- A shallow pullback on a very short timeframe (10-second or 1-minute context);
- Continuation entry as momentum resumes (commonly described as the reclaim of
  the micro pullback's high or a one-candle pullback resolving).

### Trade-avoidance conditions

The system must **decline to trade** when any of the following are true:

- spreads are too wide;
- liquidity is inadequate;
- price action is choppy or messy;
- there is no clean technical level to define risk against;
- risk/reward at the candidate entry is poor;
- the symbol is halted.

### Per-trade required fields

Every trade decision must define:

- entry trigger and price;
- stop level and price;
- profit target(s);
- position size;
- max risk in dollars;
- setup type;
- invalidation reason;
- exit plan (partials, trail, full exit conditions).

### Non-negotiable risk controls

The risk controls listed below are **hard limits** in the system. The strategy
engine cannot override them; they are enforced by the risk gate.

- max loss per trade;
- max daily loss;
- max trades per day;
- max open positions;
- max position size;
- max order rate;
- no live trading by default;
- paper account default;
- manual approval required by default;
- emergency kill switch.

---

## Implementation contract

The strategy engine must satisfy the following structural requirements. These are
not Ross's rules - they are how the system **encodes and executes** whatever
Ross's rules turn out to be.

### Strategy spec sections

The YAML spec at [`strategy_rules.yaml`](strategy_rules.yaml) must contain:

1. **Universe filters**: `min_price`, `max_price`, `min_gap_percent`,
   `min_relative_volume`, `max_float`, `min_volume`, `min_dollar_volume`,
   `max_spread_percent`, `min_catalyst_score`, `allowed_exchange`,
   `exclude_etfs`, `exclude_warrants`, `exclude_low_liquidity`,
   `exclude_active_halts`.
2. **Market regime**: `cold_market` / `normal_market` / `hot_market`, with
   criteria for determining regime and the effect of regime on max risk, max
   size, aggressiveness, and target multiples.
3. **Setup definitions**: `bull_flag`, `first_pullback`, `micro_pullback`,
   `flat_top_breakout`, `high_of_day_break`, `ABCD_continuation`. Each setup
   defines required preconditions, chart timeframe, pattern detection logic,
   valid entry trigger, invalid entry trigger, stop placement, first target,
   second target, scale-out logic, trailing stop logic, and disqualification
   rules.
4. **Entry rules**: entry type (breakout, pullback reclaim, high-of-day break,
   first candle to make a new high), confirmation requirements, volume
   requirements, spread requirements, bid/ask behaviour requirements, maximum
   chase distance, order type rules, no-entry conditions.
5. **Stop rules**: stop at pullback low; stop at VWAP loss; stop at failed
   breakout level; stop at max cents/share risk; hard stop always required
   before order approval.
6. **Position sizing** (deterministic, risk-based):

   ```
   shares = floor(max_trade_risk_dollars / abs(entry_price - stop_price))
   ```

   then capped by `max_position_value`, `max_shares_per_trade`, liquidity cap,
   buying power cap, market regime cap, and the current daily P&L state.
7. **Exit rules**: partial at first target, partial into extension, stop
   remainder at breakeven or technical level after first partial, full exit on
   failed breakout, full exit on VWAP rejection, full exit if halt risk
   detected, full exit at max loss, optional time-based exit if momentum
   stalls.
8. **Trade quality scoring**: a 0-100 score per candidate, weighted across
   relative volume, float, gap percentage, news/catalyst, price range, clean
   chart pattern, proximity to high of day, above VWAP, spread quality,
   liquidity, risk/reward, time of day, market regime, and a prior-failed-
   attempts / choppiness penalty.
9. **Proven setup metric**: a setup is considered "proven" only once it has
   accumulated a minimum sample size with acceptable win rate, average winner,
   average loser, profit factor, expectancy per trade, max drawdown,
   slippage-adjusted profitability, performance by time of day, performance by
   setup type, and performance by market regime.
10. **Decision states** the engine may emit:
    - `IGNORE`
    - `WATCH`
    - `SETUP_FORMING`
    - `READY_FOR_MANUAL_APPROVAL`
    - `PAPER_ORDER_APPROVED`
    - `PAPER_ORDER_SENT`
    - `IN_TRADE`
    - `EXIT_NOW`
    - `BLOCKED_BY_RISK`
    - `BLOCKED_BY_UNCERTAINTY`
11. **Audit requirements**: every decision logs timestamp, ticker, scanner
    source, screenshot reference, parsed scanner fields, chart features,
    detected setup, score, entry, stop, target, risk dollars, position size,
    decision state, reason for action, reason for rejection, config version,
    and model/rule version.

### Determinism

> Build this as a rule engine first. Do not use a machine learning model to
> make final trading decisions yet. ML / LLM output can be used to summarise or
> label context, but the execution decision must come from deterministic rules.

### Promotion to autonomous trading

> Do not proceed to autonomous trading until the strategy has paper-traded and
> replay-tested with acceptable expectancy.

---

## Important correction on profitability

Ross's public P&L proves that **Ross** has edge, execution skill, discipline,
size tolerance, pattern recognition, and market experience. It does **not**
prove that an automated version of his rules will have edge.

For our system, the only proof that matters is:

```
Expectancy = (Win Rate * Average Win) - (Loss Rate * Average Loss)
```

The system needs to compute expectancy broken down by:

- setup type;
- time of day;
- market regime;
- float range;
- price range;
- scanner alert type.

Until the engine has a **positive expectancy after slippage, commissions,
rejected fills, bad spreads, and latency**, it is not a trading system. It is a
signal prototype.

---

## Relationship to the current POC

Before any Ross-specific logic is written, the codebase must demonstrate it can
do the boring-but-hard parts of real-time trading: connect to IBKR TWS paper,
stream live bars, compute a trivial indicator (MACD), generate signals, submit
orders, receive fills, track positions, measure slippage and latency, and log
everything.

That capability is being built first as a deliberately simple **MACD 1m
crossover long-only** strategy. The Ross logic described in this file replaces
the MACD strategy via the same `Strategy` interface once the POC is proven
operationally green.

Until then, every threshold in [`strategy_rules.yaml`](strategy_rules.yaml) is a
scaffolded placeholder. Treat it accordingly.
