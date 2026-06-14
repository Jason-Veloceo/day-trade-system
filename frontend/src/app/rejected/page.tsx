"use client";

import useSWR from "swr";
import { useEffect } from "react";
import { fetcher } from "@/lib/api";
import type { Candidate } from "@/lib/types";
import { RejectedHeader, RejectedRow } from "@/components/RejectedRow";
import { useBrokerStream } from "@/lib/ws";

export default function Rejected() {
  const { data, error, isLoading, mutate } = useSWR<Candidate[]>(
    "/candidates?status=failed_filter&limit=300",
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
          <h1 className="text-2xl font-semibold tracking-tight text-neutral-900">Rejected</h1>
          <p className="mt-1 text-sm text-neutral-500">
            Candidates that failed at least one hard rule. Chips show which rules killed each one.
          </p>
        </div>
        <div className="text-sm text-neutral-500">
          {data ? `${data.length} rejected` : ""}
        </div>
      </div>
      <div className="overflow-hidden rounded-xl border border-neutral-200 bg-white shadow-sm">
        <RejectedHeader />
        {isLoading && <div className="p-4 text-sm text-neutral-500">Loading…</div>}
        {error && (
          <div className="p-4 text-sm text-rose-700">Failed to load candidates.</div>
        )}
        {data?.length === 0 && (
          <div className="p-6 text-center text-sm text-neutral-500">No rejections yet.</div>
        )}
        {data?.map((c) => (
          <RejectedRow key={c.id} candidate={c} />
        ))}
      </div>
    </section>
  );
}
