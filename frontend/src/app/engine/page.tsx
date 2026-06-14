"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import useSWR from "swr";
import { approveEngine, fetcher, rejectEngine, startEngine, stopEngine } from "@/lib/api";
import type {
  EngineDtdContext,
  EngineFeatureSnapshot,
  EngineRun,
  EngineStartIn,
  EngineStatus,
  WsMessage,
} from "@/lib/types";
import { useBrokerStream } from "@/lib/ws";
import { EngineChart } from "@/components/EngineChart";

const ENGINE_TOPICS = new Set([
  "engine.bar",
  "engine.indicator",
  "engine.signal",
  "engine.approval_needed",
  "engine.position",
  "engine.fill",
  "engine.pnl",
  "engine.error",
  "engine.run_state",
  "engine.depth",
  "engine.tape",
  "engine.features",
]);

const EVENT_BADGE: Record<string, string> = {
  bar: "bg-neutral-100 text-neutral-700 ring-neutral-200",
  indicator: "bg-neutral-100 text-neutral-700 ring-neutral-200",
  signal: "bg-sky-50 text-sky-700 ring-sky-200",
  decision: "bg-sky-50 text-sky-700 ring-sky-200",
  ready_for_approval: "bg-amber-50 text-amber-700 ring-amber-200",
  approval_granted: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  approval_rejected: "bg-rose-50 text-rose-700 ring-rose-200",
  order_submit: "bg-violet-50 text-violet-700 ring-violet-200",
  order_status: "bg-violet-50 text-violet-700 ring-violet-200",
  fill: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  slippage: "bg-amber-50 text-amber-700 ring-amber-200",
  position_open: "bg-sky-50 text-sky-700 ring-sky-200",
  position_close: "bg-sky-50 text-sky-700 ring-sky-200",
  risk_block: "bg-rose-50 text-rose-700 ring-rose-200",
  error: "bg-rose-50 text-rose-700 ring-rose-200",
  engine_start: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  engine_stop: "bg-neutral-100 text-neutral-700 ring-neutral-200",
  ibkr_connected: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  ibkr_disconnected: "bg-rose-50 text-rose-700 ring-rose-200",
  exit_trigger: "bg-rose-50 text-rose-700 ring-rose-200",
  depth_update: "bg-neutral-100 text-neutral-600 ring-neutral-200",
  tape_print: "bg-neutral-100 text-neutral-600 ring-neutral-200",
  feature_snapshot: "bg-neutral-100 text-neutral-600 ring-neutral-200",
};

const DEFAULT_DTD: EngineDtdContext = {
  alert_type: "",
  setup_type: "first_pullback",
  gap_pct: null,
  float_shares_millions: null,
  rel_vol: null,
  has_news: false,
  news_headline: "",
  premarket_high: null,
  dollar_volume_millions: null,
  notes: "",
};

const DEFAULT_START: EngineStartIn = {
  symbol: "EUR.USD",
  strategy_name: "first_pullback_long",
  strategy_params: {
    macd_fast: 12,
    macd_slow: 26,
    macd_signal: 9,
    trigger_mode: "pullback_break",
  },
  quantity: 25000,
  autonomous: false,
  risk_caps: {
    max_trades_per_run: 5,
    max_position_value_usd: 30000,
    max_position_qty: 100000,
    max_daily_loss_usd: 150,
  },
  order_type: "LMT",
  limit_offset_cents: 10,
  sell_anchor: "bid",
  cancel_lmt_after_seconds: 3,
  enable_depth: false,
  enable_tape: false,
  dtd_context: DEFAULT_DTD,
};

