"use client";

import { useMemo } from "react";

/**
 * TradingView Advanced Chart, rendered via the iframe-embed widget.
 *
 * Why iframe and not the tv.js JS widget? The JS widget caches chart state
 * (last symbol, indicators, drawings) in localStorage and shares it across
 * every widget instance on the page. That meant calling the widget with
 * `symbol: "ASX:BRK"` would sometimes resolve to whatever symbol was in
 * localStorage from a previous chart instead of honouring the parameter.
 * The iframe embed has no shared state; the URL is the chart.
 */
export function TradingViewChart({
  symbol,
  exchange = "NASDAQ",
  interval = "1",
  theme = "dark",
  height = 520,
}: {
  symbol: string;
  exchange?: string;
  interval?: string;
  theme?: "dark" | "light";
  height?: number;
}) {
  const fullSymbol = symbol.includes(":") ? symbol : `${exchange}:${symbol}`;

  const src = useMemo(() => {
    const params = new URLSearchParams({
      symbol: fullSymbol,
      interval,
      theme,
      style: "1",
      locale: "en",
      timezone: "America/New_York",
      hide_side_toolbar: "0",
      hide_top_toolbar: "0",
      hide_legend: "0",
      allow_symbol_change: "1",
      save_image: "0",
      details: "1",
      withdateranges: "1",
      studies: '["STD;VWAP","Volume@tv-basicstudies"]',
      autosize: "1",
    });
    return `https://s.tradingview.com/widgetembed/?${params.toString()}`;
  }, [fullSymbol, interval, theme]);

  return (
    <iframe
      key={src}
      src={src}
      title={`TradingView ${fullSymbol}`}
      allowFullScreen
      style={{ width: "100%", height, border: 0 }}
      className="overflow-hidden rounded-lg"
    />
  );
}
