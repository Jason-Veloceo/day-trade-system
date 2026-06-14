# Ross / Warrior - principles & heuristics catalogue

Narrative source of truth for the **soft, nuanced, "Ross-would-say-X"**
knowledge that does **not** belong in `strategy_rules.yaml` (which is for
deterministic thresholds only). This file is the human-readable companion to
[`scenarios.yaml`](scenarios.yaml), which encodes the same material as
structured stimulus → response records.

## How this file is used by the engine

This file is **layer 3** of the decision pipeline as agreed:

| Layer | Role | Who can BLOCK | Who can APPROVE |
|---|---|---|---|
| 1. Deterministic gates (`strategy_rules.yaml`) | Floor. | Yes | n/a (not enough alone) |
| 2. Soft-score | Sized-down / manual-approval if low. | Effectively yes | n/a |
| 3. **Principles + scenarios** (this file + `scenarios.yaml`) | Surfaces Ross-style reasoning at approval / block time; some principles map to soft-score inputs. | Indirectly (via score) | No |
| 4. LLM reasoner (optional, per-run) | Reads layers 1–3 + live features + DTD context. **Can VETO or SIZE-DOWN. CANNOT UPGRADE a blocked trade.** | Yes | No |

Every principle below has:

- A short stable **`PRINCIPLE_ID`** the engine logs when surfacing the
  principle to the user (e.g. `BIG_BID_IS_SUPPORT`).
- A **status** matching `assumptions_register.md` (`assumption` /
  `needs_validation` / `validated`).
- A **source** reference where applicable.
- An **engine use** note: hard rule? soft score input? surface only?
- A **parked** flag when we've intentionally deferred it.

## Status convention

Same vocabulary as `assumptions_register.md`:

| Status | Meaning |
|---|---|
| `placeholder` | We know the concept exists but haven't captured the principle yet. |
| `assumption` | Captured from our own inference; no traceable source. |
| `needs_validation` | Captured from a public Warrior source but not yet promoted. |
| `validated` | Confirmed against a recorded source (link in `source` field). |
| `parked` | Deliberately deferred. Recorded so we don't forget. |

---

## P0. The golden rule

### `GOLDEN_RULE_MINIMIZE_LOSERS`

**Status**: `validated`
**Source**: Warrior course Q "True or False - primary goal is to minimise
losers" → True. Captured `assets/image-389272f7-...png` (see
`scenarios.yaml::minimize_losers`).
**Engine use**: surface only — printed at the top of every approval and
block screen. Not hard-coded as an enforcement (we already enforce it
structurally through max-loss-per-trade, max-daily-loss, and the
no-entry-without-stop invariant).

> The primary goal of a consistently profitable trader is to **minimise
> losers**, not to maximise winners. Every gate, every soft-score
> penalty, every "are you sure" pause exists to honour this principle.

---

## P1. Entry timing

### `FIRST_CANDLE_NEW_HIGH_AFTER_PULLBACK_1M`

**Status**: `needs_validation`
**Source**: implicit in Ross's public bull-flag / first-pullback /
micro-pullback descriptions; matches the user-described NVFY scenario.
**Engine use**: hard rule — implemented as the `pullback_break` trigger in
`engine/triggers.py` and used by `FirstPullbackLong` v1.1.

The 1m candle whose high exceeds the high of the **last (most recent,
"smaller") red candle** in a fresh micro pullback is the canonical Ross
entry. This is the structural break trigger we built. The 1m timeframe
matches Ross's actual trading per Jason's note that he trades almost
exclusively on the 1m chart.

### `STARTER_THEN_SCALE_UP_THEN_MANAGE_OUT`

**Status**: `needs_validation`
**Source**: Warrior course Q "Bull flag forming with known entry/apex,
green T&S volume at 5.47..." (`assets/image-dedd4935-...png`); Jason's
direct guidance 2026-06-14 — Ross often takes a starter, then scales up
quickly with multiple orders, then exits all at once OR scales out as
the position turns.

**Engine use**: principle only today. **Surface to user** as a
recommendation when conditions match. **The current engine cannot
execute this** — it is single-position with optional partial exits.
Architectural extension required before this can be a hard rule:

