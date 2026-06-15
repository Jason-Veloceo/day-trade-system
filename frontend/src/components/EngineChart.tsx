"use client";

import { useEffect, useRef } from "react";
import {
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  createChart,
  createSeriesMarkers,
  type CandlestickData,
  type HistogramData,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type LineData,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";

import { API_BASE } from "@/lib/api";
import type { WsMessage } from "@/lib/types";

const HIST_POS = "#10b98166";
const HIST_NEG = "#f4364466";
const SIGNAL_ENTRY = "#2563eb";
const SIGNAL_EXIT = "#7c3aed";
const FILL_COLOR = "#047857";

interface BarApi {
  ts: string;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: string;
  macd_line: string | null;
  macd_signal: string | null;
  macd_hist: string | null;
}

function tsToTime(iso: string): UTCTimestamp {
  return Math.floor(new Date(iso).getTime() / 1000) as UTCTimestamp;
}

export function EngineChart({
  runId,
  symbol,
  events,
}: {
  runId: number;
  symbol: string;
  events: WsMessage[];
}) {
  const containerRef = useRef<HTMLDivElement>(null);

  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const macdLineRef = useRef<ISeriesApi<"Line"> | null>(null);
  const sigLineRef = useRef<ISeriesApi<"Line"> | null>(null);
  const histRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);

  const seenBarsRef = useRef<Set<string>>(new Set());
  const markersStateRef = useRef<SeriesMarker<Time>[]>([]);
  const lastEventIdxRef = useRef<number>(0);

  // 1) Create chart + series once on mount.
  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, {
      autoSize: true,
      layout: {
        background: { color: "#ffffff" },
        textColor: "#0a0a0a",
        panes: { separatorColor: "#e5e5e5", separatorHoverColor: "#d4d4d4", enableResize: true },
      },
      grid: {
        vertLines: { color: "#f5f5f5" },
        horzLines: { color: "#f5f5f5" },
      },
      timeScale: {
        timeVisible: true,
        secondsVisible: false,
        borderColor: "#e5e5e5",
      },
      rightPriceScale: { borderColor: "#e5e5e5" },
      crosshair: { mode: 1 },
    });

    const candle = chart.addSeries(CandlestickSeries, {
      upColor: "#059669",
      downColor: "#e11d48",
      wickUpColor: "#059669",
      wickDownColor: "#e11d48",
      borderVisible: false,
    });

    const macdLine = chart.addSeries(
      LineSeries,
      { color: "#2563eb", lineWidth: 2, priceLineVisible: false, title: "MACD" },
      1
    );
    const sigLine = chart.addSeries(
      LineSeries,
      { color: "#dc2626", lineWidth: 2, priceLineVisible: false, title: "Signal" },
      1
    );
    const hist = chart.addSeries(
      HistogramSeries,
      { priceLineVisible: false, title: "Hist" },
      1
    );

    // Give the bottom pane a sensible relative size.
    const panes = chart.panes();
    if (panes.length >= 2) {
      panes[0].setHeight(360);
      panes[1].setHeight(160);
    }

    const markers = createSeriesMarkers(candle, []);

    chartRef.current = chart;
    candleRef.current = candle;
    macdLineRef.current = macdLine;
    sigLineRef.current = sigLine;
    histRef.current = hist;
    markersRef.current = markers;

    return () => {
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
      macdLineRef.current = null;
      sigLineRef.current = null;
      histRef.current = null;
      markersRef.current = null;
    };
  }, []);

  // 2) Reset and bootstrap from historical bars whenever the active runId changes.
  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    seenBarsRef.current = new Set();
    markersStateRef.current = [];
    lastEventIdxRef.current = 0;

    if (markersRef.current) markersRef.current.setMarkers([]);
    if (candleRef.current) candleRef.current.setData([]);
    if (macdLineRef.current) macdLineRef.current.setData([]);
    if (sigLineRef.current) sigLineRef.current.setData([]);
    if (histRef.current) histRef.current.setData([]);

    fetch(`${API_BASE}/engine/runs/${runId}/bars?limit=1000`)
      .then((r) => r.json() as Promise<BarApi[]>)
      .then((bars) => {
        if (cancelled || !candleRef.current) return;

        const candles: CandlestickData[] = bars.map((b) => ({
          time: tsToTime(b.ts),
          open: Number(b.open),
          high: Number(b.high),
          low: Number(b.low),
          close: Number(b.close),
        }));
        const macdLine: LineData[] = bars
          .filter((b) => b.macd_line !== null)
          .map((b) => ({ time: tsToTime(b.ts), value: Number(b.macd_line) }));
        const sigLine: LineData[] = bars
          .filter((b) => b.macd_signal !== null)
          .map((b) => ({ time: tsToTime(b.ts), value: Number(b.macd_signal) }));
        const histPoints: HistogramData[] = bars
          .filter((b) => b.macd_hist !== null)
          .map((b) => {
            const h = Number(b.macd_hist);
            return { time: tsToTime(b.ts), value: h, color: h >= 0 ? HIST_POS : HIST_NEG };
          });

        candleRef.current.setData(candles);
        macdLineRef.current?.setData(macdLine);
        sigLineRef.current?.setData(sigLine);
        histRef.current?.setData(histPoints);
        bars.forEach((b) => seenBarsRef.current.add(b.ts));
        chartRef.current?.timeScale().fitContent();
      })
      .catch(() => {
        // swallow - the live stream will populate as bars arrive
      });

    return () => {
      cancelled = true;
    };
  }, [runId]);

  // 3) Apply live event updates. Only process events we haven't seen yet.
  useEffect(() => {
    if (!candleRef.current) return;
    const start = lastEventIdxRef.current;
    if (events.length <= start) return;

    let markersDirty = false;

    for (let i = start; i < events.length; i++) {
      const m = events[i];
      const env = m.payload as { run_id?: number; event_type?: string; payload?: Record<string, unknown> } | undefined;
      if (!env || env.run_id !== runId) continue;
      const type = env.event_type ?? "";
      const inner = (env.payload ?? {}) as Record<string, unknown>;

      if (type === "bar") {
        const ts = String(inner.ts ?? "");
        if (!ts || seenBarsRef.current.has(ts)) continue;
        candleRef.current.update({
          time: tsToTime(ts),
          open: Number(inner.open),
          high: Number(inner.high),
          low: Number(inner.low),
          close: Number(inner.close),
        });
        seenBarsRef.current.add(ts);
        continue;
      }

      if (type === "bar_tick") {
        // In-progress bar update (~every 5s). Update the rightmost candle
        // without marking it "seen", so subsequent ticks keep refining it.
        // Once the official `bar` event arrives at minute close, that ts is
        // added to seenBarsRef and further ticks for the same ts are no-ops.
        const ts = String(inner.ts ?? "");
        if (!ts || seenBarsRef.current.has(ts)) continue;
        candleRef.current.update({
          time: tsToTime(ts),
          open: Number(inner.open),
          high: Number(inner.high),
          low: Number(inner.low),
          close: Number(inner.close),
        });
        continue;
      }

      if (type === "indicator") {
        const barTs = String(inner.bar_ts ?? "");
        const strat = (inner.strategy ?? {}) as Record<string, unknown>;
        if (!barTs) continue;
        const time = tsToTime(barTs);
        const macd = strat.macd_line as number | null | undefined;
        const sig = strat.macd_signal as number | null | undefined;
        const hist = strat.macd_histogram as number | null | undefined;
        if (macd !== null && macd !== undefined && macdLineRef.current) {
          macdLineRef.current.update({ time, value: macd });
        }
        if (sig !== null && sig !== undefined && sigLineRef.current) {
          sigLineRef.current.update({ time, value: sig });
        }
        if (hist !== null && hist !== undefined && histRef.current) {
          histRef.current.update({
            time,
            value: hist,
            color: hist >= 0 ? HIST_POS : HIST_NEG,
          });
        }
        continue;
      }

      if (type === "signal") {
        const kind = String(inner.kind ?? "");
        const ts = String(inner.ts ?? "");
        const price = Number(inner.price ?? 0);
        if (!ts) continue;
        const isEntry = kind === "enter_long";
        markersStateRef.current.push({
          time: tsToTime(ts),
          position: isEntry ? "belowBar" : "aboveBar",
          color: isEntry ? SIGNAL_ENTRY : SIGNAL_EXIT,
          shape: isEntry ? "arrowUp" : "arrowDown",
          text: `${isEntry ? "BUY" : "SELL"} @ ${price.toFixed(4)}`,
          size: 1.5,
        });
        markersDirty = true;
        continue;
      }

      if (type === "fill") {
        const fillTs = String(inner.fill_ts ?? "");
        const side = String(inner.side ?? "");
        const fillPrice = Number(inner.fill_price ?? 0);
        if (!fillTs) continue;
        markersStateRef.current.push({
          time: tsToTime(fillTs),
          position: side === "BUY" ? "belowBar" : "aboveBar",
          color: FILL_COLOR,
          shape: "circle",
          text: `FILL ${side} @ ${fillPrice.toFixed(4)}`,
          size: 1,
        });
        markersDirty = true;
        continue;
      }

      if (type === "slippage") {
        // Decorate the most recent fill marker with the slippage in cents.
        const last = markersStateRef.current[markersStateRef.current.length - 1];
        if (last && typeof last.text === "string" && last.text.startsWith("FILL")) {
          const cents = Number(inner.slippage_cents ?? 0);
          const sign = cents > 0 ? "+" : "";
          last.text = `${last.text} (${sign}${cents.toFixed(2)}c)`;
          markersDirty = true;
        }
      }
    }

    if (markersDirty && markersRef.current) {
      // setMarkers requires the time-sorted list, so sort defensively.
      const sorted = [...markersStateRef.current].sort(
        (a, b) => (a.time as number) - (b.time as number)
      );
      markersStateRef.current = sorted;
      markersRef.current.setMarkers(sorted);
    }

    lastEventIdxRef.current = events.length;
  }, [events, runId]);

  return (
    <div className="space-y-2">
      <div className="flex items-baseline justify-between">
        <div className="text-sm text-neutral-600">
          Live chart for run{" "}
          <span className="font-mono">#{runId}</span> ·{" "}
          <span className="font-semibold text-neutral-900">{symbol}</span>
        </div>
        <Legend />
      </div>
      <div
        ref={containerRef}
        className="w-full rounded-md border border-neutral-200 bg-white"
        style={{ height: 540 }}
      />
    </div>
  );
}

function Legend() {
  const items: { color: string; shape: string; label: string }[] = [
    { color: SIGNAL_ENTRY, shape: "▲", label: "BUY signal" },
    { color: SIGNAL_EXIT, shape: "▼", label: "SELL signal" },
    { color: FILL_COLOR, shape: "●", label: "Fill (with slippage)" },
  ];
  return (
    <div className="flex flex-wrap gap-3 text-xs text-neutral-600">
      {items.map((it) => (
        <span key={it.label} className="inline-flex items-center gap-1">
          <span style={{ color: it.color }}>{it.shape}</span>
          {it.label}
        </span>
      ))}
    </div>
  );
}
