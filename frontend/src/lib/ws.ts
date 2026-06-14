"use client";

import { useEffect, useRef, useState } from "react";
import type { WsMessage } from "./types";

const WS_BASE = process.env.NEXT_PUBLIC_WS_BASE ?? "ws://localhost:8000";

/**
 * Subscribe to all broker topics multiplexed on /ws.
 * Each new message is appended to the local buffer (capped) and the latest is
 * exposed for components that only care about freshness.
 */
export function useBrokerStream(opts?: { bufferSize?: number }) {
  const bufferSize = opts?.bufferSize ?? 200;
  const [messages, setMessages] = useState<WsMessage[]>([]);
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    let cancelled = false;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

    const connect = () => {
      if (cancelled) return;
      const ws = new WebSocket(`${WS_BASE}/ws`);
      wsRef.current = ws;
      ws.onopen = () => setConnected(true);
      ws.onclose = () => {
        setConnected(false);
        if (!cancelled) {
          reconnectTimer = setTimeout(connect, 2000);
        }
      };
      ws.onerror = () => ws.close();
      ws.onmessage = (ev) => {
        try {
          const msg: WsMessage = JSON.parse(ev.data);
          setMessages((prev) => {
            const next = [...prev, msg];
            if (next.length > bufferSize) next.splice(0, next.length - bufferSize);
            return next;
          });
        } catch {
          // ignore
        }
      };
    };

    connect();
    return () => {
      cancelled = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      wsRef.current?.close();
    };
  }, [bufferSize]);

  return { messages, connected };
}