- A logical **position** = a sequence of **legs** (entries).
- Each leg has its own entry timestamp, signal price, fill price, and
  size, but they share the position-level stop and management.
- Exit logic must support BOTH:
  - **Full exit** (close every leg at once on a hard signal like stop
    hit, MACD flip, L2 distress, halt risk).
  - **Scale out** (close legs in the same order they were opened — FIFO
    — as the position cools).
- Total size across all legs must respect `risk.max_position_value` and
  `risk.max_position_qty`.
- Each leg's entry must independently pass the deterministic gates;
  prior legs being open does NOT exempt a new leg from a fresh gate
  evaluation.

**Captured for future engine work** — see `scenarios.yaml::starter_then_scale_up`.

### `GREEN_TS_VOLUME_BELOW_PSYCH_LEVEL_IS_PRE_BREAK_SIGNAL`

**Status**: `needs_validation`
**Source**: Q5.37 (`assets/image-dedd4935-...png`). When the entry/apex
of the next "first candle to make new high" is known (e.g. 5.52), and
green T&S volume appears just below the half-dollar (5.47–5.49), this is
a pre-break signal that justifies a starter position.
**Engine use**: soft-score input + surface to user. Composes with
`PSYCH_LEVEL_MAGNETS` and `STARTER_THEN_SCALE_UP_THEN_MANAGE_OUT`.

### `LAYERED_ENTRIES_DURING_SUSTAINED_UPTREND`

**Status**: `needs_validation`
**Source**: PRGN chart (`assets/image-31a778be-...png`) — 3 distinct
entries marked during one continuous uptrend:
1. First 1m pullback after HOD momo scanner alert.
2. First 5m bull flag.
3. First 5m candle to make a new high.
**Engine use**: principle only today. The two 5m setups are **parked**
per Jason's guidance (Ross trades almost exclusively on 1m).

What we keep from this scenario for v1.1: **multiple sequential entries
on the same symbol in the same session are normal and expected.** The
engine's auto-rearm behaviour after every exit is therefore correct;
the user should not feel the system is "over-trading" if it fires 2–3
times on the same continuation.

---

## P2. Psychological levels

### `PSYCH_LEVEL_MAGNETS`

**Status**: `needs_validation`
**Source**: implicit in Q5.37's framing of 5.50 as the gate to 5.52, and
in widely-documented Warrior commentary on half-dollar and whole-dollar
levels.
**Engine use**: soft-score input + surface only.

**Levels to track per symbol per session:**

- **Half-dollar and whole-dollar** prices within ~2% of last price
  (e.g. for a $5 stock: 5.00, 5.50, 6.00).
- **Premarket high** (already a DTD field).
- **Prior-day high / prior-day low** (NEW — not yet captured by DTD form).
- **Current session high (HOD)** — already tracked.
- **VWAP** — already tracked.

**Behaviours associated with proximity to a psych level:**

| Proximity | Behaviour | Soft-score effect |
|---|---|---|
| Approaching from below, T&S green volume building | Pre-break signal | +score, principle `GREEN_TS_VOLUME_BELOW_PSYCH_LEVEL_IS_PRE_BREAK_SIGNAL` |
| At the level, large ask sitting | Reject risk | −score, principle `BIG_ASK_MAYBE_ICEBERG` |
| Just broke above the level | Cleanest entry zone if confirmed | +score |
| Just rejected from the level | Failed-break risk | hard veto via existing backside score |

### `NEXT_VISIBLE_BID_IS_NEXT_SUPPORT`

**Status**: `needs_validation`
**Source**: CHFS Q&A (`assets/image-eafc6cba-...png`) — "if stock drops
below 20.55, where will it likely drop to?" → 20.40 (the next visible
bid level on the L2).
**Engine use**: soft input to **stop placement**. Today the
`pullback_break` trigger sets the stop at `pullback_low − 2c`. This
principle says: also look at the L2 and place the stop **below the
nearest meaningful visible bid level** (not just the chart low) when
the two disagree.

Implementation note for a future pass:

