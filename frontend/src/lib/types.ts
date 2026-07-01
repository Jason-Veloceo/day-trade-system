export type CandidateStatus = "passed" | "failed_filter" | "stale";

export interface Candidate {
  id: number;
  symbol: string;
  trading_day: string;
  first_alert_at: string;
  last_alert_at: string;
  cooldown_until: string;
  alert_count: number;
  widgets_fired: string[];
  strategies_fired: string[];
  is_5_pillars: boolean;
  last_close_price: string | null;
  last_volume: number | null;
  last_float: number | null;
  last_rel_vol_today: string | null;
  last_rel_vol_5min: string | null;
  last_rel_gap: string | null;
  last_rel_gain: string | null;
  last_short_interest: number | null;
  has_news: boolean;
  latest_newsid: string | null;
  status: CandidateStatus;
  failed_rules: string[];
}

export interface FilterEvaluation {
  rule_key: string;
  passed: boolean;
  observed: unknown;
  threshold: unknown;
}

export interface CandidateDetail extends Candidate {
  evaluations: FilterEvaluation[];
  news_headline: string | null;
  news_storyurl: string | null;
  news_datetime: string | null;
}

export interface Rule {
  id: number;
  rule_key: string;
  field: string;
  op: string;
  value: unknown;
  enabled: boolean;
  severity: string;
  note: string | null;
}

export interface RuleSet {
  id: number;
  name: string;
  is_active: boolean;
  created_at: string;
  note: string | null;
  rules: Rule[];
}

export interface RuleUpdateIn {
  rule_key: string;
  field: string;
  op: string;
  value: unknown;
  enabled: boolean;
  severity: string;
  note?: string | null;
}

export interface RuleSetUpdateIn {
  name: string;
  rules: RuleUpdateIn[];
  note?: string | null;
}

export interface WsMessage {
  topic: string;
  payload: Record<string, unknown>;
}

// ----- engine (POC trading engine) -----

export interface EngineRiskCaps {
  max_trades_per_run: number;
  max_position_value_usd: number;
  max_position_qty: number;
  max_daily_loss_usd: number;
}

export interface EngineDtdContext {
  alert_type?: string | null;
  setup_type?: string | null;
  gap_pct?: number | null;
  float_shares_millions?: number | null;
  rel_vol?: number | null;
  has_news?: boolean | null;
  news_headline?: string | null;
  premarket_high?: number | null;
  dollar_volume_millions?: number | null;
  notes?: string | null;
}

export type EngineSellAnchor = "bid" | "ask";
export type EngineTriggerMode = "pullback_break" | "macd_cross";

// One reason an entry gate said no. Matches the backend's
// `GateFailureCategory` enum + message pair. The UI groups failures by
// `category` so the trader can tell "still warming up" from
// "indicator currently negative" from "backside hard veto" at a
// glance instead of pattern-matching opaque strings.
export type EngineGateFailureCategory =
  | "warmup"
  | "indicator"
  | "vwap"
  | "backside"
  | "trigger"
  | "microstructure";

export interface GateFailure {
  category: EngineGateFailureCategory;
  message: string;
}

// Optional per-engine overrides for the entry-time microstructure gate
// (first_pullback_long only). Unset fields fall through to the strategy
// defaults; auto-armed engines never send this block.
export interface EngineMicrostructureOverrides {
  max_spread_bps?: number;
  max_spread_bps_under_5?: number;
  max_spread_bps_under_20?: number;
  min_bid_ask_imbalance?: number;
  min_tape_buy_pct?: number;
}

export interface EngineStartIn {
  symbol: string;
  strategy_name: string;
  strategy_params: Record<string, unknown>;
  quantity: number;
  autonomous: boolean;
  risk_caps: EngineRiskCaps;
  // v1.1
  order_type: "MKT" | "LMT";
  limit_offset_cents: number;
  sell_anchor: EngineSellAnchor;
  cancel_lmt_after_seconds: number;
  enable_depth: boolean;
  enable_tape: boolean;
  require_5m_macd: boolean;
  dtd_context: EngineDtdContext;
  microstructure?: EngineMicrostructureOverrides | null;
}

export interface EngineFeatureSnapshot {
  ts: string;
  best_bid: number | null;
  best_ask: number | null;
  spread: number | null;
  spread_bps: number | null;
  mid: number | null;
  bid_size_top: number | null;
  ask_size_top: number | null;
  bid_ask_imbalance: number | null;
  ask_wall_price: number | null;
  ask_wall_size: number | null;
  ask_wall_distance_bps: number | null;
  tape_count_60s: number | null;
  tape_buy_volume_60s: number | null;
  tape_sell_volume_60s: number | null;
  tape_buy_pct_60s: number | null;
  tape_speed_30s: number | null;
  tape_speed_decay_pct: number | null;
  has_depth: boolean;
  has_tape: boolean;
}