export default function EnginePage() {
  const { data: status, mutate: refetchStatus } = useSWR<EngineStatus>(
    "/engine/status",
    fetcher,
    { revalidateOnFocus: false, refreshInterval: 2000 }
  );
  const { data: runs } = useSWR<EngineRun[]>("/engine/runs?limit=20", fetcher, {
    revalidateOnFocus: false,
    refreshInterval: 5000,
  });
  const { messages, connected } = useBrokerStream({ bufferSize: 600 });

  const [form, setForm] = useState<EngineStartIn>(DEFAULT_START);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const engineEvents = useMemo(
    () => messages.filter((m) => ENGINE_TOPICS.has(m.topic)),
    [messages]
  );

  const lastEventIdRef = useRef<number>(-1);
  useEffect(() => {
    if (engineEvents.length === 0) return;
    if (engineEvents.length === lastEventIdRef.current) return;
    lastEventIdRef.current = engineEvents.length;
    refetchStatus();
  }, [engineEvents.length, refetchStatus]);

  const active = !!status?.active;

  async function handleStart() {
    setBusy(true);
    setErr(null);
    try {
      await startEngine(form);
      await refetchStatus();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function handleStop() {
    setBusy(true);
    try {
      await stopEngine();
      await refetchStatus();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function handleApprove() {
    setBusy(true);
    try {
      await approveEngine();
      await refetchStatus();
    } finally {
      setBusy(false);
    }
  }

  async function handleReject() {
    setBusy(true);
    try {
      await rejectEngine();
      await refetchStatus();
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="space-y-6">
      <div className="flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-neutral-900">Trading engine</h1>
          <p className="mt-1 text-sm text-neutral-500">
            Semi-automated FirstPullback / MACD-crossover engine. You arm the symbol (with DTD
            context), the engine watches the gate stack (5m + 1m MACD, VWAP, backside, L2/T&S)
            and submits paper orders. Paper-only by hard config.
          </p>
        </div>
        <span
          className={
            "inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium ring-1 " +
            (connected
              ? "bg-emerald-50 text-emerald-700 ring-emerald-200"
              : "bg-neutral-100 text-neutral-600 ring-neutral-200")
          }
        >
          <span
            className={
              "h-1.5 w-1.5 rounded-full " + (connected ? "bg-emerald-500" : "bg-neutral-400")
            }
          />
          WS {connected ? "connected" : "reconnecting…"}
        </span>
      </div>

      <PendingApprovalBanner
        status={status}
        onApprove={handleApprove}
        onReject={handleReject}
        busy={busy}
      />

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <Card title={active ? "Active run" : "Arm a symbol"}>
          {active ? (
            <ActiveRunPanel status={status!} onStop={handleStop} busy={busy} />
          ) : (
            <StartForm form={form} setForm={setForm} onStart={handleStart} busy={busy} err={err} />
          )}
        </Card>

        <Card title="Strategy state">
          <StrategyStatePanel status={status} />
        </Card>
      </div>

      {active ? (
        <Card title="Live features (L2 / T&S / VWAP)">
          <FeaturePanel
            features={status?.features ?? null}
            enableDepth={status?.enable_depth ?? false}
            enableTape={status?.enable_tape ?? false}
          />
        </Card>
      ) : null}

      {active && status?.run_id ? (
        <Card title="Chart">
          <EngineChart
            runId={status.run_id}
            symbol={status.symbol ?? ""}
            events={engineEvents}
          />
        </Card>
      ) : null}

      <Card title="Live event log">
        <EventLog events={engineEvents} />
      </Card>

      <Card title="Recent runs">
        <RecentRuns runs={runs} />
      </Card>
    </section>
  );
}

// ---------- subcomponents ----------

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-neutral-200 bg-white shadow-sm">
      <div className="border-b border-neutral-200 px-5 py-3">
        <h2 className="text-sm font-semibold text-neutral-700">{title}</h2>
      </div>
      <div className="px-5 py-4">{children}</div>
    </div>
  );
}

function PendingApprovalBanner({
  status,
  onApprove,
  onReject,
  busy,
}: {
  status: EngineStatus | undefined;
  onApprove: () => void;
  onReject: () => void;
  busy: boolean;
}) {
  if (!status?.has_pending_approval) return null;
  return (
    <div className="rounded-xl border border-amber-200 bg-amber-50 px-5 py-4 shadow-sm">
      <div className="flex items-center justify-between gap-4">
        <div>
          <div className="text-sm font-semibold text-amber-900">Signal awaiting approval</div>
          <div className="mt-0.5 text-xs text-amber-700">
            A trade signal has been generated and is parked. Approve to submit the order, reject
            to drop it. The engine remains armed either way.
          </div>
        </div>
        <div className="flex gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={onReject}
            className="rounded-md border border-rose-200 bg-white px-4 py-2 text-sm font-medium text-rose-700 hover:bg-rose-50 disabled:opacity-50"
          >
            Reject
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={onApprove}
            className="rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-700 disabled:opacity-50"
          >
            Approve
          </button>
        </div>
      </div>
    </div>
  );
}

function StartForm({
  form,
  setForm,
  onStart,
  busy,
  err,
}: {
  form: EngineStartIn;
  setForm: (s: EngineStartIn) => void;
  onStart: () => void;
  busy: boolean;
  err: string | null;
}) {
  const isFirstPullback = form.strategy_name === "first_pullback_long";
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        onStart();
      }}
      className="space-y-4"
    >
      <div className="grid grid-cols-2 gap-4">
        <Field label="Symbol">
          <input
            type="text"
            value={form.symbol}
            onChange={(e) => setForm({ ...form, symbol: e.target.value.toUpperCase() })}
            placeholder="e.g. SPY or EUR.USD"
            className="w-full rounded-md border border-neutral-300 bg-white px-3 py-2 text-sm text-neutral-900 placeholder-neutral-400 focus:border-neutral-900 focus:outline-none"
          />
          <Hint>
            Stock tickers (e.g. <code>SPY</code>) route to SMART/USD. Forex tickers (e.g.{" "}
            <code>EUR.USD</code>) route to IDEALPRO.
          </Hint>
        </Field>
        <Field label="Quantity">
          <input
            type="number"
            value={form.quantity}
            min={1}
            onChange={(e) => setForm({ ...form, quantity: Number(e.target.value) })}
            className="w-full rounded-md border border-neutral-300 bg-white px-3 py-2 text-sm text-neutral-900 focus:border-neutral-900 focus:outline-none"
          />
          <Hint>Shares for stocks; base-currency units for forex (EUR.USD min 25,000).</Hint>
        </Field>
      </div>

      <Field label="Strategy">
        <select
          value={form.strategy_name}
          onChange={(e) => {
            const name = e.target.value;
            const params =
              name === "first_pullback_long"
                ? {
                    macd_fast: 12,
                    macd_slow: 26,
                    macd_signal: 9,
                    trigger_mode: "pullback_break",
                  }
                : { fast: 12, slow: 26, signal: 9 };
            setForm({ ...form, strategy_name: name, strategy_params: params });
          }}
          className="w-full rounded-md border border-neutral-300 bg-white px-3 py-2 text-sm text-neutral-900 focus:border-neutral-900 focus:outline-none"
        >
          <option value="first_pullback_long">
            FirstPullback long (5m+1m MACD, VWAP, backside, L2/T&S) — v1.1
          </option>
          <option value="macd_crossover_long">MACD crossover (long-only, POC)</option>
        </select>
      </Field>

      {isFirstPullback ? (
        <Field label="Entry trigger">
          <select
            value={(form.strategy_params.trigger_mode as string) ?? "pullback_break"}
            onChange={(e) =>
              setForm({
                ...form,
                strategy_params: {
                  ...form.strategy_params,
                  trigger_mode: e.target.value,
                },
              })
            }
            className="w-full rounded-md border border-neutral-300 bg-white px-3 py-2 text-sm focus:border-neutral-900 focus:outline-none"
          >
            <option value="pullback_break">
              Pullback breakout — first 1m candle to make a new high after the last
              red-candle pullback (Ross-style)
            </option>
            <option value="macd_cross">
              MACD cross-up — 1m histogram crosses ≥ 0 or is positive-and-rising
            </option>
          </select>
          <Hint>
            <strong>pullback_break</strong> looks for a small red-candle pullback (1–3
            bars), then fires when the next green bar's high exceeds the high of the
            most-recent (smaller) red. The pullback low is used as the suggested stop.
            5m MACD must still be positive-and-not-falling, and 1m MACD must be
            positive (context only — not the trigger). <strong>macd_cross</strong>{" "}
            uses the indicator as the trigger.
          </Hint>
        </Field>
      ) : null}

      {/* Order routing */}
      <div className="rounded-md border border-neutral-200 bg-neutral-50 px-3 py-3">
        <div className="text-xs font-semibold text-neutral-600">Order routing</div>
        <div className="mt-2 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Field label="Order type">
            <select
              value={form.order_type}
              onChange={(e) =>
                setForm({ ...form, order_type: e.target.value as "MKT" | "LMT" })
              }
              className="w-full rounded-md border border-neutral-300 bg-white px-3 py-2 text-sm focus:border-neutral-900 focus:outline-none"
            >
              <option value="LMT">LMT (marketable)</option>
              <option value="MKT">MKT</option>
            </select>
          </Field>
          <Field label="Offset (cents)">
            <input
              type="number"
              value={form.limit_offset_cents}
              min={0}
              step={1}
              disabled={form.order_type !== "LMT"}
              onChange={(e) =>
                setForm({ ...form, limit_offset_cents: Number(e.target.value) })
              }
              className="w-full rounded-md border border-neutral-300 bg-white px-3 py-2 text-sm focus:border-neutral-900 focus:outline-none disabled:bg-neutral-100"
            />
          </Field>
          <Field label="Sell anchor">
            <select
              value={form.sell_anchor}
              onChange={(e) =>
                setForm({
                  ...form,
                  sell_anchor: e.target.value as "bid" | "ask",
                })
              }
              disabled={form.order_type !== "LMT"}
              className="w-full rounded-md border border-neutral-300 bg-white px-3 py-2 text-sm focus:border-neutral-900 focus:outline-none disabled:bg-neutral-100"
            >
              <option value="bid">bid − offset (aggressive)</option>
              <option value="ask">ask − offset (passive)</option>
            </select>
          </Field>
          <Field label="Cancel-after (sec)">
            <input
              type="number"
              value={form.cancel_lmt_after_seconds}
              min={0.5}
              step={0.5}
              disabled={form.order_type !== "LMT"}
              onChange={(e) =>
                setForm({ ...form, cancel_lmt_after_seconds: Number(e.target.value) })
              }
              className="w-full rounded-md border border-neutral-300 bg-white px-3 py-2 text-sm focus:border-neutral-900 focus:outline-none disabled:bg-neutral-100"
            />
          </Field>
        </div>
        <Hint>
          BUY is always anchored to the ask: <code>LMT @ ask + offset</code>. SELL is
          anchored per <em>Sell anchor</em>: <code>LMT @ bid − offset</code>{" "}
          (aggressive, matches your "Sell at Bid" hotkeys — used for most exits) or{" "}
          <code>LMT @ ask − offset</code> (passive, matches your "Sell at Ask"
          hotkeys — better fills when you're not in a hurry). Unfilled orders
          auto-cancel after the configured timeout and the engine re-evaluates.
        </Hint>
      </div>

      {/* L2 / T&S subscriptions */}
      <div className="rounded-md border border-neutral-200 bg-neutral-50 px-3 py-3">
        <div className="text-xs font-semibold text-neutral-600">Market data subscriptions</div>
        <div className="mt-2 grid grid-cols-2 gap-3">
          <Toggle
            label="L2 depth (reqMktDepth)"
            checked={form.enable_depth}
            onChange={(v) => setForm({ ...form, enable_depth: v })}
            hint="Subscribes to 10-level book. Needs NASDAQ TotalView or NYSE ArcaBook entitlements for US small caps."
          />
          <Toggle
            label="T&S (tickByTick AllLast)"
            checked={form.enable_tape}
            onChange={(v) => setForm({ ...form, enable_tape: v })}
            hint="Subscribes to per-print tape. Required for tape-flip exit trigger and buy% gate."
          />
        </div>
      </div>

      {/* DTD context (only for first_pullback) */}
      {isFirstPullback ? <DtdContextFields form={form} setForm={setForm} /> : null}

      {/* Risk caps */}
      <div className="rounded-md border border-neutral-200 bg-neutral-50 px-3 py-3">
        <div className="text-xs font-semibold text-neutral-600">Risk caps</div>
        <div className="mt-2 grid grid-cols-2 gap-3">
          <Field label="Max trades / run">
            <input
              type="number"
              value={form.risk_caps.max_trades_per_run}
              onChange={(e) =>
                setForm({
                  ...form,
                  risk_caps: { ...form.risk_caps, max_trades_per_run: Number(e.target.value) },
                })
              }
              className="w-full rounded-md border border-neutral-300 bg-white px-3 py-2 text-sm focus:border-neutral-900 focus:outline-none"
            />
          </Field>
          <Field label="Max daily loss ($)">
            <input
              type="number"
              value={form.risk_caps.max_daily_loss_usd}
              onChange={(e) =>
                setForm({
                  ...form,
                  risk_caps: { ...form.risk_caps, max_daily_loss_usd: Number(e.target.value) },
                })
              }
              className="w-full rounded-md border border-neutral-300 bg-white px-3 py-2 text-sm focus:border-neutral-900 focus:outline-none"
            />
          </Field>
          <Field label="Max position value ($)">
            <input
              type="number"
              value={form.risk_caps.max_position_value_usd}
              onChange={(e) =>
                setForm({
                  ...form,
                  risk_caps: {
                    ...form.risk_caps,
                    max_position_value_usd: Number(e.target.value),
                  },
                })
              }
              className="w-full rounded-md border border-neutral-300 bg-white px-3 py-2 text-sm focus:border-neutral-900 focus:outline-none"
            />
          </Field>
          <Field label="Max position qty">
            <input
              type="number"
              value={form.risk_caps.max_position_qty}
              onChange={(e) =>
                setForm({
                  ...form,
                  risk_caps: { ...form.risk_caps, max_position_qty: Number(e.target.value) },
                })
              }
              className="w-full rounded-md border border-neutral-300 bg-white px-3 py-2 text-sm focus:border-neutral-900 focus:outline-none"
            />
          </Field>
        </div>
      </div>

      <label className="flex items-start gap-3 rounded-md border border-neutral-200 bg-neutral-50 px-3 py-3">
        <input
          type="checkbox"
          checked={form.autonomous}
          onChange={(e) => setForm({ ...form, autonomous: e.target.checked })}
          className="mt-0.5 h-4 w-4 rounded border-neutral-300"
        />
        <span className="text-sm">
          <span className="font-medium text-neutral-900">Run autonomously (auto re-arm)</span>
          <span className="ml-2 text-neutral-500">
            (no per-signal approval; entries fire automatically as the strategy emits them — paper
            only). Either mode auto re-arms after each exit until you Stop.
          </span>
        </span>
      </label>

      {err && (
        <div className="rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
          {err}
        </div>
      )}

      <button
        type="submit"
        disabled={busy}
        className="w-full rounded-md bg-neutral-900 px-4 py-2.5 text-sm font-semibold text-white hover:bg-neutral-800 disabled:opacity-50"
      >
        {busy ? "Arming…" : "Arm engine"}
      </button>
    </form>
  );
}

function DtdContextFields({
  form,
  setForm,
}: {
  form: EngineStartIn;
  setForm: (s: EngineStartIn) => void;
}) {
  const ctx = form.dtd_context;
  const set = (patch: Partial<EngineDtdContext>) =>
    setForm({ ...form, dtd_context: { ...ctx, ...patch } });

  return (
    <div className="rounded-md border border-neutral-200 bg-neutral-50 px-3 py-3">
      <div className="text-xs font-semibold text-neutral-600">
        DTD context (persisted with the run)
      </div>
      <div className="mt-2 grid grid-cols-2 gap-3">
        <Field label="Setup type">
          <select
            value={ctx.setup_type ?? ""}
            onChange={(e) => set({ setup_type: e.target.value || null })}
            className="w-full rounded-md border border-neutral-300 bg-white px-3 py-2 text-sm focus:border-neutral-900 focus:outline-none"
          >
            <option value="">—</option>
            <option value="first_pullback">first_pullback</option>
            <option value="micro_pullback">micro_pullback</option>
            <option value="bull_flag">bull_flag</option>
            <option value="hod_break">hod_break</option>
            <option value="flat_top_breakout">flat_top_breakout</option>
            <option value="abcd_continuation">abcd_continuation</option>
          </select>
        </Field>
        <Field label="DTD alert type">
          <input
            type="text"
            value={ctx.alert_type ?? ""}
            placeholder="e.g. Momo / New High"
            onChange={(e) => set({ alert_type: e.target.value || null })}
            className="w-full rounded-md border border-neutral-300 bg-white px-3 py-2 text-sm focus:border-neutral-900 focus:outline-none"
          />
        </Field>
        <Field label="Gap %">
          <input
            type="number"
            step="0.1"
            value={ctx.gap_pct ?? ""}
            onChange={(e) =>
              set({ gap_pct: e.target.value === "" ? null : Number(e.target.value) })
            }
            className="w-full rounded-md border border-neutral-300 bg-white px-3 py-2 text-sm focus:border-neutral-900 focus:outline-none"
          />
        </Field>
        <Field label="Float (millions)">
          <input
            type="number"
            step="0.1"
            value={ctx.float_shares_millions ?? ""}
            onChange={(e) =>
              set({
                float_shares_millions:
                  e.target.value === "" ? null : Number(e.target.value),
              })
            }
            className="w-full rounded-md border border-neutral-300 bg-white px-3 py-2 text-sm focus:border-neutral-900 focus:outline-none"
          />
        </Field>
        <Field label="Rel vol (x)">
          <input
            type="number"
            step="0.1"
            value={ctx.rel_vol ?? ""}
            onChange={(e) =>
              set({ rel_vol: e.target.value === "" ? null : Number(e.target.value) })
            }
            className="w-full rounded-md border border-neutral-300 bg-white px-3 py-2 text-sm focus:border-neutral-900 focus:outline-none"
          />
        </Field>
        <Field label="$ volume (millions)">
          <input
            type="number"
            step="0.1"
            value={ctx.dollar_volume_millions ?? ""}
            onChange={(e) =>
              set({
                dollar_volume_millions:
                  e.target.value === "" ? null : Number(e.target.value),
              })
            }
            className="w-full rounded-md border border-neutral-300 bg-white px-3 py-2 text-sm focus:border-neutral-900 focus:outline-none"
          />
        </Field>
        <Field label="Premarket high">
          <input
            type="number"
            step="0.01"
            value={ctx.premarket_high ?? ""}
            onChange={(e) =>
              set({ premarket_high: e.target.value === "" ? null : Number(e.target.value) })
            }
            className="w-full rounded-md border border-neutral-300 bg-white px-3 py-2 text-sm focus:border-neutral-900 focus:outline-none"
          />
        </Field>
        <Toggle
          label="Has news / catalyst"
          checked={!!ctx.has_news}
          onChange={(v) => set({ has_news: v })}
        />
      </div>
      <div className="mt-3">
        <Field label="News headline">
          <input
            type="text"
            value={ctx.news_headline ?? ""}
            onChange={(e) => set({ news_headline: e.target.value || null })}
            placeholder="(optional)"
            className="w-full rounded-md border border-neutral-300 bg-white px-3 py-2 text-sm focus:border-neutral-900 focus:outline-none"
          />
        </Field>
      </div>
      <div className="mt-3">
        <Field label="Notes">
          <textarea
            value={ctx.notes ?? ""}
            onChange={(e) => set({ notes: e.target.value || null })}
            rows={2}
            placeholder="anything you want preserved with this run"
            className="w-full rounded-md border border-neutral-300 bg-white px-3 py-2 text-sm focus:border-neutral-900 focus:outline-none"
          />
        </Field>
      </div>
    </div>
  );
}

function ActiveRunPanel({
  status,
  onStop,
  busy,
}: {
  status: EngineStatus;
  onStop: () => void;
  busy: boolean;
}) {
  const rs = status.risk_state;
  const ctx = status.dtd_context;
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-3 text-sm">
        <KV label="Run ID" value={String(status.run_id ?? "-")} />
        <KV label="Status" value={status.status ?? "-"} />
        <KV label="Symbol" value={status.symbol ?? "-"} />
        <KV label="Strategy" value={status.strategy ?? "-"} />
        <KV label="Quantity" value={String(status.quantity ?? "-")} />
        <KV
          label="Mode"
          value={status.autonomous ? "Autonomous" : "Manual approval"}
          accent={status.autonomous ? "amber" : "neutral"}
        />
        <KV
          label="Order type"
          value={`${status.order_type ?? "MKT"}${
            status.order_type === "LMT" && status.limit_offset_cents !== null
              ? ` (+${status.limit_offset_cents}c)`
              : ""
          }`}
        />
        <KV
          label="Sell anchor"
          value={
            status.order_type === "LMT"
              ? `${status.sell_anchor ?? "bid"} − ${status.limit_offset_cents ?? "?"}c`
              : "n/a"
          }
        />
        <KV label="IBKR account" value={status.ibkr_account ?? "-"} />
        <KV
          label="L2 / T&S"
          value={`depth=${status.enable_depth ? "on" : "off"}, tape=${
            status.enable_tape ? "on" : "off"
          }`}
        />
        <KV
          label="Open position"
          value={String(rs?.open_position_qty ?? 0)}
          accent={(rs?.open_position_qty ?? 0) > 0 ? "sky" : "neutral"}
        />
        <KV label="Trades count" value={String(rs?.trades_count ?? 0)} />
        <KV label="Realised P&L" value={`$${(rs?.realized_pnl_usd ?? 0).toFixed(2)}`} />
        <KV
          label="Kill switch"
          value={rs?.kill_switch_on ? "ENGAGED" : "off"}
          accent={rs?.kill_switch_on ? "rose" : "neutral"}
        />
      </div>

      {ctx && Object.keys(ctx).length > 0 ? (
        <div className="rounded-md border border-neutral-200 bg-neutral-50 px-3 py-3 text-xs">
          <div className="font-semibold uppercase tracking-wide text-neutral-500">
            DTD context for this run
          </div>
          <div className="mt-1 grid grid-cols-2 gap-x-4 gap-y-1 text-neutral-700">
            {ctx.setup_type ? <span>setup: {ctx.setup_type}</span> : null}
            {ctx.alert_type ? <span>alert: {ctx.alert_type}</span> : null}
            {ctx.gap_pct !== null && ctx.gap_pct !== undefined ? (
              <span>gap: {ctx.gap_pct}%</span>
            ) : null}
            {ctx.float_shares_millions ? (
              <span>float: {ctx.float_shares_millions}M</span>
            ) : null}
            {ctx.rel_vol ? <span>rel vol: {ctx.rel_vol}x</span> : null}
            {ctx.dollar_volume_millions ? (
              <span>$vol: {ctx.dollar_volume_millions}M</span>
            ) : null}
            {ctx.premarket_high ? <span>PMH: {ctx.premarket_high}</span> : null}
            {ctx.has_news ? <span>news: yes</span> : null}
          </div>
          {ctx.news_headline ? (
            <div className="mt-1 text-neutral-600">“{ctx.news_headline}”</div>
          ) : null}
          {ctx.notes ? <div className="mt-1 text-neutral-600">{ctx.notes}</div> : null}
        </div>
      ) : null}

      <button
        type="button"
        disabled={busy}
        onClick={onStop}
        className="w-full rounded-md border border-rose-200 bg-white px-4 py-2.5 text-sm font-semibold text-rose-700 hover:bg-rose-50 disabled:opacity-50"
      >
        Stop engine
      </button>
    </div>
  );
}

