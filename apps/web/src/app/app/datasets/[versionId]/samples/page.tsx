"use client";

import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { SampleModal } from "@/components/sample-detail";
import { EmptyState, ErrorState, inputCls, Loading, PageHeader, Pager, SampleThumb, Select, VerdictBadge } from "@/components/ui";
import { qs } from "@/lib/api";
import { useDataset } from "@/lib/dataset";
import { useVersionApi, useFilteredOffset } from "@/lib/hooks";

const FINDINGS = [
  ["", "Any finding"],
  ["label", "Label review"],
  ["duplicate", "In a duplicate family"],
  ["leakage", "In a leakage finding"],
  ["privacy", "Privacy flag"],
  ["potential_blur", "Potential blur"],
  ["potential_underexposure", "Underexposure"],
  ["potential_overexposure", "Overexposure"],
  ["low_contrast", "Low contrast"],
  ["low_resolution", "Low resolution"],
  ["near_empty", "Near-empty"],
  ["unusual_aspect_ratio", "Unusual aspect ratio"],
  ["extension_mismatch", "Extension mismatch"],
];
const VERDICTS = ["", "LEAKAGE_ACTION_NEEDED", "POSSIBLE_RELABEL", "STRONG_REVIEW", "POSSIBLE_REMOVE", "REVIEW", "UNCERTAIN", "LIKELY_RARE", "KEEP"];

function SamplesInner() {
  const params = useSearchParams();
  const { version, versionId } = useDataset();
  const [q, setQ] = useState(params.get("q") ?? "");
  const [debounced, setDebounced] = useState(q);
  const [cls, setCls] = useState("");
  const [split, setSplit] = useState("");
  const [finding, setFinding] = useState(params.get("finding") ?? "");
  const [verdict, setVerdict] = useState("");
  const [sort, setSort] = useState("path");
  const [open, setOpen] = useState<string | null>(null);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(q), 300);
    return () => clearTimeout(t);
  }, [q]);
  const [offset, setOffset] = useFilteredOffset(debounced, cls, split, finding, verdict, sort);
  const { data, error, isLoading } = useVersionApi<any>(
    `/samples${qs({ q: debounced, class_name: cls, split, finding, verdict, sort, offset, limit: 60 })}`,
    { requireAudit: false },
  );
  const classes = Object.keys(version?.stats?.class_counts ?? {}).sort();
  const splits = Object.keys(version?.stats?.splits ?? {});
  return (
    <div>
      <PageHeader title="Sample explorer" description="Search by file name or SHA-256, filter by class, split, finding or verdict. Click a sample for its full evidence." />
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search file name or SHA-256…" aria-label="Search samples" className={`${inputCls} h-8 max-w-xs py-1`} />
        <Select label="Class" value={cls} onChange={setCls} options={[{ value: "", label: "All classes" }, ...classes.map((c) => ({ value: c, label: c }))]} />
        <Select label="Split" value={split} onChange={setSplit} options={[{ value: "", label: "All splits" }, ...splits.map((c) => ({ value: c, label: c }))]} />
        <Select label="Finding" value={finding} onChange={setFinding} options={FINDINGS.map(([v, l]) => ({ value: v, label: l }))} />
        <Select label="Verdict" value={verdict} onChange={setVerdict} options={VERDICTS.map((v) => ({ value: v, label: v ? v.replace(/_/g, " ").toLowerCase() : "Any verdict" }))} />
        <Select label="Sort" value={sort} onChange={setSort} options={[{ value: "path", label: "Sort: name" }, { value: "suspicion", label: "Sort: label suspicion" }, { value: "priority", label: "Sort: review priority" }]} />
        {data && <span className="text-xs text-muted tabular">{data.total.toLocaleString()} samples</span>}
      </div>
      <ErrorState error={error} />
      {isLoading && <Loading />}
      {data && data.items.length === 0 && <EmptyState title="No samples match these filters" />}
      <ul className="grid grid-cols-[repeat(auto-fill,minmax(120px,1fr))] gap-4">
        {(data?.items ?? []).map((s: any) => (
          <li key={s.id}>
            <button className="text-left" onClick={() => setOpen(s.id)} aria-label={`Open ${s.name}`}>
              <SampleThumb s={s} size={120} badge={s.case ? <VerdictBadge verdict={s.case.verdict} /> : undefined} />
            </button>
          </li>
        ))}
      </ul>
      {data && <Pager offset={offset} limit={60} total={data.total} onChange={setOffset} />}
      <SampleModal sampleId={open} versionId={versionId} onClose={() => setOpen(null)} />
    </div>
  );
}

export default function SamplesPage() {
  return (
    <Suspense>
      <SamplesInner />
    </Suspense>
  );
}
