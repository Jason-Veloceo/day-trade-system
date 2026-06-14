export function fmtNumber(v: string | number | null | undefined, decimals = 2): string {
  if (v === null || v === undefined) return "-";
  const n = typeof v === "string" ? parseFloat(v) : v;
  if (!isFinite(n)) return "-";
  return n.toLocaleString(undefined, { maximumFractionDigits: decimals });
}

export function fmtInt(v: number | null | undefined): string {
  if (v === null || v === undefined) return "-";
  return v.toLocaleString();
}

export function fmtFloat(v: number | null | undefined): string {
  if (v === null || v === undefined) return "-";
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(2)}M`;
  if (v >= 1_000) return `${(v / 1_000).toFixed(1)}K`;
  return v.toString();
}

export function fmtPct(v: string | number | null | undefined): string {
  if (v === null || v === undefined) return "-";
  const n = typeof v === "string" ? parseFloat(v) : v;
  if (!isFinite(n)) return "-";
  return `${n.toFixed(1)}%`;
}

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "-";
  const d = new Date(iso);
  return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
