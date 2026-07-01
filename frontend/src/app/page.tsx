"use client";

import useSWR from "swr";
import { useEffect } from "react";
import { fetcher } from "@/lib/api";
import type { Candidate } from "@/lib/types";
import { CandidateHeader, CandidateRow } from "@/components/CandidateRow";
import { useBrokerStream } from "@/lib/ws";

export default function Watchlist() {
  const { data, error, isLoading, mutate } = useSWR<Candidate[]>(
    "/candidates?status=passed&limit=200",
    fetcher,
    { revalidateOnFocus: false }
  );
  const { messages } = useBrokerStream();

  useEffect(() => {
    const last = messages[messages.length - 1];
    if (last?.topic === "candidate.update" || last?.topic === "rules.changed") {
      mutate();
    }
  }, [messages, mutate]);

  return (
    <section>
      <div className="mb-4 flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-neutral-900">Watchlist</h1>
          <p className="mt-1 text-sm text-neutral-500">
            Candidates that passed the active Stage-1 filter rules. Sorted by most recent alert.
          </p>
        </div>
        <div className="text-sm text-neutral-500">
          {data ? `${data.length} candidates` : ""}
        </div>
      </div>
      <div className="overflow-hidden rounded-xl border border-neutral-200 bg-white shadow-sm">
        <CandidateHeader />
        {isLoading && <div className="p-4 text-sm text-neutral-500">Loading…</div>}
        {error && (
          <div className="p-4 text-sm text-rose-700">
            Failed to load candidates. Is the backend running at{" "}
            <code className="font-mono">
              {process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000"}
            </code>
            ?
          </div>
        )}
        {data?.length === 0 && (
          <div className="p-6 text-center text-sm text-neutral-500">
            No candidates yet. Once DTD alerts arrive and pass filters, they will appear here.
          </div>
        )}
        {data?.map((c) => (
          <CandidateRow key={c.id} candidate={c} />
        ))}
      </div>
    </section>
  );
}
