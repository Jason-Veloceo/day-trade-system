import type {
  Candidate,
  CandidateDetail,
  CandidateStatus,
  EngineEvent,
  EnginePortfolioStatus,
  EngineRegistryStatus,
  EngineRun,
  EngineStartIn,
  RuleSet,
  RuleSetUpdateIn,
} from "./types";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

async function jsonFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    cache: "no-store",
    ...init,
    headers: {
      "content-type": "application/json",
      ...(init?.headers ?? {}),
    },
  });
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText}`);
  }
  return res.json();
}

export const fetcher = (path: string) => jsonFetch(path);

export function listCandidates(params: {
  status?: CandidateStatus;
  trading_day?: string;
  limit?: number;
}): Promise<Candidate[]> {
  const qs = new URLSearchParams();
  if (params.status) qs.set("status", params.status);
  if (params.trading_day) qs.set("trading_day", params.trading_day);
  if (params.limit) qs.set("limit", String(params.limit));
  return jsonFetch(`/candidates?${qs.toString()}`);
}

export function getCandidate(id: number | string): Promise<CandidateDetail> {
  return jsonFetch(`/candidates/${id}`);
}

export function getActiveRuleSet(): Promise<RuleSet> {
  return jsonFetch(`/rules/active`);
}

export function replaceActiveRuleSet(payload: RuleSetUpdateIn): Promise<RuleSet> {
  return jsonFetch(`/rules/active`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

// ----- engine (v1.3 multi-engine registry) -----

// GET /engine/status -> all active engines + portfolio gate + slot caps.
export function getEngineStatus(): Promise<EngineRegistryStatus> {
  return jsonFetch(`/engine/status`);
}

// GET /engine/portfolio -> just the portfolio gate snapshot.
export function getEnginePortfolio(): Promise<EnginePortfolioStatus> {
  return jsonFetch(`/engine/portfolio`);
}

export function listEngineRuns(limit = 50): Promise<EngineRun[]> {
  return jsonFetch(`/engine/runs?limit=${limit}`);
}

export function getEngineRunEvents(
  runId: number,
  opts?: { limit?: number; after_id?: number; event_type?: string }
): Promise<EngineEvent[]> {
  const qs = new URLSearchParams();
  if (opts?.limit) qs.set("limit", String(opts.limit));
  if (opts?.after_id) qs.set("after_id", String(opts.after_id));
  if (opts?.event_type) qs.set("event_type", opts.event_type);
  return jsonFetch(`/engine/runs/${runId}/events?${qs.toString()}`);
}

// POST /engine/start -> add a new engine for body.symbol.
// 409 if the symbol already has an active engine or the registry is full.
export function startEngine(
  body: EngineStartIn,
): Promise<{ run_id: number; symbol: string; status: string }> {
  return jsonFetch(`/engine/start`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

// POST /engine/stop?symbol=X -> stop one engine (idempotent).
export function stopEngine(symbol: string): Promise<{ stopped: boolean; symbol: string }> {
  return jsonFetch(`/engine/stop?symbol=${encodeURIComponent(symbol)}`, { method: "POST" });
}

// POST /engine/stop_all -> stop every active engine.
export function stopAllEngines(): Promise<{ stopped: number }> {
  return jsonFetch(`/engine/stop_all`, { method: "POST" });
}

// POST /engine/approve?run_id=X -> approve the pending entry on that engine.
export function approveEngine(runId: number): Promise<{ handled: boolean }> {
  return jsonFetch(`/engine/approve?run_id=${runId}`, { method: "POST" });
}

// POST /engine/reject?run_id=X -> reject the pending entry on that engine.
export function rejectEngine(runId: number): Promise<{ handled: boolean }> {
  return jsonFetch(`/engine/reject?run_id=${runId}`, { method: "POST" });
}

// POST /engine/portfolio/reset_kill_switch -> manually clear the daily
// kill switch. Realized P&L and trade count are preserved.
export function resetPortfolioKillSwitch(): Promise<EnginePortfolioStatus> {
  return jsonFetch(`/engine/portfolio/reset_kill_switch`, { method: "POST" });
}

export { API_BASE };