> When `enable_depth=true`, compute the nearest visible bid level
> ≤ `pullback_low + buffer`. If a meaningful bid sits within that
> window, prefer placing the stop just below that bid level rather than
> the chart low. "Meaningful" = size ≥ some multiple of top-of-book size.

---

## P3. L2 (Level 2) reading — **soft / advisory inputs only**

Jason: "L2 guidelines — these do not have to be hard rules but should be
taken into account." All L2 principles below are therefore **soft-score
inputs** and **surface only**. None hard-blocks a trade.

### `BIG_BID_IS_SUPPORT`

**Status**: `needs_validation`
**Source**: Q "25k buyer on bid interpretation" (`assets/image-58e046b6-...png`)
and PULM image Q (`assets/image-fa727340-...png`) — large 34k bid creates
psychological support.
**Engine use**: soft-score input.

A bid size meaningfully larger than the rest of the book (default: ≥ 5×
median bid size in the top-N levels) acts as psychological support.
Composes well with `PSYCH_LEVEL_MAGNETS` when the big bid sits at a
half/whole dollar.

### `BIG_ASK_MAYBE_ICEBERG`

**Status**: `needs_validation`
**Source**: Q "25k seller on ask interpretation" (`assets/image-dfef3685-...png`)
— traders recognise there is a larger seller and the visible part may be
the tip of a larger hidden order.
**Engine use**: soft-score penalty + surface only.

A visible ask wall is informative **but not conclusive**. It can be:
- a real wall that the price stalls at, OR
- the visible fragment of an iceberg that will keep refreshing as it's
  hit, OR
- a spoof that lifts before the price arrives.

The engine should NOT hard-block on a single big ask. It SHOULD apply a
soft-score penalty proportional to wall size, and surface the wall in
the live features panel.

### `L2_ENABLES_THREE_EDGES`

**Status**: `validated`
**Source**: Q "Ability to read L2 will allow you to..." (`assets/image-62176d12-...png`)
→ all of the above.
**Engine use**: framing principle only.

The three edges L2 reading is supposed to provide:

1. **Determine psychological support and resistance levels.**
2. **Identify low-risk entry points.**
3. **Capture quick profits on the break of critical levels.**

The current engine surfaces the raw L2 features (spread, imbalance,
walls). It does NOT yet articulate which of these three edges a given
snapshot is enabling. A future UX improvement is to label each L2
snapshot with which of the three edges (if any) it currently supports.

### `L2_IS_MULTI_DIMENSIONAL`

**Status**: `validated`
**Source**: Q "Why is Level 2 important" (`assets/image-de3e9ea8-...png`)
→ all of: depth, lined-up buyers/sellers, big buyers/sellers nearby,
thin vs thick.
**Engine use**: framing principle. Already reflected in the live
features panel (spread, imbalance, walls, top-of-book sizes).

### `L1_INFORMATIONAL_FALLBACK`

**Status**: `validated`
**Source**: Q "Level 1 Top of Book describes" → all of above
(`assets/image-6f357534-...png`).
**Engine use**: informational. Today the engine subscribes to NBBO
when `order_type=LMT`. This is the L1 fallback when L2 is unavailable
(forex, no-subscription mode). Document that L1 still gives us best
price per route and (where applicable) halt-up / halt-down levels.

---

## P4. Time & Sales (T&S) reading — soft / advisory inputs

### `BIG_GREEN_BURST_IS_STRENGTH`

**Status**: `validated`
**Source**: Q "burst of large green orders on tape, what does it tell
us?" (`assets/image-6f357534-...png` — green orders Q) →
**strength, regardless of short sellers covering or new buyers taking
position**.
**Engine use**: soft-score input. The `tape_buy_pct_60s` feature already
captures this; reinforce by adding a `tape_big_green_burst` boolean
(size ≥ Nx median print size AND on-ask) as an additional input.

### `DONT_DISTINGUISH_INTENT_ON_THE_TAPE`

**Status**: `validated`
**Source**: same Q as above — the answer is "strength regardless".
**Engine use**: framing principle, surface only.

> Don't try to figure out whether the green prints are shorts covering
> or new longs entering. The tape doesn't care, and neither should the
> engine. Buying = buying. Selling = selling.