// Per-engine status (one entry in EngineRegistryStatus.engines).
// The optional `active` field is kept for backwards compatibility with
// the v1.2 single-engine response shape but always true in v1.3+.
export interface EngineStatus {
  active?: boolean;
  run_id: number;
  status: string;
  symbol: string;
  strategy: string;
  autonomous: boolean;
  quantity: number;
  ibkr_account?: string | null;
  order_type?: string | null;
  limit_offset_cents?: number | null;
  sell_anchor?: EngineSellAnchor | null;
  cancel_lmt_after_seconds?: number | null;
  enable_depth?: boolean | null;
  enable_tape?: boolean | null;
  require_5m_macd?: boolean | null;
  dtd_context?: EngineDtdContext | null;
  risk_state?: {
    trades_count: number;
    open_position_qty: number;
    realized_pnl_usd: number;
    kill_switch_on: boolean;
  } | null;
  strategy_state?: {
    name: string;
    params: Record<string, number | string>;
    trigger_mode?: EngineTriggerMode | null;
    in_position: boolean;
    prev_histogram?: number | null;
    macd_line?: number | null;
    macd_signal?: number | null;
    macd_histogram?: number | null;
    macd_1m_hist?: number | null;
    macd_5m_hist?: number | null;
    vwap?: number | null;
    vwap_state?: string | null;
    high_of_day?: number | null;
    // Reference levels carried from the bootstrap replay. `pmhod` is
    // today's premarket high (04:00 - 09:30 ET, updates during
    // premarket); `pdhod` is the most recent prior session's RTH high.
    // Both are nullable when the bootstrap window didn't cover the
    // corresponding period.
    pmhod?: number | null;
    pdhod?: number | null;
    bars_below_vwap_consecutive?: number | null;
    macd_1m_crossed_down_today?: boolean | null;
    failed_setups_today?: number | null;
    last_entry_gate?: {
      passed: boolean | null;
      // The current backend emits `{category, message}` per failure so
      // the UI can group them. Older runs (and the POC
      // macd_crossover_long strategy) emit plain strings. Both shapes
      // are accepted here; the renderer handles both.
      failures: Array<GateFailure | string>;
      notes: Record<string, unknown>;
    } | null;
    last_trigger?: {
      mode: EngineTriggerMode | string;
      fired: boolean | null;
      reason: string | null;
      pullback_test_high: number | null;
      pullback_low: number | null;
      pullback_bar_count: number;
      impulse_bar_count: number;
    } | null;
    config?: Record<string, unknown> | null;
  } | null;
  features?: EngineFeatureSnapshot | null;
  has_pending_approval?: boolean | null;
}

// Portfolio-level risk gate state. Shared across every engine in the
// registry; enforces "1 open position at a time" + daily caps.
export interface EnginePortfolioCaps {
  max_daily_loss_usd: number;
  max_concurrent_engines: number;
  max_total_trades_per_day: number;
}

export interface EnginePortfolioStatus {
  caps: EnginePortfolioCaps;
  holder: string | null;
  is_holding: boolean;
  realized_pnl_usd: number;
  trades_count: number;
  kill_switch_on: boolean;
  day_utc: string | null;
}

export interface EngineSlotsStatus {
  active: number;
  max: number;
}

// Top-level GET /engine/status response (v1.3+). Lists every active
// engine in the registry plus the shared portfolio gate state and the
// slot capacity summary.
export interface EngineRegistryStatus {
  engines: EngineStatus[];
  portfolio: EnginePortfolioStatus;
  slots: EngineSlotsStatus;
}

export interface EngineRun {
  id: number;
  symbol: string;
  instrument_type: string;
  strategy_name: string;
  params: Record<string, unknown>;
  risk_caps: Record<string, unknown>;
  autonomous: boolean;
  market_data_type: string;
  ibkr_client_id: number;
  ibkr_account: string | null;
  status: string;
  started_at: string;
  stopped_at: string | null;
  stop_reason: string | null;
  realized_pnl: string;
  trades_count: number;
  dtd_context: EngineDtdContext;
  order_type: string;
  limit_offset_cents: string;
  sell_anchor: EngineSellAnchor;
  enable_depth: boolean;
  enable_tape: boolean;
}

export interface EngineEvent {
  id: number;
  run_id: number;
  ts: string;
  event_type: string;
  payload: Record<string, unknown>;
}
