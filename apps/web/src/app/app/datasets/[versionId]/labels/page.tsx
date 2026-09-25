"use client";

import Link from "next/link";
import { useState } from "react";
import { RequireAudit } from "@/components/audit-progress";
import { Meter } from "@/components/charts";
import { SampleModal } from "@/components/sample-detail";
import { Badge, Card, EmptyState, InfoTip, KindTag, Loading, Notice, PageHeader, Pager, SampleThumb, Select, Stat, VerdictBadge } from "@/components/ui";
import { qs } from "@/lib/api";
import { useDataset } from "@/lib/dataset";
import { fixed, humanize, num, pct } from "@/lib/format";
import { caseHref, useVersionApi, useFilteredOffset } from "@/lib/hooks";

const ACTION_TONE: Record<string, "bad" | "warn" | "accent" | "neutral"> = { REVIEW: "bad", LOW_PRIORITY_REVIEW: "warn", LIKELY_RARE: "accent", NO_ACTION: "neutral" };

export default function LabelsPage() {
  const { version, versionId } = useDataset();
  const [action, setAction] = useState("");
  const [cls, setCls] = useState("");
  const [offset, setOffset] = useFilteredOffset(action, cls);
  const [open, setOpen] = useState<string | null>(null);
  const { data } = useVersionApi<any>(`/labels${qs({ action, class_name: cls, offset, limit: 40 })}`);
  const classes = Object.keys(version?.stats?.class_counts ?? {}).sort();
  const rel = data?.summary?.witness_reliability ?? {};
  const weak = Object.entries(rel).filter(([, v]) => (v as number) < 0.35);
  return (
    <RequireAudit>
      <PageHeader
        title="Label forensics"
        description="Independent witnesses — a cross-validated baseline model, visual neighbours, class centroids, training dynamics and duplicate relations — are combined into an evidence score. Model disagreement is one signal, never a verdict."
      />
      {!data ? (
        <Loading />
      ) : (
        <div className="space-y-5">
          <div className="grid gap-3 md:grid-cols-4">
            {["REVIEW", "LOW_PRIORITY_REVIEW", "LIKELY_RARE", "NO_ACTION"].map((a) => (
              <Stat key={a} label={humanize(a)} value={num(data.summary.actions?.[a] ?? 0)} />
            ))}
          </div>
          <Card title="Witness reliability on this dataset" info="reliability" subtitle="Weights are scaled by how well each witness predicts the given labels here (chance-corrected balanced accuracy).">
            <div className="grid gap-4 sm:grid-cols-3">
              {Object.entries(rel).map(([k, v]) => (
                <div key={k}>
                  <div className="mb-1 flex justify-between text-sm">
                    <span>{humanize(k)}</span>
                    <span className="tabular text-muted">
                      {fixed(v as number, 2)} → weight {fixed(data.summary.effective_weights?.[k], 3)}
                    </span>
                  </div>
                  <Meter value={v as number} tone={(v as number) < 0.35 ? "warn" : "accent"} label={`${k} reliability`} />
                </div>
              ))}
            </div>
            {weak.length > 0 && (
              <div className="mt-3">
                <Notice tone="warn">
                  {weak.map(([k]) => humanize(k)).join(", ")} evidence is weak on this dataset and is discounted. A stronger embedding backend (DINOv2) typically raises neighbour and centroid reliability.
                </Notice>
              </div>
            )}
          </Card>
          <Card
            title="Samples ranked by suspicion"
            info="suspicion"
            actions={
              <>
                <Select label="Action" value={action} onChange={setAction} options={[{ value: "", label: "All actions" }, ...["REVIEW", "LOW_PRIORITY_REVIEW", "LIKELY_RARE", "NO_ACTION"].map((a) => ({ value: a, label: humanize(a) }))]} />
                <Select label="Class" value={cls} onChange={setCls} options={[{ value: "", label: "All classes" }, ...classes.map((c) => ({ value: c, label: c }))]} />
              </>
            }
          >
            {data.items.length === 0 ? (
              <EmptyState title="No label findings for this filter" />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead className="text-left text-xs text-subtle">
                    <tr>
                      <th className="pb-2 font-medium">Sample</th>
                      <th className="pb-2 font-medium">Label → model</th>
                      <th className="pb-2 font-medium">
                        <span className="inline-flex items-center gap-1">Suspicion <InfoTip term="suspicion" /></span>
                      </th>
                      <th className="pb-2 font-medium">
                        <span className="inline-flex items-center gap-1">Neighbours <InfoTip term="neighbor_agreement" /></span>
                      </th>
                      <th className="pb-2 font-medium">
                        <span className="inline-flex items-center gap-1">Centroid <InfoTip term="centroid_ratio" /></span>
                      </th>
                      <th className="pb-2 font-medium">Action</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.items.map((r: any) => (
                      <tr key={r.sample.id} className="border-t hairline align-middle">
                        <td className="py-2 pr-3">
                          <button onClick={() => setOpen(r.sample.id)} className="flex items-center gap-2 text-left" aria-label={`Open ${r.sample.name}`}>
                            <SampleThumb s={r.sample} size={52} caption={false} />
                            <span className="max-w-40 truncate text-xs text-muted">{r.sample.name}</span>
                          </button>
                        </td>
                        <td className="py-2 pr-3">
                          <div className="font-medium">{r.evidence.current_label}</div>
                          <div className="text-xs text-muted">
                            model: {r.evidence.predicted_label} ({pct(r.evidence.p_predicted)}) <KindTag kind="model_prediction" />
                          </div>
                        </td>
                        <td className="w-36 py-2 pr-3">
                          <div className="tabular text-xs">{fixed(r.suspicion, 2)}</div>
                          <Meter value={r.suspicion} tone={r.suspicion > 0.55 ? "bad" : "warn"} label="suspicion" />
                        </td>
                        <td className="py-2 pr-3 text-xs tabular">{pct(r.neighbor_agreement)} agree</td>
                        <td className="py-2 pr-3 text-xs tabular">
                          {fixed(r.centroid_ratio, 2)}
                          {r.centroid_ratio > 0.5 && <span className="text-muted"> → {r.evidence.nearest_other_class}</span>}
                        </td>
                        <td className="py-2">
                          <div className="flex flex-col items-start gap-1">
                            <Badge tone={ACTION_TONE[r.action]}>{humanize(r.action)}</Badge>
                            {r.case && (
                              <Link href={caseHref(versionId, r.case.id)} className="text-xs text-accent hover:underline">
                                case #{r.case.number} <VerdictBadge verdict={r.case.verdict} />
                              </Link>
                            )}
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <Pager offset={offset} limit={40} total={data.total} onChange={setOffset} />
          </Card>
        </div>
      )}
      <SampleModal sampleId={open} versionId={versionId} onClose={() => setOpen(null)} />
    </RequireAudit>
  );
}