This is a guard against over-thinking on the strategy/principle side. The
engine should NOT add features that try to classify "is this a short
cover or a real long?" — there's no reliable signal.

### `TS_REPRESENTS_EVERY_EXECUTION`

**Status**: `validated`
**Source**: Q "What does T&S represent?" (`assets/image-96ea44af-...png`)
→ all: each order executed, at bid, at ask, timestamp.
**Engine use**: framing principle. Already reflected in our T&S ingestion
(`subscribe_tape` via `reqTickByTickData('AllLast')`).

---

## P5. Multi-timeframe doctrine

### `MIN_TWO_TIMEFRAMES`

**Status**: `validated`
**Source**: Q "How many time frames should an active trader review?"
(`assets/image-5ea70271-...png`) → at least 2.
**Engine use**: framing principle — already satisfied by 1m + 5m gates.

### `PARKED__HOLD_ABOVE_9_EMA_5M`

**Status**: `parked`
**Source**: Q "stocks hold above which MA on the 5-min chart?"
(`assets/image-3a80fb87-...png`) → 9 EMA.
**Parked reason**: Jason notes Ross trades almost exclusively on the 1m
chart. The 5m 9 EMA hold-line is a Warrior teaching point; record it
here but do NOT add it as a gate today.

If revisited later: this is a 5m trend filter (`price > EMA9(5m,
close)`) used as a soft gate during a position to decide whether to
hold a runner.

### `PARKED__FIRST_5M_CANDLE_NEW_HIGH_SETUP`

**Status**: `parked`
**Source**: AKER chart Q (`assets/image-11e5e060-...png`) and PRGN
entries #2 + #3 (`assets/image-31a778be-...png`).
**Parked reason**: same as above — 1m is Ross's primary chart per
Jason. If we ever want longer-hold continuations, this is the
canonical 5m setup.

---

## P6. Trade-avoidance principles

These already exist in `ross_notes.md` section "Trade-avoidance
conditions" — listed here for completeness with stable IDs.

### `AVOID__WIDE_SPREAD`
### `AVOID__INADEQUATE_LIQUIDITY`
### `AVOID__CHOPPY_PRICE_ACTION`
### `AVOID__NO_CLEAN_STOP`
### `AVOID__POOR_RISK_REWARD`
### `AVOID__HALTED_SYMBOL`

All status: `needs_validation`. Engine use: hard gate (already enforced
in `entry.no_entry_conditions` + `risk.reject_if_no_clean_stop`).

---

## P7. Risk-management principles

### `THREE_LOSS_CAPS`

**Status**: `validated`
**Source**: Q "A trader's set of rules should include" → all of:
max loss per day, max loss per trade, max number of consecutive losses
(`assets/image-65f2e99f-...png`).
**Engine use**: Two of three are hard caps today (max_loss_per_trade,
max_daily_loss). The third — **max consecutive losses** — is
**tracked-only** per Jason's guidance: record consecutive-loss count on
every closed trade, expose it on the engine page, but do NOT enforce a
hard stop on it yet. Decision pending: do we want consecutive-loss
auto-pause in paper, or only in live?

### `SIZE_DOWN_AFTER_LOSERS`

**Status**: `needs_validation`
**Source**: implicit in Ross's well-known "if you lose, get smaller"
mantra; matches `sizing.caps.daily_pnl_state_cap=true` in the YAML.
**Engine use**: soft-score / sizing input. Today the strategy spec
flags this is supposed to apply; the actual sizing math doesn't yet
read recent P&L to shrink the next trade. Future work.

---

## Catalogue index — see also

- [`scenarios.yaml`](scenarios.yaml) — structured stimulus → response
  records, one per Warrior course Q&A. Each scenario has a
  `principle_refs` list that points back to the IDs in this file.
- [`strategy_rules.yaml`](strategy_rules.yaml) — deterministic
  thresholds. New sections added for psychological levels, entry
  legs, L2/T&S guideline configuration, and tracked-only consecutive
  losses.
- [`assumptions_register.md`](assumptions_register.md) — status rows
  for every new principle and YAML value.
- [`ross_notes.md`](ross_notes.md) — narrative methodology source of
  truth; points here for principles.
