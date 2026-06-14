import type {
  Candidate,
  CandidateDetail,
  CandidateStatus,
  EngineEvent,
  EngineRun,
  EngineStartIn,
  EngineStatus,
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

// ----- engine -----

export function getEngineStatus(): Promise<EngineStatus> {
  return jsonFetch(`/engine/status`);
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

export function startEngine(body: EngineStartIn): Promise<{ run_id: number; status: string }> {
  return jsonFetch(`/engine/start`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function stopEngine(): Promise<{ stopped: boolean }> {
  return jsonFetch(`/engine/stop`, { method: "POST" });
}

export function approveEngine(): Promise<{ handled: boolean }> {
  return jsonFetch(`/engine/approve`, { method: "POST" });
}

export function rejectEngine(): Promise<{ handled: boolean }> {
  return jsonFetch(`/engine/reject`, { method: "POST" });
}

export { API_BASE };
