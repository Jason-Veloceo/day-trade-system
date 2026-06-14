"use client";

import useSWR from "swr";
import { use } from "react";
import { fetcher } from "@/lib/api";
import { TradingViewChart } from "@/components/TradingViewChart";
import { fmtFloat, fmtNumber, fmtPct, fmtTime } from "@/lib/format";
import type { CandidateDetail } from "@/lib/types";

export default function CandidatePage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const { data: c, error, isLoading } = useSWR<CandidateDetail>(
    `/candidates/${id}`,
    fetcher,
    { refreshInterval: 5000 }
  );

  if (isLoading) return <div className="p-4 text-sm text-neutral-500">Loading…</div>;
  if (error || !c)
    return (
      <div className="p-4 text-sm text-rose-700">
        Failed to load candidate.
      </div>
    );

  return (
    <section className="space-y-6">
      <header className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-semibold tracking-tight text-neutral-900">{c.symbol}</h1>
        {c.is_5_pillars && (
          <span className="rounded-md bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-800 ring-1 ring-amber-200">
            5 Pillars
          </span>
        )}
        <span
          className={
            c.status === "passed"
              ? "rounded-md bg-emerald-50 px-2 py-0.5 text-xs font-medium text-emerald-700 ring-1 ring-emerald-200"
              : "rounded-md bg-rose-50 px-2 py-0.5 text-xs font-medium text-rose-700 ring-1 ring-rose-200"
          }
        >
          {c.status}
        </span>
        <span className="text-sm text-neutral-500">
          first alert {fmtTime(c.first_alert_at)} · last {fmtTime(c.last_alert_at)} · {c.alert_count} fires
        </span>
      </header>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Price" value={`$${fmtNumber(c.last_close_price, 2)}`} />
        <Stat label="Float" value={fmtFloat(c.last_float)} />
        <Stat label="Volume" value={fmtFloat(c.last_volume)} />
        <Stat label="Short int." value={fmtFloat(c.last_short_interest)} />
        <Stat label="RVOL today" value={`${fmtNumber(c.last_rel_vol_today, 1)}x`} />
        <Stat label="RVOL 5m" value={`${fmtNumber(c.last_rel_vol_5min, 1)}x`} />
        <Stat label="Gap" value={fmtPct(c.last_rel_gap)} />
        <Stat label="% gain" value={fmtPct(c.last_rel_gain)} tone="positive" />
      </div>

      <div className="overflow-hidden rounded-xl border border-neutral-200 bg-white shadow-sm">
        <div className="border-b border-neutral-200 bg-neutral-50 px-4 py-2 text-[11px] font-semibold uppercase tracking-wider text-neutral-500">
          Chart (NASDAQ:{c.symbol}, 1m)
        </div>
        <div className="p-2">
          <TradingViewChart symbol={c.symbol} interval="1" />
        </div>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <div className="rounded-xl border border-neutral-200 bg-white p-4 shadow-sm">
          <h2 className="mb-3 text-[11px] font-semibold uppercase tracking-wider text-neutral-500">
            Strategies fired
          </h2>
          <ul className="space-y-1.5 text-sm">
            {c.strategies_fired.map((s) => (
              <li
                key={s}
                className="rounded-md bg-neutral-50 px-2.5 py-1.5 text-neutral-700 ring-1 ring-neutral-200"
              >
                {s}
              </li>
            ))}
            {c.strategies_fired.length === 0 && (
              <li className="text-neutral-400">No strategies recorded.</li>
            )}
          </ul>
        </div>

        <div className="rounded-xl border border-neutral-200 bg-white p-4 shadow-sm">
          <h2 className="mb-3 text-[11px] font-semibold uppercase tracking-wider text-neutral-500">
            Filter rules
          </h2>
          <ul className="space-y-1.5 text-sm">
            {c.evaluations.map((e) => (
              <li
                key={e.rule_key}
                className="flex items-center justify-between rounded-md bg-neutral-50 px-2.5 py-1.5 ring-1 ring-neutral-200"
              >
                <span className="text-neutral-700">{e.rule_key}</span>
                <span className="flex items-center gap-2">
                  <code className="tnum text-xs text-neutral-500">obs={String(e.observed)}</code>
                  <span
                    className={
                      e.passed
                        ? "rounded-md bg-emerald-50 px-2 py-0.5 text-xs font-medium text-emerald-700 ring-1 ring-emerald-200"
                        : "rounded-md bg-rose-50 px-2 py-0.5 text-xs font-medium text-rose-700 ring-1 ring-rose-200"
                    }
                  >
                    {e.passed ? "pass" : "fail"}
                  </span>
                </span>
              </li>
            ))}
          </ul>
        </div>
      </div>

      {c.news_headline && (
        <div className="rounded-xl border border-amber-200 bg-amber-50 p-4">
          <div className="text-[11px] font-semibold uppercase tracking-wider text-amber-800">News</div>
          <div className="mt-1 text-sm text-neutral-900">{c.news_headline}</div>
          {c.news_storyurl && (
            <a
              href={c.news_storyurl}
              target="_blank"
              rel="noreferrer"
              className="mt-1 inline-block text-xs font-medium text-amber-800 hover:underline"
            >
              Open story →
            </a>
          )}
        </div>
      )}
    </section>
  );
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "positive" | "negative";
}) {
  const valueClass =
    tone === "positive"
      ? "text-emerald-700"
      : tone === "negative"
      ? "text-rose-700"
      : "text-neutral-900";
  return (
    <div className="rounded-xl border border-neutral-200 bg-white px-4 py-3 shadow-sm">
      <div className="text-[11px] font-semibold uppercase tracking-wider text-neutral-500">
        {label}
      </div>
      <div className={`mt-1 text-xl font-semibold tnum ${valueClass}`}>{value}</div>
    </div>
  );
}
