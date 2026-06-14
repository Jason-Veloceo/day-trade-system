import Link from "next/link";
import type { Candidate } from "@/lib/types";
import { fmtFloat, fmtNumber, fmtPct, fmtTime } from "@/lib/format";

export function CandidateRow({ candidate }: { candidate: Candidate }) {
  const c = candidate;
  return (
    <Link
      href={`/candidates/${c.id}`}
      className="grid grid-cols-12 gap-2 border-b border-neutral-200 px-4 py-2.5 text-sm transition-colors hover:bg-neutral-50"
    >
      <div className="col-span-1 flex items-center gap-1.5 font-semibold text-neutral-900">
        {c.symbol}
        {c.is_5_pillars && (
          <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-amber-800 ring-1 ring-amber-200">
            5P
          </span>
        )}
      </div>
      <div className="col-span-1 tnum text-neutral-700">${fmtNumber(c.last_close_price, 2)}</div>
      <div className="col-span-1 tnum text-neutral-700">{fmtFloat(c.last_float)}</div>
      <div className="col-span-1 tnum text-neutral-700">{fmtNumber(c.last_rel_vol_today, 1)}x</div>
      <div className="col-span-1 tnum text-neutral-700">{fmtNumber(c.last_rel_vol_5min, 1)}x</div>
      <div className="col-span-1 tnum font-medium text-emerald-700">{fmtPct(c.last_rel_gain)}</div>
      <div className="col-span-1 text-neutral-500">
        {c.has_news ? (
          <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[11px] font-medium text-amber-800 ring-1 ring-amber-200">
            news
          </span>
        ) : (
          <span className="text-neutral-400">—</span>
        )}
      </div>
      <div
        className="col-span-2 truncate text-neutral-600"
        title={c.strategies_fired.join(", ")}
      >
        {c.strategies_fired.join(", ") || <span className="text-neutral-400">—</span>}
      </div>
      <div className="col-span-1 tnum text-neutral-500">{c.alert_count}</div>
      <div className="col-span-2 tnum text-right text-neutral-500">{fmtTime(c.last_alert_at)}</div>
    </Link>
  );
}

export function CandidateHeader() {
  return (
    <div className="grid grid-cols-12 gap-2 border-b border-neutral-200 bg-neutral-50 px-4 py-2 text-[11px] font-semibold uppercase tracking-wider text-neutral-500">
      <div className="col-span-1">Sym</div>
      <div className="col-span-1">Price</div>
      <div className="col-span-1">Float</div>
      <div className="col-span-1">RVOL day</div>
      <div className="col-span-1">RVOL 5m</div>
      <div className="col-span-1">% gain</div>
      <div className="col-span-1">News</div>
      <div className="col-span-2">Strategies</div>
      <div className="col-span-1">Alerts</div>
      <div className="col-span-2 text-right">Last alert</div>
    </div>
  );
}
