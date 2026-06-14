import Link from "next/link";
import type { Candidate } from "@/lib/types";
import { fmtFloat, fmtNumber, fmtPct, fmtTime } from "@/lib/format";

export function RejectedRow({ candidate }: { candidate: Candidate }) {
  const c = candidate;
  return (
    <Link
      href={`/candidates/${c.id}`}
      className="grid grid-cols-12 gap-2 border-b border-neutral-200 px-4 py-2.5 text-sm transition-colors hover:bg-neutral-50"
    >
      <div className="col-span-1 font-medium text-neutral-700">{c.symbol}</div>
      <div className="col-span-1 tnum text-neutral-500">${fmtNumber(c.last_close_price, 2)}</div>
      <div className="col-span-1 tnum text-neutral-500">{fmtFloat(c.last_float)}</div>
      <div className="col-span-1 tnum text-neutral-500">{fmtNumber(c.last_rel_vol_5min, 1)}x</div>
      <div className="col-span-1 tnum text-neutral-500">{fmtPct(c.last_rel_gain)}</div>
      <div className="col-span-5 flex flex-wrap gap-1">
        {c.failed_rules.map((r) => (
          <span
            key={r}
            className="rounded-md bg-rose-50 px-2 py-0.5 text-xs font-medium text-rose-700 ring-1 ring-rose-200"
          >
            {r}
          </span>
        ))}
      </div>
      <div className="col-span-2 tnum text-right text-neutral-500">{fmtTime(c.last_alert_at)}</div>
    </Link>
  );
}

export function RejectedHeader() {
  return (
    <div className="grid grid-cols-12 gap-2 border-b border-neutral-200 bg-neutral-50 px-4 py-2 text-[11px] font-semibold uppercase tracking-wider text-neutral-500">
      <div className="col-span-1">Sym</div>
      <div className="col-span-1">Price</div>
      <div className="col-span-1">Float</div>
      <div className="col-span-1">RVOL 5m</div>
      <div className="col-span-1">% gain</div>
      <div className="col-span-5">Failed rules</div>
      <div className="col-span-2 text-right">Last alert</div>
    </div>
  );
}
