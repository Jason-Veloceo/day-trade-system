"use client";

import useSWR from "swr";
import { useEffect, useState } from "react";
import { fetcher, replaceActiveRuleSet } from "@/lib/api";
import type { RuleSet, RuleUpdateIn } from "@/lib/types";

const OPS = [
  "lt",
  "le",
  "gt",
  "ge",
  "eq",
  "between",
  "in",
  "contains_any",
  "contains_none",
  "within_minutes",
];

const FIELDS = [
  "last_close_price",
  "last_volume",
  "last_float",
  "last_rel_vol_today",
  "last_rel_vol_5min",
  "last_rel_gap",
  "last_rel_gain",
  "last_short_interest",
  "has_news",
  "news_age_minutes",
  "news_headline",
  "strategies_fired",
  "widgets_fired",
  "is_5_pillars",
];

const inputClass =
  "w-full rounded-md border border-neutral-300 bg-white px-2.5 py-1.5 text-sm text-neutral-900 placeholder:text-neutral-400 focus:border-neutral-900 focus:outline-none focus:ring-1 focus:ring-neutral-900";

export default function RulesPage() {
  const { data, mutate, isLoading } = useSWR<RuleSet>("/rules/active", fetcher, {
    revalidateOnFocus: false,
  });

  const [name, setName] = useState("default");
  const [note, setNote] = useState("");
  const [rules, setRules] = useState<RuleUpdateIn[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<Date | null>(null);

  useEffect(() => {
    if (!data) return;
    setName(data.name);
    setNote(data.note ?? "");
    setRules(
      data.rules.map((r) => ({
        rule_key: r.rule_key,
        field: r.field,
        op: r.op,
        value: r.value,
        enabled: r.enabled,
        severity: r.severity,
        note: r.note,
      }))
    );
  }, [data]);

  const updateRule = (idx: number, patch: Partial<RuleUpdateIn>) => {
    setRules((rs) => rs.map((r, i) => (i === idx ? { ...r, ...patch } : r)));
  };

  const removeRule = (idx: number) => setRules((rs) => rs.filter((_, i) => i !== idx));

  const addRule = () =>
    setRules((rs) => [
      ...rs,
      {
        rule_key: "new_rule",
        field: "last_rel_vol_5min",
        op: "ge",
        value: 3,
        enabled: true,
        severity: "hard",
      },
    ]);

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      await replaceActiveRuleSet({ name, note, rules });
      await mutate();
      setSavedAt(new Date());
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  if (isLoading) return <div className="p-4 text-sm text-neutral-500">Loading…</div>;

  return (
    <section>
      <div className="mb-4 flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-neutral-900">
            Filter Rules <span className="text-neutral-400">(Stage 1)</span>
          </h1>
          <p className="mt-1 text-sm text-neutral-500">
            Edit the active rule set. Saving creates a new version and marks it active. Old versions are kept for audit.
          </p>
        </div>
      </div>

      <div className="mb-4 rounded-xl border border-neutral-200 bg-white p-4 shadow-sm">
        <div className="grid grid-cols-1 gap-3 md:grid-cols-12">
          <label className="md:col-span-3">
            <span className="mb-1 block text-xs font-medium uppercase tracking-wider text-neutral-500">
              Rule set name
            </span>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              className={inputClass}
              placeholder="default"
            />
          </label>
          <label className="md:col-span-6">
            <span className="mb-1 block text-xs font-medium uppercase tracking-wider text-neutral-500">
              Note
            </span>
            <input
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="why this version…"
              className={inputClass}
            />
          </label>
          <div className="flex items-end gap-2 md:col-span-3">
            <button
              onClick={addRule}
              className="rounded-md border border-neutral-300 bg-white px-3 py-1.5 text-sm font-medium text-neutral-700 hover:bg-neutral-50"
            >
              + Add rule
            </button>
            <button
              onClick={save}
              disabled={saving}
              className="flex-1 rounded-md bg-neutral-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-neutral-800 disabled:opacity-50"
            >
              {saving ? "Saving…" : "Save & activate"}
            </button>
          </div>
        </div>
        {error && (
          <div className="mt-3 rounded-md bg-rose-50 px-3 py-2 text-sm text-rose-700 ring-1 ring-rose-200">
            {error}
          </div>
        )}
        {savedAt && !error && (
          <div className="mt-3 text-xs text-emerald-700">
            Saved at {savedAt.toLocaleTimeString()}.
          </div>
        )}
      </div>

      <div className="overflow-hidden rounded-xl border border-neutral-200 bg-white shadow-sm">
        <div className="grid grid-cols-12 items-center gap-2 border-b border-neutral-200 bg-neutral-50 px-4 py-2 text-[11px] font-semibold uppercase tracking-wider text-neutral-500">
          <div className="col-span-1">On</div>
          <div className="col-span-2">Key</div>
          <div className="col-span-2">Field</div>
          <div className="col-span-1">Op</div>
          <div className="col-span-3">Value</div>
          <div className="col-span-1">Severity</div>
          <div className="col-span-2 text-right">Actions</div>
        </div>

        {rules.map((r, i) => (
          <div
            key={i}
            className="grid grid-cols-12 items-center gap-2 border-b border-neutral-100 px-4 py-2.5 text-sm last:border-b-0 hover:bg-neutral-50/50"
          >
            <div className="col-span-1">
              <input
                type="checkbox"
                checked={r.enabled}
                onChange={(e) => updateRule(i, { enabled: e.target.checked })}
                className="h-4 w-4 rounded border-neutral-300 text-neutral-900 focus:ring-neutral-900"
              />
            </div>
            <input
              value={r.rule_key}
              onChange={(e) => updateRule(i, { rule_key: e.target.value })}
              className={`col-span-2 ${inputClass}`}
            />
            <select
              value={r.field}
              onChange={(e) => updateRule(i, { field: e.target.value })}
              className={`col-span-2 ${inputClass}`}
            >
              {FIELDS.map((f) => (
                <option key={f} value={f}>
                  {f}
                </option>
              ))}
            </select>
            <select
              value={r.op}
              onChange={(e) => updateRule(i, { op: e.target.value })}
              className={`col-span-1 ${inputClass}`}
            >
              {OPS.map((o) => (
                <option key={o} value={o}>
                  {o}
                </option>
              ))}
            </select>
            <input
              value={typeof r.value === "object" ? JSON.stringify(r.value) : String(r.value ?? "")}
              onChange={(e) => {
                const text = e.target.value;
                let parsed: unknown = text;
                try {
                  parsed = JSON.parse(text);
                } catch {
                  /* keep as string when not valid JSON */
                }
                updateRule(i, { value: parsed });
              }}
              className={`col-span-3 ${inputClass} font-mono text-xs`}
            />
            <select
              value={r.severity}
              onChange={(e) => updateRule(i, { severity: e.target.value })}
              className={`col-span-1 ${inputClass}`}
            >
              <option value="hard">hard</option>
              <option value="soft">soft</option>
            </select>
            <div className="col-span-2 text-right">
              <button
                onClick={() => removeRule(i)}
                className="rounded-md bg-white px-2.5 py-1 text-xs font-medium text-rose-700 ring-1 ring-rose-200 hover:bg-rose-50"
              >
                Remove
              </button>
            </div>
          </div>
        ))}

        {rules.length === 0 && (
          <div className="p-6 text-center text-sm text-neutral-500">
            No rules yet. Click <span className="font-medium">+ Add rule</span> to create one.
          </div>
        )}
      </div>
    </section>
  );
}
