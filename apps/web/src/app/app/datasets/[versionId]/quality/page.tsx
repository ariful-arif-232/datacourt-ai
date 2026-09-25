"use client";

import { useState } from "react";
import { RequireAudit } from "@/components/audit-progress";
import { BarList } from "@/components/charts";
import { SampleModal } from "@/components/sample-detail";
import { Badge, Card, EmptyState, KindTag, Loading, Notice, PageHeader, Pager, SampleThumb, Select } from "@/components/ui";
import { qs } from "@/lib/api";
import { useDataset } from "@/lib/dataset";
import { fixed, humanize, num } from "@/lib/format";
import { useVersionApi, useFilteredOffset } from "@/lib/hooks";

export default function QualityPage() {
  const { versionId } = useDataset();
  const [type, setType] = useState("");
  const [severity, setSeverity] = useState("");
  const [offset, setOffset] = useFilteredOffset(type, severity);
  const [open, setOpen] = useState<string | null>(null);
  const { data } = useVersionApi<any>(`/quality${qs({ finding_type: type, severity, offset, limit: 48 })}`);
  const byType: Record<string, number> = {};
  (data?.counts ?? []).forEach((c: any) => (byType[c.type] = (byType[c.type] ?? 0) + c.count));
  return (
    <RequireAudit>
      <PageHeader
        title="Visual quality engine"
        description="Measured image properties outside normal ranges. Each finding stores the measured value, the threshold and the rule version — and none of them proves an image is bad on its own."
      />
      {!data ? (
        <Loading />
      ) : (
        <div className="space-y-5">
          <div className="grid gap-5 lg:grid-cols-[1fr_1fr]">
            <Card title="Findings by type" info="quality" subtitle={`Rule set ${data.rule_version}`}>
              {Object.keys(byType).length ? (
                <BarList data={Object.entries(byType).sort((a, b) => b[1] - a[1]).map(([k, v]) => ({ label: humanize(k), value: v }))} onSelect={(l) => setType(Object.keys(byType).find((k) => humanize(k) === l) ?? "")} />
              ) : (
                <p className="text-sm text-muted">No quality rule fired.</p>
              )}
            </Card>
            <Card title="Thresholds used (versioned & configurable)">
              <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-xs">
                {Object.entries(data.thresholds).map(([k, v]) => (
                  <div key={k} className="flex justify-between border-b hairline py-1">
                    <span className="text-muted">{humanize(k)}</span>
                    <span className="tabular">{String(v)}</span>
                  </div>
                ))}
              </div>
              {Object.keys(data.rejected_files || {}).length > 0 && (
                <div className="mt-4">
                  <Notice tone="warn" title="Files rejected at ingestion">
                    {Object.entries(data.rejected_files).map(([r, n]) => `${r} (${n})`).join(" · ")}
                  </Notice>
                </div>
              )}
            </Card>
          </div>
          <Card
            title="Samples with findings"
            actions={
              <>
                <Select label="Type" value={type} onChange={setType} options={[{ value: "", label: "All types" }, ...Object.keys(byType).map((k) => ({ value: k, label: humanize(k) }))]} />
                <Select label="Severity" value={severity} onChange={setSeverity} options={["", "high", "medium", "low", "info"].map((v) => ({ value: v, label: v ? humanize(v) : "Any severity" }))} />
              </>
            }
          >
            {data.items.length === 0 ? (
              <EmptyState title="No findings for this filter" />
            ) : (
              <ul className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
                {data.items.map((f: any, i: number) => (
                  <li key={i} className="flex gap-3">
                    <button onClick={() => setOpen(f.sample.id)} aria-label={`Open ${f.sample.name}`}>
                      <SampleThumb s={f.sample} size={88} caption={false} />
                    </button>
                    <div className="min-w-0 text-xs">
                      <div className="flex flex-wrap items-center gap-1">
                        <Badge tone={f.severity === "high" ? "bad" : f.severity === "medium" ? "warn" : "neutral"}>{f.severity}</Badge>
                        <span className="font-medium">{humanize(f.type)}</span>
                        <KindTag kind={f.deterministic ? "measured" : "heuristic"} />
                      </div>
                      <p className="mt-1 text-muted">{f.description}</p>
                      <p className="mt-1 tabular text-subtle">
                        measured {fixed(f.value, 2)} {f.comparator} {f.threshold ?? "—"} · {f.sample.label} · {f.sample.split}
                      </p>
                    </div>
                  </li>
                ))}
              </ul>
            )}
            <Pager offset={offset} limit={48} total={data.total} onChange={setOffset} />
            <p className="mt-2 text-xs text-subtle">{num(data.total)} findings. “Heuristic” findings combine an absolute threshold with a dataset-relative outlier test.</p>
          </Card>
        </div>
      )}
      <SampleModal sampleId={open} versionId={versionId} onClose={() => setOpen(null)} />
    </RequireAudit>
  );
}