function StrategyStatePanel({ status }: { status: EngineStatus | undefined }) {
  const ss = status?.strategy_state;
  if (!ss) {
    return <p className="text-sm text-neutral-500">No active run. Strategy state will appear here.</p>;
  }
  const fmt = (n: number | null | undefined) =>
    n === null || n === undefined ? "—" : n.toFixed(6);
  const fmt2 = (n: number | null | undefined) =>
    n === null || n === undefined ? "—" : n.toFixed(4);
  const macd1m = ss.macd_1m_hist ?? ss.macd_histogram;
  const macd5m = ss.macd_5m_hist;
  return (
    <div className="space-y-3 text-sm">
      <KV label="Strategy" value={ss.name} />
      <KV
        label="In position"
        value={ss.in_position ? "yes" : "no"}
        accent={ss.in_position ? "sky" : "neutral"}
      />
      <div className="grid grid-cols-3 gap-3">
        <Tile
          label="1m hist"
          value={fmt(macd1m)}
          accent={
            macd1m === null || macd1m === undefined
              ? "neutral"
              : macd1m > 0
              ? "emerald"
              : "rose"
          }
        />
        <Tile
          label="5m hist"
          value={fmt(macd5m)}
          accent={
            macd5m === null || macd5m === undefined
              ? "neutral"
              : macd5m > 0
              ? "emerald"
              : "rose"
          }
        />
        <Tile
          label="VWAP"
          value={fmt2(ss.vwap)}
          accent={
            ss.vwap_state === "above"
              ? "emerald"
              : ss.vwap_state === "below"
              ? "rose"
              : "neutral"
          }
        />
      </div>
      {ss.last_trigger ? (
        <div className="rounded-md border border-neutral-200 bg-neutral-50 px-3 py-2 text-xs">
          <div className="font-semibold uppercase tracking-wide text-neutral-500">
            Last trigger ({ss.last_trigger.mode})
          </div>
          <div className="mt-1 grid grid-cols-2 gap-x-3 gap-y-1">
            <div>
              <span className="text-neutral-500">Fired:</span>{" "}
              {ss.last_trigger.fired === null ? (
                <span className="text-neutral-400">—</span>
              ) : ss.last_trigger.fired ? (
                <span className="text-emerald-700">yes</span>
              ) : (
                <span className="text-neutral-600">no</span>
              )}
            </div>
            {ss.last_trigger.mode === "pullback_break" ? (
              <>
                <div>
                  <span className="text-neutral-500">Test high:</span>{" "}
                  {fmt2(ss.last_trigger.pullback_test_high)}
                </div>
                <div>
                  <span className="text-neutral-500">Pullback low:</span>{" "}
                  {fmt2(ss.last_trigger.pullback_low)}
                </div>
                <div>
                  <span className="text-neutral-500">
                    Pullback / impulse bars:
                  </span>{" "}
                  {ss.last_trigger.pullback_bar_count} /{" "}
                  {ss.last_trigger.impulse_bar_count}
                </div>
              </>
            ) : null}
          </div>
          {ss.last_trigger.reason ? (
            <div className="mt-1 text-neutral-600">
              <span className="text-neutral-500">Reason:</span>{" "}
              {ss.last_trigger.reason}
            </div>
          ) : null}
        </div>
      ) : null}
      {ss.last_entry_gate ? (
        <div className="rounded-md border border-neutral-200 bg-neutral-50 px-3 py-2 text-xs">
          <div className="font-semibold uppercase tracking-wide text-neutral-500">
            Last entry gate eval
          </div>
          <div className="mt-1">
            {ss.last_entry_gate.passed ? (
              <span className="text-emerald-700">PASSED</span>
            ) : ss.last_entry_gate.failures.length > 0 ? (
              <ul className="list-disc pl-4 text-rose-700">
                {ss.last_entry_gate.failures.map((f: string, i: number) => (
                  <li key={i}>{f}</li>
                ))}
              </ul>
            ) : (
              <span className="text-neutral-600">no eval yet</span>
            )}
          </div>
        </div>
      ) : null}
      {ss.macd_1m_crossed_down_today ? (
        <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
          ⚠ 1m MACD already crossed down today (backside-of-move latch).
        </div>
      ) : null}
    </div>
  );
}

