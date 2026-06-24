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

export interface EngineStatus {
  active: boolean;
  run_id?: number | null;
  status?: string | null;
  symbol?: string | null;
  strategy?: string | null;
  autonomous?: boolean | null;
  quantity?: number | null;
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
    bars_below_vwap_consecutive?: number | null;
    macd_1m_crossed_down_today?: boolean | null;
    failed_setups_today?: number | null;
    last_entry_gate?: {
      passed: boolean | null;
      failures: string[];
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
