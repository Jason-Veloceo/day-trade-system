"use client";

import { Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { TradingViewChart } from "@/components/TradingViewChart";

// Curated exchange list. Add more as needed; TradingView supports many more.
const EXCHANGES = [
  { code: "NASDAQ", label: "NASDAQ (US)" },
  { code: "NYSE", label: "NYSE (US)" },
  { code: "AMEX", label: "AMEX / NYSE Arca (US)" },
  { code: "ASX", label: "ASX (Australia)" },
  { code: "LSE", label: "LSE (London)" },
  { code: "HKEX", label: "HKEX (Hong Kong)" },
  { code: "TSE", label: "TSE (Tokyo)" },
  { code: "TSX", label: "TSX (Toronto)" },
  { code: "FX", label: "Forex (FX)" },
  { code: "BINANCE", label: "Binance (crypto)" },
];

const INTERVALS: { value: string; label: string }[] = [
  { value: "1", label: "1m" },
  { value: "5", label: "5m" },
  { value: "15", label: "15m" },
  { value: "60", label: "1h" },
  { value: "D", label: "1D" },
  { value: "W", label: "1W" },
];

const THEMES: { value: "dark" | "light"; label: string }[] = [
  { value: "dark", label: "Dark" },
  { value: "light", label: "Light" },
];

export default function InspectPage() {
  return (
    <Suspense fallback={<div className="text-sm text-neutral-500">Loading…</div>}>
      <InspectInner />
    </Suspense>
  );
}

function InspectInner() {
  const router = useRouter();
  const params = useSearchParams();

  const urlExchange = params.get("exchange") ?? "NASDAQ";
  const urlSymbol = (params.get("symbol") ?? "").toUpperCase();
  const urlInterval = params.get("interval") ?? "1";
  const urlTheme = (params.get("theme") ?? "dark") as "dark" | "light";

  const [exchange, setExchange] = useState(urlExchange);
  const [symbol, setSymbol] = useState(urlSymbol);
  const [interval, setInterval] = useState(urlInterval);
  const [theme, setTheme] = useState<"dark" | "light">(urlTheme);

  function open(e: React.FormEvent) {
    e.preventDefault();
    const sym = symbol.trim().toUpperCase();
    if (!sym) return;
    const qs = new URLSearchParams({
      exchange,
      symbol: sym,
      interval,
      theme,
    });
    router.push(`/inspect?${qs.toString()}`);
  }

  const showChart = urlSymbol.length > 0;

  return (
    <section className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-neutral-900">Ticker inspector</h1>
        <p className="mt-1 text-sm text-neutral-500">
          Pull up a TradingView chart for any ticker on any supported exchange. ASX tickers route to{" "}
          <span className="font-mono">ASX:&lt;symbol&gt;</span> (e.g. <span className="font-mono">ASX:BHP</span>).
          US tickers default to NASDAQ; TradingView will auto-resolve to NYSE / AMEX if needed.
        </p>
      </div>

      <div className="rounded-xl border border-neutral-200 bg-white p-5 shadow-sm">
        <form onSubmit={open} className="grid grid-cols-1 gap-3 sm:grid-cols-12">
          <div className="sm:col-span-4">
            <label className="mb-1 block text-xs font-semibold uppercase tracking-wide text-neutral-600">
              Exchange
            </label>
            <select
              value={exchange}
              onChange={(e) => setExchange(e.target.value)}
              className="w-full rounded-md border border-neutral-300 bg-white px-3 py-2 text-sm text-neutral-900 focus:border-neutral-900 focus:outline-none"
            >
              {EXCHANGES.map((x) => (
                <option key={x.code} value={x.code}>
                  {x.label}
                </option>
              ))}
            </select>
          </div>
          <div className="sm:col-span-3">
            <label className="mb-1 block text-xs font-semibold uppercase tracking-wide text-neutral-600">
              Symbol
            </label>
            <input
              type="text"
              value={symbol}
              onChange={(e) => setSymbol(e.target.value.toUpperCase())}
              placeholder="SPY / BHP / EURUSD"
              className="w-full rounded-md border border-neutral-300 bg-white px-3 py-2 font-mono text-sm uppercase text-neutral-900 placeholder-neutral-400 focus:border-neutral-900 focus:outline-none"
            />
          </div>
          <div className="sm:col-span-2">
            <label className="mb-1 block text-xs font-semibold uppercase tracking-wide text-neutral-600">
              Interval
            </label>
            <select
              value={interval}
              onChange={(e) => setInterval(e.target.value)}
              className="w-full rounded-md border border-neutral-300 bg-white px-3 py-2 text-sm text-neutral-900 focus:border-neutral-900 focus:outline-none"
            >
              {INTERVALS.map((x) => (
                <option key={x.value} value={x.value}>
                  {x.label}
                </option>
              ))}
            </select>
          </div>
          <div className="sm:col-span-2">
            <label className="mb-1 block text-xs font-semibold uppercase tracking-wide text-neutral-600">
              Theme
            </label>
            <select
              value={theme}
              onChange={(e) => setTheme(e.target.value as "dark" | "light")}
              className="w-full rounded-md border border-neutral-300 bg-white px-3 py-2 text-sm text-neutral-900 focus:border-neutral-900 focus:outline-none"
            >
              {THEMES.map((x) => (
                <option key={x.value} value={x.value}>
                  {x.label}
                </option>
              ))}
            </select>
          </div>
          <div className="sm:col-span-1 flex items-end">
            <button
              type="submit"
              className="h-[38px] w-full rounded-md bg-neutral-900 px-4 text-sm font-semibold text-white hover:bg-neutral-800"
            >
              Open
            </button>
          </div>
        </form>
        <div className="mt-3 flex flex-wrap gap-2 text-xs">
          <ExampleChip
            label="SPY (NASDAQ)"
            onClick={() => {
              setExchange("NASDAQ");
              setSymbol("SPY");
            }}
          />
          <ExampleChip
            label="BHP (ASX)"
            onClick={() => {
              setExchange("ASX");
              setSymbol("BHP");
            }}
          />
          <ExampleChip
            label="CBA (ASX)"
            onClick={() => {
              setExchange("ASX");
              setSymbol("CBA");
            }}
          />
          <ExampleChip
            label="EURUSD (FX)"
            onClick={() => {
              setExchange("FX");
              setSymbol("EURUSD");
            }}
          />
          <ExampleChip
            label="BTCUSDT (Binance)"
            onClick={() => {
              setExchange("BINANCE");
              setSymbol("BTCUSDT");
            }}
          />
        </div>
      </div>

      {showChart ? (
        <div className="rounded-xl border border-neutral-200 bg-white shadow-sm">
          <div className="border-b border-neutral-200 px-5 py-3">
            <h2 className="text-sm font-semibold text-neutral-700">
              <span className="font-mono">{urlExchange}:{urlSymbol}</span>
              <span className="ml-3 text-neutral-500">
                · {INTERVALS.find((i) => i.value === urlInterval)?.label ?? urlInterval}
              </span>
            </h2>
          </div>
          <div className="p-2">
            <TradingViewChart
              symbol={urlSymbol}
              exchange={urlExchange}
              interval={urlInterval}
              theme={urlTheme}
            />
          </div>
        </div>
      ) : (
        <div className="rounded-xl border border-dashed border-neutral-300 bg-neutral-50 p-8 text-center">
          <p className="text-sm text-neutral-600">
            Pick an exchange + symbol above and press <span className="font-semibold">Open</span> to
            load the chart. The chart UI supports panning, indicators, drawings, and you can change
            the symbol inside the chart itself.
          </p>
        </div>
      )}
    </section>
  );
}

function ExampleChip({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="rounded-full border border-neutral-300 bg-white px-3 py-1 text-xs text-neutral-700 hover:bg-neutral-50"
    >
      {label}
    </button>
  );
}
