# Assumptions register

Every threshold, weight, or behavioural rule that influences a trading
decision must be tracked here. The runtime engine refuses to act on values
whose status is `assumption` or `placeholder` when running autonomously - they
must be promoted to `validated` first by a deliberate human review against a
recorded source.

## Status definitions

| Status              | Meaning                                                                                        |
| ------------------- | ---------------------------------------------------------------------------------------------- |
| `placeholder`       | Schema slot exists; no meaningful value yet.                                                   |
| `assumption`        | Value chosen by us (or by ChatGPT's seed) without an external source. Untrusted.               |
| `needs_validation`  | Value present and inferred from public Warrior content but not yet traced to a citation.       |
| `validated`         | Traced to a specific source recorded in the `source` column and confirmed by reviewer.         |
| `non_negotiable`    | Hard system invariant. Cannot be relaxed by any strategy spec.                                 |
| `tracked_only`      | Engine records the metric but does NOT act on it. Promoted later by deliberate decision.       |
| `parked`            | Concept intentionally deferred. Recorded so it isn't lost.                                     |

## How to update

1. Provide a source: a Ross / Warrior video timestamp, transcript excerpt,
   screenshot path, course module ID, or live-trade observation row in the
   `engine_events` table.
2. Update the row's `status` to `validated` and fill in the `source` column.
3. Bump `meta.version` in [`strategy_rules.yaml`](strategy_rules.yaml).
4. Set `last_reviewed_at` on the affected row.

---

## Register

### Universe filters

| rule_id                            | current_value                   | status      | source | last_reviewed | notes |
| ---------------------------------- | ------------------------------- | ----------- | ------ | ------------- | ----- |
| `universe.min_price`               | 1.00                            | assumption  | -      | 2026-06-14    | ChatGPT seed value. |
| `universe.max_price`               | 20.00                           | assumption  | -      | 2026-06-14    | ChatGPT seed value. |
| `universe.min_gap_percent`         | 10                              | assumption  | -      | 2026-06-14    | ChatGPT seed value. |
| `universe.min_relative_volume`     | 5                               | assumption  | -      | 2026-06-14    | Relative to daily average. |
| `universe.max_float_millions`      | 20                              | assumption  | -      | 2026-06-14    | Ross frequently discusses low-float; specific cap unverified. |
| `universe.min_volume`              | 500,000                         | assumption  | -      | 2026-06-14    | Day cumulative volume. |
| `universe.min_dollar_volume`       | null                            | placeholder | -      | 2026-06-14    | Needs definition. |
| `universe.max_spread_percent`      | 1.5                             | assumption  | -      | 2026-06-14    | |
| `universe.min_catalyst_score`     | null                            | placeholder | -      | 2026-06-14    | Catalyst scoring system not yet built. |
| `universe.allowed_exchanges`       | NASDAQ, NYSE, AMEX              | assumption  | -      | 2026-06-14    | |
| `universe.exclude_etfs`            | true                            | assumption  | -      | 2026-06-14    | |
| `universe.exclude_warrants`        | true                            | assumption  | -      | 2026-06-14    | |
| `universe.exclude_low_liquidity`   | true                            | assumption  | -      | 2026-06-14    | Definition of low_liquidity TBD. |
| `universe.exclude_active_halts`    | true                            | assumption  | -      | 2026-06-14    | |

### Market regime

| rule_id                                              | current_value | status      | source | last_reviewed | notes |
| ---------------------------------------------------- | ------------- | ----------- | ------ | ------------- | ----- |
| `market_regime.cold_market.max_risk_multiplier`      | 0.5           | assumption  | -      | 2026-06-14    | |
| `market_regime.cold_market.max_size_multiplier`      | 0.5           | assumption  | -      | 2026-06-14    | |
| `market_regime.cold_market.target_multiple`          | 1.5           | assumption  | -      | 2026-06-14    | |
| `market_regime.normal_market.target_multiple`        | 2.0           | assumption  | -      | 2026-06-14    | |
| `market_regime.hot_market.max_risk_multiplier`       | 1.5           | assumption  | -      | 2026-06-14    | |
| `market_regime.hot_market.max_size_multiplier`       | 1.25          | assumption  | -      | 2026-06-14    | |
| `market_regime.hot_market.target_multiple`           | 2.5           | assumption  | -      | 2026-06-14    | |
| regime classification criteria (all three regimes)   | -             | placeholder | -      | 2026-06-14    | Need objective inputs (gapper count, broad-market vol). |

### Setups

| rule_id                                                  | current_value                                  | status      | source | last_reviewed | notes |
| -------------------------------------------------------- | ---------------------------------------------- | ----------- | ------ | ------------- | ----- |
| `setups.bull_flag.required_impulse_percent`              | 5                                              | assumption  | -      | 2026-06-14    | |
| `setups.bull_flag.pullback_min_bars`                     | 2                                              | assumption  | -      | 2026-06-14    | |
| `setups.bull_flag.pullback_max_bars`                     | 5                                              | assumption  | -      | 2026-06-14    | |
| `setups.bull_flag.max_pullback_percent_of_impulse`       | 50                                             | assumption  | -      | 2026-06-14    | |
| `setups.bull_flag.entry_trigger`                         | first_candle_to_make_new_high_after_pullback   | needs_validation | -      | 2026-06-14    | Shape is publicly described; exact bar rule still loose. |
| `setups.bull_flag.stop_rule`                             | below_pullback_low                             | needs_validation | -      | 2026-06-14    | |
| `setups.bull_flag.min_reward_risk`                       | 2.0                                            | assumption  | -      | 2026-06-14    | |
| `setups.first_pullback` (everything)                     | placeholder                                    | placeholder | -      | 2026-06-14    | Spec not yet drafted. |
| `setups.micro_pullback.timeframes`                       | [10s, 1m]                                      | assumption  | -      | 2026-06-14    | |
| `setups.micro_pullback.max_pullback_bars`                | 3                                              | assumption  | -      | 2026-06-14    | |
| `setups.micro_pullback.entry_trigger`                    | reclaim_of_micro_pullback_high                 | needs_validation | -      | 2026-06-14    | |
| `setups.micro_pullback.stop_rule`                        | below_micro_pullback_low                       | needs_validation | -      | 2026-06-14    | |
| `setups.micro_pullback.min_reward_risk`                  | 1.5                                            | assumption  | -      | 2026-06-14    | Ross often uses 1.5R+ on micros; magnitude unverified. |
| `setups.flat_top_breakout.min_touches`                   | 2                                              | assumption  | -      | 2026-06-14    | |
| `setups.flat_top_breakout.max_resistance_band_percent`   | 0.5                                            | assumption  | -      | 2026-06-14    | |
| `setups.flat_top_breakout.min_reward_risk`               | 2.0                                            | assumption  | -      | 2026-06-14    | |
| `setups.high_of_day_break.require_volume_expansion`      | true                                           | assumption  | -      | 2026-06-14    | |
| `setups.high_of_day_break.max_chase_percent`             | 1.0                                            | assumption  | -      | 2026-06-14    | |
| `setups.high_of_day_break.min_reward_risk`               | 2.0                                            | assumption  | -      | 2026-06-14    | |
| `setups.ABCD_continuation`                               | disabled                                       | placeholder | -      | 2026-06-14    | Not yet specified. |

### Entry rules

| rule_id                              | current_value | status      | source | last_reviewed | notes |
| ------------------------------------ | ------------- | ----------- | ------ | ------------- | ----- |
| `entry.max_chase_distance_cents`     | 5             | assumption  | -      | 2026-06-14    | |
| `entry.order_type.default`           | LMT           | assumption  | -      | 2026-06-14    | POC uses MKT for slippage measurement. |
| `entry.order_type.aggressive_regime` | MKT           | assumption  | -      | 2026-06-14    | |
| `entry.volume_requirements.min_volume_expansion_factor` | 1.5 | assumption | -    | 2026-06-14    | |
| `entry.spread_requirements.max_spread_percent` | 1.5     | assumption  | -      | 2026-06-14    | |

### Stops

| rule_id                                | current_value      | status         | source | last_reviewed | notes |
| -------------------------------------- | ------------------ | -------------- | ------ | ------------- | ----- |
| `stops.required_before_order_approval` | true               | non_negotiable | -      | 2026-06-14    | Always required. |
| `stops.default_rule`                   | below_pullback_low | assumption     | -      | 2026-06-14    | |
| `stops.max_cents_per_share_risk`       | 20                 | assumption     | -      | 2026-06-14    | |

### Sizing

| rule_id                                          | current_value | status         | source | last_reviewed | notes |
| ------------------------------------------------ | ------------- | -------------- | ------ | ------------- | ----- |
| `sizing.max_trade_risk_dollars`                  | 50            | assumption     | -      | 2026-06-14    | |
| `sizing.caps.max_position_value_dollars`         | 5000          | assumption     | -      | 2026-06-14    | |
| `sizing.caps.liquidity_cap_pct_of_avg_volume`    | 1             | assumption     | -      | 2026-06-14    | |
| `sizing.caps.market_regime_cap`                  | true          | assumption     | -      | 2026-06-14    | |
| `sizing.caps.daily_pnl_state_cap`                | true          | assumption     | -      | 2026-06-14    | |

### Exits

| rule_id                                       | current_value           | status         | source | last_reviewed | notes |
| --------------------------------------------- | ----------------------- | -------------- | ------ | ------------- | ----- |
| `exits.partial_at_first_target`               | true                    | assumption     | -      | 2026-06-14    | |
| `exits.partial_at_first_target_pct`           | 50                      | assumption     | -      | 2026-06-14    | |
| `exits.partial_into_extension`                | true                    | assumption     | -      | 2026-06-14    | |
| `exits.stop_remainder_after_first_partial`    | breakeven_or_technical  | assumption     | -      | 2026-06-14    | |
| `exits.exit_on_failed_breakout`               | true                    | assumption     | -      | 2026-06-14    | |
| `exits.exit_on_vwap_rejection`                | true                    | assumption     | -      | 2026-06-14    | |
| `exits.exit_on_halt_risk`                     | true                    | assumption     | -      | 2026-06-14    | |
| `exits.exit_at_max_loss`                      | true                    | non_negotiable | -      | 2026-06-14    | |
| `exits.time_based_exit.stall_minutes`         | 3                       | assumption     | -      | 2026-06-14    | |

### Scoring

| rule_id                                          | current_value | status      | source | last_reviewed | notes |
| ------------------------------------------------ | ------------- | ----------- | ------ | ------------- | ----- |
| `scoring.minimum_score_to_watch`                 | 60            | assumption  | -      | 2026-06-14    | |
| `scoring.minimum_score_to_prepare_trade`         | 75            | assumption  | -      | 2026-06-14    | |
| `scoring.minimum_score_to_request_manual_approval` | 85          | assumption  | -      | 2026-06-14    | |
| `scoring.weights.*`                              | sums to 100   | assumption  | -      | 2026-06-14    | Re-normalise when regime, HOD proximity, choppiness penalty added. |

### Proven setup thresholds

| rule_id                                          | current_value | status         | source | last_reviewed | notes |
| ------------------------------------------------ | ------------- | -------------- | ------ | ------------- | ----- |
| `proven_setup_thresholds.minimum_sample_size`    | 30            | assumption     | -      | 2026-06-14    | |
| `proven_setup_thresholds.min_win_rate`           | 0.40          | assumption     | -      | 2026-06-14    | |
| `proven_setup_thresholds.min_profit_factor`      | 1.5           | assumption     | -      | 2026-06-14    | |
| `proven_setup_thresholds.min_expectancy_per_trade_dollars` | 5   | assumption     | -      | 2026-06-14    | After slippage + commission. |
| `proven_setup_thresholds.max_drawdown_pct`       | 10            | assumption     | -      | 2026-06-14    | |
| `proven_setup_thresholds.slippage_adjusted_profit_required` | true | non_negotiable | -   | 2026-06-14    | |

### Risk controls

| rule_id                                  | current_value | status         | source | last_reviewed | notes |
| ---------------------------------------- | ------------- | -------------- | ------ | ------------- | ----- |
| `risk.paper_trading_only`                | true          | non_negotiable | -      | 2026-06-14    | |
| `risk.live_trading_enabled`              | false         | non_negotiable | -      | 2026-06-14    | Default. Flip only with explicit, deliberate ceremony. |
| `risk.manual_approval_required`          | true          | non_negotiable | -      | 2026-06-14    | Per-run autonomous flag may opt out, paper-only. |
| `risk.max_risk_per_trade_dollars`        | 50            | assumption     | -      | 2026-06-14    | |
| `risk.max_daily_loss_dollars`            | 150           | assumption     | -      | 2026-06-14    | |
| `risk.max_trades_per_day`                | 5             | assumption     | -      | 2026-06-14    | |
| `risk.max_open_positions`                | 1             | assumption     | -      | 2026-06-14    | |
| `risk.max_position_value_dollars`        | 5000          | assumption     | -      | 2026-06-14    | |
| `risk.max_order_rate_per_minute`         | 3             | assumption     | -      | 2026-06-14    | |
| `risk.stop_required`                     | true          | non_negotiable | -      | 2026-06-14    | |
| `risk.reject_if_no_clean_stop`           | true          | non_negotiable | -      | 2026-06-14    | |
| `risk.emergency_kill_switch`             | true          | non_negotiable | -      | 2026-06-14    | |

---

## Outstanding work (high-priority placeholders)

These are missing definitions that block confident execution. They must be
filled in before promoting any setup to autonomous trading.

- `universe.min_dollar_volume` - define
- `universe.min_catalyst_score` - define catalyst scoring system
- `market_regime.*.criteria` - objective, computable criteria for cold / normal / hot
- All `first_target` / `second_target` / `scale_out_logic` / `trailing_stop_logic` fields per setup
- `first_pullback` full spec
- `ABCD_continuation` full spec
- `entry.bid_ask_behaviour.require_lifted_offer` - decide on / off
- `proven_setup_thresholds.*` values backed by realistic Ross-strategy backtest data

---

## Principles & scenarios (2026-06-14 Warrior course Q&A ingest)

Status rows for the principles introduced in
[`principles.md`](principles.md) and the structured scenarios in
[`scenarios.yaml`](scenarios.yaml). Every row points to BOTH the
principle ID and the scenario(s) that source it.

### Golden rule

| principle_id                       | status     | scenario(s)         | source                                              | last_reviewed | notes |
| ---------------------------------- | ---------- | ------------------- | --------------------------------------------------- | ------------- | ----- |
| `GOLDEN_RULE_MINIMIZE_LOSERS`      | validated  | minimize_losers     | Warrior TF Q (`image-389272f7`)                     | 2026-06-14    | Surface-only. Jason: "I think it's obvious. Not sure we have to hard-code that." |

### Entry timing

| principle_id                                              | status            | scenario(s)                                       | source                                  | last_reviewed | notes |
| --------------------------------------------------------- | ----------------- | ------------------------------------------------- | --------------------------------------- | ------------- | ----- |
| `FIRST_CANDLE_NEW_HIGH_AFTER_PULLBACK_1M`                | validated         | first_1m_candle_new_high_after_micro_pullback     | Jason NVFY example + Warrior public material | 2026-06-14    | Implemented in `engine/triggers.py::detect_pullback_break`. |
| `STARTER_THEN_SCALE_UP_THEN_MANAGE_OUT`                   | needs_validation  | bull_flag_starter_below_psych_level_with_green_ts | Warrior Q5.37 + Jason guidance          | 2026-06-14    | Engine extension required (multi-leg position). |
| `GREEN_TS_VOLUME_BELOW_PSYCH_LEVEL_IS_PRE_BREAK_SIGNAL`   | needs_validation  | bull_flag_starter_below_psych_level_with_green_ts | Warrior Q5.37 (`image-dedd4935`)        | 2026-06-14    | Soft-score input, composes with `PSYCH_LEVEL_MAGNETS`. |
| `LAYERED_ENTRIES_DURING_SUSTAINED_UPTREND`                | needs_validation  | layered_entries_during_sustained_uptrend          | Warrior PRGN chart (`image-31a778be`)   | 2026-06-14    | 5m sub-setups parked; 1m re-entry already supported via auto-rearm. |

### Psychological levels

| principle_id                          | status            | scenario(s)                                                                   | source                                          | last_reviewed | notes |
| ------------------------------------- | ----------------- | ----------------------------------------------------------------------------- | ----------------------------------------------- | ------------- | ----- |
| `PSYCH_LEVEL_MAGNETS`                 | needs_validation  | bull_flag_starter_below_psych_level_with_green_ts, large_bid_at_psych_level_is_support | Warrior Q5.37 + widely-documented Warrior commentary | 2026-06-14    | Soft-score input. Engine extension required. |
| `NEXT_VISIBLE_BID_IS_NEXT_SUPPORT`   | validated         | next_visible_bid_is_next_support                                              | Warrior CHFS Q (`image-eafc6cba`)               | 2026-06-14    | Soft input to stop placement when L2 enabled. |

### L2 reading

| principle_id                  | status     | scenario(s)                              | source                                          | last_reviewed | notes |
| ----------------------------- | ---------- | ---------------------------------------- | ----------------------------------------------- | ------------- | ----- |
| `BIG_BID_IS_SUPPORT`          | validated  | large_bid_at_psych_level_is_support      | Warrior Qs (`image-58e046b6`, `image-fa727340`) | 2026-06-14    | Soft +score. Jason: "do not have to be hard rules but should be taken into account." |
| `BIG_ASK_MAYBE_ICEBERG`       | validated  | large_ask_might_be_iceberg               | Warrior Q (`image-dfef3685`)                    | 2026-06-14    | Soft -score, never hard-block. |
| `L2_ENABLES_THREE_EDGES`      | validated  | l2_enables_three_edges                   | Warrior Q (`image-62176d12`)                    | 2026-06-14    | Framing principle. UX TODO: label each L2 snapshot. |
| `L2_IS_MULTI_DIMENSIONAL`     | validated  | l2_is_multi_dimensional                  | Warrior Q (`image-de3e9ea8`)                    | 2026-06-14    | Framing. Already reflected in features panel. |
| `L1_INFORMATIONAL_FALLBACK`   | validated  | l1_top_of_book_informational             | Warrior Q (`image-6f357534` L1)                 | 2026-06-14    | Already used as NBBO source for LMT pricing. |

### T&S reading

| principle_id                              | status     | scenario(s)                          | source                                          | last_reviewed | notes |
| ----------------------------------------- | ---------- | ------------------------------------ | ----------------------------------------------- | ------------- | ----- |
| `BIG_GREEN_BURST_IS_STRENGTH`            | validated  | big_green_burst_is_strength          | Warrior Q (`image-76d86d7e`)                    | 2026-06-14    | Soft +score input. Future: `tape_big_green_burst_30s` feature. |
| `DONT_DISTINGUISH_INTENT_ON_THE_TAPE`    | validated  | big_green_burst_is_strength          | same Q                                          | 2026-06-14    | Framing principle - DO NOT build "is this a cover or new long" features. |
| `TS_REPRESENTS_EVERY_EXECUTION`          | validated  | ts_represents_every_execution        | Warrior Q (`image-96ea44af`)                    | 2026-06-14    | Already reflected in `subscribe_tape`. |

### Multi-timeframe

| principle_id                              | status     | scenario(s)                              | source                                          | last_reviewed | notes |
| ----------------------------------------- | ---------- | ---------------------------------------- | ----------------------------------------------- | ------------- | ----- |
| `MIN_TWO_TIMEFRAMES`                     | validated  | minimum_two_timeframes_before_trade      | Warrior Q (`image-5ea70271`)                    | 2026-06-14    | Already satisfied by 1m + 5m gates. |
| `PARKED__HOLD_ABOVE_9_EMA_5M`            | parked     | hold_above_5m_9_ema_PARKED               | Warrior Q (`image-3a80fb87`)                    | 2026-06-14    | Parked - Ross trades 1m primarily. |
| `PARKED__FIRST_5M_CANDLE_NEW_HIGH_SETUP` | parked     | first_5m_candle_new_high                 | Warrior AKER Q (`image-11e5e060`)               | 2026-06-14    | Parked - same reason. |

### Risk management

| principle_id          | status                | scenario(s)        | source                                          | last_reviewed | notes |
| --------------------- | --------------------- | ------------------ | ----------------------------------------------- | ------------- | ----- |
| `THREE_LOSS_CAPS`    | validated (partial)   | three_loss_caps    | Warrior Q (`image-65f2e99f`)                    | 2026-06-14    | 2 of 3 enforced (per-trade, daily). Consecutive losses is `tracked_only`. |
| `SIZE_DOWN_AFTER_LOSERS` | needs_validation  | -                  | implicit Warrior mantra; `sizing.caps.daily_pnl_state_cap=true` | 2026-06-14 | Flagged in YAML; sizing math doesn't yet read recent P&L. |

### Trade avoidance (existing - listed for catalogue completeness)

| principle_id                       | status            | source                              | last_reviewed | notes |
| ---------------------------------- | ----------------- | ----------------------------------- | ------------- | ----- |
| `AVOID__WIDE_SPREAD`              | needs_validation  | `ross_notes.md` trade-avoidance     | 2026-06-14    | Already enforced via `entry.no_entry_conditions`. |
| `AVOID__INADEQUATE_LIQUIDITY`     | needs_validation  | same                                | 2026-06-14    | Same. |
| `AVOID__CHOPPY_PRICE_ACTION`      | needs_validation  | same                                | 2026-06-14    | Same. |
| `AVOID__NO_CLEAN_STOP`            | non_negotiable    | `risk.reject_if_no_clean_stop`       | 2026-06-14    | Hard invariant. |
| `AVOID__POOR_RISK_REWARD`         | needs_validation  | per-setup `min_reward_risk`         | 2026-06-14    | |
| `AVOID__HALTED_SYMBOL`            | needs_validation  | `universe.exclude_active_halts`     | 2026-06-14    | |

### New YAML thresholds (sections 13-17 of strategy_rules.yaml)

| rule_id                                                     | current_value     | status        | source / scenario                                            | last_reviewed | notes |
| ----------------------------------------------------------- | ----------------- | ------------- | ------------------------------------------------------------ | ------------- | ----- |
| `risk.consecutive_losses.mode`                             | tracked_only      | assumption    | `THREE_LOSS_CAPS`                                            | 2026-06-14    | Promote to soft_pause / hard_pause later based on paper data. |
| `risk.consecutive_losses.max_count_before_pause`          | 3                 | assumption    | Jason guidance - placeholder                                 | 2026-06-14    | Not enforced today. |
| `psychological_levels.enabled`                            | false             | assumption    | `PSYCH_LEVEL_MAGNETS`                                        | 2026-06-14    | Engine doesn't compute these yet. |
| `psychological_levels.proximity_window_cents`             | 5                 | assumption    | `PSYCH_LEVEL_MAGNETS`                                        | 2026-06-14    | |
| `psychological_levels.level_kinds.prior_day_high/low`     | true              | assumption    | `PSYCH_LEVEL_MAGNETS`                                        | 2026-06-14    | Needs new DTD form fields. |
| `psychological_levels.scoring.*`                          | 5-10              | assumption    | Jason: "soft / advisory"                                     | 2026-06-14    | Subject to score weight re-normalisation. |
| `entry_legs.enabled`                                       | false             | assumption    | `STARTER_THEN_SCALE_UP_THEN_MANAGE_OUT`                      | 2026-06-14    | Engine is single-leg today. |
| `entry_legs.max_legs_per_position`                        | 4                 | assumption    | "Ross often takes a starter and scales up quickly"           | 2026-06-14    | |
| `entry_legs.leg_sizing.starter_fraction`                  | 0.25              | assumption    | Standard Ross starter = ~25%                                 | 2026-06-14    | |
| `entry_legs.re_entry_conditions.fresh_gate_eval_required` | true              | non_negotiable| Engine design invariant                                      | 2026-06-14    | Every leg passes the full gate stack. |
| `entry_legs.exit_modes.full_exit_triggers`                 | (list)            | assumption    | merges current ExitTriggerSet                                | 2026-06-14    | |
| `entry_legs.exit_modes.scale_out_order`                    | fifo              | assumption    | Ross "cool down" exits oldest first                          | 2026-06-14    | |
| `l2_guidelines.big_size_multiplier_of_median`              | 5                 | assumption    | `BIG_BID_IS_SUPPORT` / `BIG_ASK_MAYBE_ICEBERG`              | 2026-06-14    | |
| `l2_guidelines.big_bid_support.score_bonus`                | 5                 | assumption    | Jason: soft-input                                            | 2026-06-14    | |
| `l2_guidelines.big_ask_caution.score_penalty`              | 5                 | assumption    | Jason: soft-input, never hard-block                          | 2026-06-14    | |
| `l2_guidelines.next_visible_bid_stop.enabled`              | false             | assumption    | `NEXT_VISIBLE_BID_IS_NEXT_SUPPORT`                          | 2026-06-14    | Off until implemented in `suggest_stop_price`. |
| `ts_guidelines.big_green_burst.window_seconds`             | 30                | assumption    | `BIG_GREEN_BURST_IS_STRENGTH`                                | 2026-06-14    | Engine doesn't compute this feature yet. |
| `ts_guidelines.big_green_burst.min_large_prints`           | 3                 | assumption    | same                                                         | 2026-06-14    | |
| `ts_guidelines.dont_distinguish_intent`                    | true              | non_negotiable| `DONT_DISTINGUISH_INTENT_ON_THE_TAPE`                       | 2026-06-14    | Design rule: do NOT build short-cover-vs-new-long features. |
| `ts_guidelines.green_volume_below_psych_level.enabled`     | false             | assumption    | `GREEN_TS_VOLUME_BELOW_PSYCH_LEVEL_IS_PRE_BREAK_SIGNAL`     | 2026-06-14    | Composes with `psychological_levels`. |
| `parked.five_minute_setups`                                | parked            | parked        | Jason 2026-06-14                                             | 2026-06-14    | |
| `parked.hold_above_5m_9_ema`                               | parked            | parked        | Jason 2026-06-14                                             | 2026-06-14    | |

---

## Outstanding work (added by 2026-06-14 ingest)

Engine extensions needed before the above can be promoted from documents
to active rules:

- **Psychological-level detection per symbol** (half/whole dollars, prior-day H/L, premarket H) - new feature module + new DTD form fields.
- **Multi-leg position support** - `StarterPosition` / `Position { legs[] }` model; per-leg sizing; FIFO scale-out; per-leg slippage tracking; UI cards.
- **`tape_big_green_burst_30s` feature** - extension of `engine/features.py`.
- **L2-aware stop placement** in `FirstPullbackLong.suggest_stop_price` (uses `l2_guidelines.next_visible_bid_stop`).
- **Consecutive-loss counter** persisted per run + surfaced in `/engine` UI.
- **Soft-score adjuster pipeline** that consumes the new YAML scoring inputs (today there is a hard-coded `BacksideGate` soft score; needs a general soft-score adjuster registry).
- **LLM reasoner layer** (gated, opt-in per run) - reads layers 1-3 + live features + DTD context; can VETO or SIZE-DOWN; CANNOT UPGRADE a blocked trade.