function FeaturePanel({
  features,
  enableDepth,
  enableTape,
}: {
  features: EngineFeatureSnapshot | null;
  enableDepth: boolean;
  enableTape: boolean;
}) {
  if (!features) {
    return (
      <p className="text-sm text-neutral-500">
        No feature snapshot yet. Make sure depth/tape are enabled if you want L2/T&S features.
      </p>
    );
  }
  const f = features;
  return (
    <div className="space-y-4 text-sm">
      <div className="grid grid-cols-4 gap-3">
        <Tile label="Best bid" value={fmt4(f.best_bid)} />
        <Tile label="Best ask" value={fmt4(f.best_ask)} />
        <Tile
          label="Spread (bps)"
          value={fmt2(f.spread_bps)}
          accent={
            f.spread_bps === null
              ? "neutral"
              : f.spread_bps > 50
              ? "rose"
              : f.spread_bps > 20
              ? "amber"
              : "emerald"
          }
        />
        <Tile
          label="Imbalance (bid share)"
          value={fmtPct(f.bid_ask_imbalance)}
          accent={
            f.bid_ask_imbalance === null
              ? "neutral"
              : f.bid_ask_imbalance >= 0.55
              ? "emerald"
              : f.bid_ask_imbalance <= 0.4
              ? "rose"
              : "amber"
          }
        />
      </div>
      <div className="grid grid-cols-4 gap-3">
        <Tile label="Ask wall px" value={fmt4(f.ask_wall_price)} />
        <Tile label="Ask wall sz" value={fmt0(f.ask_wall_size)} />
        <Tile label="Wall dist (bps)" value={fmt2(f.ask_wall_distance_bps)} />
        <Tile label="Mid" value={fmt4(f.mid)} />
      </div>
      <div className="grid grid-cols-4 gap-3">
        <Tile
          label="Tape buy%"
          value={fmtPct(f.tape_buy_pct_60s)}
          accent={
            f.tape_buy_pct_60s === null
              ? "neutral"
              : f.tape_buy_pct_60s >= 0.55
              ? "emerald"
              : f.tape_buy_pct_60s <= 0.4
              ? "rose"
              : "amber"
          }
        />
        <Tile label="Tape speed 30s" value={fmt2(f.tape_speed_30s)} />
        <Tile
          label="Speed decay"
          value={fmtPct(f.tape_speed_decay_pct)}
          accent={
            f.tape_speed_decay_pct === null
              ? "neutral"
              : f.tape_speed_decay_pct >= -0.2
              ? "emerald"
              : f.tape_speed_decay_pct >= -0.5
              ? "amber"
              : "rose"
          }
        />
        <Tile label="Prints 60s" value={String(f.tape_count_60s ?? "—")} />
      </div>
      <div className="rounded-md border border-neutral-200 bg-neutral-50 px-3 py-2 text-xs text-neutral-600">
        Subscriptions: depth={enableDepth ? "on" : "off"} (
        {f.has_depth ? "data flowing" : "no data"}), tape={enableTape ? "on" : "off"} (
        {f.has_tape ? "data flowing" : "no data"}). For forex on IDEALPRO the depth/tape data
        is sparse - features will read N/A.
      </div>
    </div>
  );
}

