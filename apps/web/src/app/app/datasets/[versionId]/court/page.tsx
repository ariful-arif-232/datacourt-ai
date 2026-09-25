"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { RequireAudit } from "@/components/audit-progress";
import { Badge, Card, cx, EmptyState, inputCls, Loading, PageHeader, Pager, SampleThumb, Select, SplitTag, StatusBadge, VerdictBadge } from "@/components/ui";
import { qs } from "@/lib/api";
import { useDataset } from "@/lib/dataset";
import { fixed, humanize, num } from "@/lib/format";
import { useVersionApi, useFilteredOffset } from "@/lib/hooks";

const VERDICTS = ["LEAKAGE_ACTION_NEEDED", "POSSIBLE_RELABEL", "STRONG_REVIEW", "POSSIBLE_REMOVE", "REVIEW", "UNCERTAIN", "LIKELY_RARE", "KEEP"];
const CATEGORIES = ["label", "leakage", "duplicate", "quality", "rare", "shortcut", "influence", "privacy"];

function CourtInner() {
  const { version, versionId } = useDataset();
  const params = useSearchParams();
  const [verdict, setVerdict] = useState(params.get("verdict") ?? "");
  const [status, setStatus] = useState(params.get("status") ?? "");
  const [category, setCategory] = useState("");
  const [cls, setCls] = useState("");
  const [split, setSplit] = useState("");
  const [uncertainty, setUncertainty] = useState("");
  const [sort, setSort] = useState("priority");
  const [q, setQ] = useState(params.get("q") ?? "");
  const [debounced, setDebounced] = useState(q);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(q), 300);
    return () => clearTimeout(t);
  }, [q]);
  const [offset, setOffset] = useFilteredOffset(verdict, status, category, cls, split, uncertainty, sort, debounced);
  const { data } = useVersionApi<any>(`/cases${qs({ verdict, status, category, class_name: cls, split, uncertainty, sort, q: debounced, offset, limit: 40 })}`);
  const totals: Record<string, number> = {};
  const statusTotals: Record<string, number> = {};
  (data?.counts ?? []).forEach((c: any) => {
    totals[c.verdict] = (totals[c.verdict] ?? 0) + c.count;
    statusTotals[c.status] = (statusTotals[c.status] ?? 0) + c.count;
  });
  const classes = Object.keys(version?.stats?.class_counts ?? {}).sort();
  return (
    <>
      <PageHeader
        title="Court cases"
        description="Every important suspicious sample becomes a case: witnesses give evidence for the prosecution and the defense, a deterministic jury issues a verdict with reason codes, and a human makes the final decision."
        actions={<Link href={`/app/datasets/${versionId}/review`} className="text-sm text-accent hover:underline">Plan a review session →</Link>}
      />
      <div className="mb-4 flex flex-wrap gap-2">
        <button onClick={() => setVerdict("")} className={cx("rounded-full border px-3 py-1 text-xs", !verdict ? "border-accent bg-accent-soft" : "border-border hover:bg-surface-2")}>
          All <span className="tabular text-muted">{num(Object.values(totals).reduce((a, b) => a + b, 0))}</span>
        </button>
        {VERDICTS.filter((v) => totals[v]).map((v) => (
          <button key={v} onClick={() => setVerdict(v === verdict ? "" : v)} className={cx("flex items-center gap-1.5 rounded-full border px-2 py-1 text-xs", verdict === v ? "border-accent bg-accent-soft" : "border-border hover:bg-surface-2")} aria-pressed={verdict === v}>
            <VerdictBadge verdict={v} /> <span className="tabular text-muted">{num(totals[v])}</span>
          </button>
        ))}
      </div>
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Case # or file name…" aria-label="Search cases" className={`${inputCls} h-8 max-w-52 py-1`} />
        <Select label="Status" value={status} onChange={setStatus} options={[{ value: "", label: "Any status" }, ...["open", "decided", "disputed", "resolved"].map((s) => ({ value: s, label: `${humanize(s)} (${statusTotals[s] ?? 0})` }))]} />
        <Select label="Category" value={category} onChange={setCategory} options={[{ value: "", label: "Any category" }, ...CATEGORIES.map((c) => ({ value: c, label: humanize(c) }))]} />
        <Select label="Class" value={cls} onChange={setCls} options={[{ value: "", label: "Any class" }, ...classes.map((c) => ({ value: c, label: c }))]} />
        <Select label="Split" value={split} onChange={setSplit} options={[{ value: "", label: "Any split" }, ...Object.keys(version?.stats?.splits ?? {}).map((s) => ({ value: s, label: s }))]} />
        <Select label="Uncertainty" value={uncertainty} onChange={setUncertainty} options={[{ value: "", label: "Any uncertainty" }, ...["high", "medium", "low"].map((s) => ({ value: s, label: `${humanize(s)} uncertainty` }))]} />
        <Select
          label="Sort"
          value={sort}
          onChange={setSort}
          options={[
            { value: "priority", label: "Sort: review priority" },
            { value: "impact", label: "Sort: model impact" },
            { value: "uncertainty", label: "Sort: uncertainty" },
            { value: "newest", label: "Sort: recently updated" },
            { value: "class", label: "Sort: class" },
            { value: "name", label: "Sort: sample name" },
            { value: "case", label: "Sort: case number" },
          ]}
        />
      </div>
      {!data ? (
        <Loading />
      ) : data.items.length === 0 ? (
        <EmptyState title="No cases match these filters" />
      ) : (
        <Card>
          <ul className="divide-y divide-border">
            {data.items.map((c: any) => (
              <li key={c.id}>
                <Link href={`/app/datasets/${versionId}/court/${c.id}`} className="flex w-full items-center gap-4 py-3 text-left hover:bg-surface-2/60">
                  <SampleThumb s={c.sample} size={56} caption={false} />
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-mono text-xs text-subtle">#{c.case_number}</span>
                      <VerdictBadge verdict={c.verdict} />
                      <StatusBadge status={c.status} />
                      <span className="text-sm font-medium">{humanize(c.primary_concern)}</span>
                    </div>
                    <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-muted">
                      <span>{c.sample.label}</span>
                      <SplitTag split={c.sample.split} />
                      <span className="truncate">{c.sample.name}</span>
                      {c.categories.map((x: string) => (
                        <Badge key={x}>{x}</Badge>
                      ))}
                    </div>
                  </div>
                  <div className="hidden text-right text-xs sm:block">
                    <div className="tabular">priority {fixed(c.priority, 2)}</div>
                    <div className="text-muted">{humanize(c.uncertainty)} uncertainty</div>
                    <div className="text-subtle tabular">~{fixed(c.est_review_minutes, 1)} min</div>
                  </div>
                </Link>
              </li>
            ))}
          </ul>
          <Pager offset={offset} limit={40} total={data.total} onChange={setOffset} />
        </Card>
      )}
    </>
  );
}

export default function CourtPage() {
  return (
    <RequireAudit>
      <Suspense>
        <CourtInner />
      </Suspense>
    </RequireAudit>
  );
}