function fmt4(n: number | null | undefined): string {
  return n === null || n === undefined ? "—" : n.toFixed(4);
}
function fmt2(n: number | null | undefined): string {
  return n === null || n === undefined ? "—" : n.toFixed(2);
}
function fmt0(n: number | null | undefined): string {
  return n === null || n === undefined ? "—" : Math.round(n).toLocaleString();
}
function fmtPct(n: number | null | undefined): string {
  return n === null || n === undefined ? "—" : `${(n * 100).toFixed(1)}%`;
}

const FILTERS = [
  "all",
  "bar",
  "signal",
  "decision",
  "fill",
  "exit_trigger",
  "risk_block",
  "error",
] as const;
type FilterKey = (typeof FILTERS)[number];

function EventLog({ events }: { events: WsMessage[] }) {
  const [filter, setFilter] = useState<FilterKey>("all");
  const filtered = useMemo(() => {
    if (filter === "all") return events;
    return events.filter((m) => (m.payload?.event_type ?? "") === filter);
  }, [events, filter]);

  const reversed = [...filtered].reverse();

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-2">
        {FILTERS.map((f) => (
          <button
            key={f}
            type="button"
            onClick={() => setFilter(f)}
            className={
              "rounded-full px-3 py-1 text-xs font-medium ring-1 " +
              (filter === f
                ? "bg-neutral-900 text-white ring-neutral-900"
                : "bg-white text-neutral-700 ring-neutral-300 hover:bg-neutral-50")
            }
          >
            {f}
          </button>
        ))}
        <span className="ml-auto text-xs text-neutral-500">
          {filtered.length} {filtered.length === 1 ? "event" : "events"} (most recent first)
        </span>
      </div>
      <div className="max-h-[480px] overflow-y-auto rounded-md border border-neutral-200">
        {reversed.length === 0 ? (
          <div className="px-4 py-6 text-center text-sm text-neutral-500">
            No engine events yet. Arm a symbol to see live events.
          </div>
        ) : (
          <ul className="divide-y divide-neutral-100">
            {reversed.map((m, idx) => (
              <EventRow key={idx} msg={m} />
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function EventRow({ msg }: { msg: WsMessage }) {
  const eventType =
    typeof msg.payload?.event_type === "string" ? (msg.payload.event_type as string) : "unknown";
  const ts = typeof msg.payload?.ts === "string" ? (msg.payload.ts as string) : "";
  const inner =
    msg.payload?.payload && typeof msg.payload.payload === "object"
      ? (msg.payload.payload as Record<string, unknown>)
      : {};

  const badgeClass =
    EVENT_BADGE[eventType] ?? "bg-neutral-100 text-neutral-700 ring-neutral-200";

  return (
    <li className="px-4 py-2.5 text-sm">
      <div className="flex items-center gap-3">
        <span
          className={
            "inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ring-1 " +
            badgeClass
          }
        >
          {eventType}
        </span>
        <span className="font-mono text-xs text-neutral-500">{formatTs(ts)}</span>
        <span className="flex-1 truncate text-neutral-800">{summarise(eventType, inner)}</span>
      </div>
    </li>
  );
}

function summarise(type: string, p: Record<string, unknown>): string {
  switch (type) {
    case "bar": {
      const close = p.close as number | undefined;
      const vol = p.volume as number | undefined;
      return `close=${close?.toFixed(4)} vol=${vol?.toFixed(0)}`;
    }
    case "indicator": {
      const strat = p.strategy as
        | { macd_1m_hist?: number; macd_5m_hist?: number; macd_histogram?: number; vwap?: number; vwap_state?: string }
        | undefined;
      if (!strat) return JSON.stringify(p);
      const m1 = strat.macd_1m_hist ?? strat.macd_histogram;
      return `1m=${m1?.toFixed(6) ?? "—"} 5m=${strat.macd_5m_hist?.toFixed(6) ?? "—"} vwap=${strat.vwap?.toFixed(4) ?? "—"} (${strat.vwap_state ?? "—"})`;
    }
    case "signal":
      return `${p.kind} @ ${p.price} — ${p.reason}`;
    case "decision": {
      const stage = p.stage ?? p.action ?? "";
      if (stage === "exit_trigger") {
        return `EXIT ${p.kind}: ${p.reason}`;
      }
      if (stage === "microstructure_gate") {
        return `microstructure ${p.passed ? "PASS" : "FAIL"}: ${(p.failures as string[] | undefined)?.join("; ") ?? ""}`;
      }
      return `${stage} qty=${p.qty ?? "-"}`;
    }
    case "ready_for_approval":
      return `${p.signal_kind} qty=${p.intended_qty} @ ${p.price}`;
    case "approval_granted":
      return "user approved";
    case "approval_rejected":
      return "user rejected";
    case "order_submit":
      return `${p.side} ${p.quantity} ${p.symbol} ${p.order_type ?? ""} ${p.limit_price ? `@ ${p.limit_price}` : ""} (signal ${p.signal_kind} @ ${p.signal_price})`;
    case "order_status":
      return `id=${p.ibkr_order_id} status=${p.status} ${p.reason ? `(${p.reason})` : ""} filled=${p.filled ?? "-"} avg=${p.avgFillPrice ?? "-"}`;
    case "fill":
      return `id=${p.ibkr_order_id} ${p.side} ${p.qty} @ ${p.fill_price}`;
    case "slippage":
      return `${p.side} signal=${p.signal_price} fill=${p.fill_price} ${Number(p.slippage_cents ?? 0).toFixed(2)}c (${Number(p.slippage_bps ?? 0).toFixed(2)}bps) latency=${p.latency_ms}ms`;
    case "risk_block":
      return `${p.action} blocked: ${(p.reasons as string[] | undefined)?.join("; ")}`;
    case "engine_start":
      return `${p.symbol} ${p.strategy} qty=${p.quantity} autonomous=${p.autonomous} order=${p.order_type}`;
    case "engine_stop":
      return `${p.reason} trades=${p.trades_count} pnl=$${Number(p.realized_pnl ?? 0).toFixed(2)}`;
    case "ibkr_connected":
      return `account=${p.account} client_id=${p.client_id}`;
    case "error":
      return `${p.where ?? ""}: ${p.error ?? p.msg ?? ""}`;
    default:
      return JSON.stringify(p);
  }
}

function formatTs(s: string): string {
  if (!s) return "";
  try {
    return new Date(s).toLocaleTimeString();
  } catch {
    return s;
  }
}

function RecentRuns({ runs }: { runs: EngineRun[] | undefined }) {
  if (!runs) return <p className="text-sm text-neutral-500">Loading…</p>;
  if (runs.length === 0)
    return <p className="text-sm text-neutral-500">No runs yet. Arm one above.</p>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-neutral-200 text-left text-xs uppercase tracking-wide text-neutral-500">
            <th className="px-2 py-2">ID</th>
            <th className="px-2 py-2">Symbol</th>
            <th className="px-2 py-2">Strategy</th>
            <th className="px-2 py-2">Mode</th>
            <th className="px-2 py-2">Order</th>
            <th className="px-2 py-2">L2/T&amp;S</th>
            <th className="px-2 py-2">Started</th>
            <th className="px-2 py-2">Stopped</th>
            <th className="px-2 py-2">Trades</th>
            <th className="px-2 py-2">P&amp;L</th>
            <th className="px-2 py-2">Status</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => (
            <tr key={r.id} className="border-b border-neutral-100">
              <td className="px-2 py-2 font-mono">{r.id}</td>
              <td className="px-2 py-2 font-semibold">{r.symbol}</td>
              <td className="px-2 py-2">{r.strategy_name}</td>
              <td className="px-2 py-2">{r.autonomous ? "auto" : "manual"}</td>
              <td className="px-2 py-2">{r.order_type}</td>
              <td className="px-2 py-2 text-xs">
                {(r.enable_depth ? "L2" : "") +
                  (r.enable_depth && r.enable_tape ? "+" : "") +
                  (r.enable_tape ? "T&S" : "") || "—"}
              </td>
              <td className="px-2 py-2 text-xs text-neutral-600">{formatTs(r.started_at)}</td>
              <td className="px-2 py-2 text-xs text-neutral-600">
                {r.stopped_at ? formatTs(r.stopped_at) : "—"}
              </td>
              <td className="px-2 py-2">{r.trades_count}</td>
              <td className="px-2 py-2">${Number(r.realized_pnl).toFixed(2)}</td>
              <td className="px-2 py-2">{r.status}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------- small ui primitives ----------

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-semibold uppercase tracking-wide text-neutral-600">
        {label}
      </span>
      {children}
    </label>
  );
}

function Hint({ children }: { children: React.ReactNode }) {
  return <p className="mt-1 text-xs text-neutral-500">{children}</p>;
}

function Toggle({
  label,
  checked,
  onChange,
  hint,
}: {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  hint?: string;
}) {
  return (
    <label className="flex items-start gap-3">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="mt-1 h-4 w-4 rounded border-neutral-300"
      />
      <span className="text-sm">
        <span className="font-medium text-neutral-800">{label}</span>
        {hint ? <span className="ml-1 text-xs text-neutral-500">— {hint}</span> : null}
      </span>
    </label>
  );
}

function KV({
  label,
  value,
  accent,
}: {
  label: string;
  value: string;
  accent?: "neutral" | "amber" | "sky" | "rose" | "emerald";
}) {
  const accentClass =
    accent === "amber"
      ? "text-amber-700"
      : accent === "sky"
      ? "text-sky-700"
      : accent === "rose"
      ? "text-rose-700"
      : accent === "emerald"
      ? "text-emerald-700"
      : "text-neutral-900";
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-neutral-100 py-1.5 last:border-b-0">
      <span className="text-xs font-semibold uppercase tracking-wide text-neutral-500">{label}</span>
      <span className={"font-medium " + accentClass}>{value}</span>
    </div>
  );
}

function Tile({
  label,
  value,
  accent,
}: {
  label: string;
  value: string;
  accent?: "neutral" | "amber" | "sky" | "rose" | "emerald";
}) {
  const ringClass =
    accent === "rose"
      ? "ring-rose-200 bg-rose-50 text-rose-700"
      : accent === "emerald"
      ? "ring-emerald-200 bg-emerald-50 text-emerald-700"
      : accent === "amber"
      ? "ring-amber-200 bg-amber-50 text-amber-700"
      : accent === "sky"
      ? "ring-sky-200 bg-sky-50 text-sky-700"
      : "ring-neutral-200 bg-neutral-50 text-neutral-900";
  return (
    <div className={"rounded-md px-3 py-2 ring-1 " + ringClass}>
      <div className="text-[10px] font-semibold uppercase tracking-wide opacity-70">{label}</div>
      <div className="mt-0.5 font-mono text-sm">{value}</div>
    </div>
  );
}
