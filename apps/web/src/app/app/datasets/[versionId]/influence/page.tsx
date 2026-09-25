"use client";

import Link from "next/link";
import { useState } from "react";
import { RequireAudit } from "@/components/audit-progress";
import { Meter } from "@/components/charts";
import { SampleModal } from "@/components/sample-detail";
import { Card, EmptyState, InfoTip, KindTag, Loading, Notice, PageHeader, SampleThumb, Select, VerdictBadge } from "@/components/ui";
import { qs } from "@/lib/api";
import { useDataset } from "@/lib/dataset";
import { fixed, pct } from "@/lib/format";
import { caseHref, useVersionApi } from "@/lib/hooks";

export default function InfluencePage() {
  const { versionId } = useDataset();
  const [sort, setSort] = useState("harmful");
  const [open, setOpen] = useState<string | null>(null);
  const { data } = useVersionApi<any>(`/influence${qs({ sort, limit: 60 })}`);
  const maxHarm = Math.max(1e-9, ...(data?.items ?? []).map((r: any) => r.harmful_influence));
  return (
    <RequireAudit>
      <PageHeader
        title="Model influence — why this sample matters"
        description="TracIn estimates over the baseline head's training checkpoints: which training samples the model had to memorise, and which pushed evaluation samples toward the wrong answer."
      />
      {!data ? (
        <Loading />
      ) : !data.available ? (
        <EmptyState title="Influence not computed">{data.reason}. Run a DEEP audit from the overview page.</EmptyState>
      ) : (
        <div className="space-y-5">
          <Notice tone="info" title="An approximation, labelled as one">
            {data.summary.note} Targets: {data.summary.targets}; {data.summary.failures_analyzed} failures analysed over {data.summary.checkpoints} checkpoints.
          </Notice>
          <Card
            title={
              <span className="flex items-center gap-2">
                Most influential training samples <KindTag kind="model_estimate" />
              </span>
            }
            actions={<Select label="Sort" value={sort} onChange={setSort} options={[{ value: "harmful", label: "Harm to evaluation failures" }, { value: "self", label: "Self-influence (memorisation)" }]} />}
          >
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-left text-xs text-subtle">
                  <tr>
                    <th className="pb-2 font-medium">Sample</th>
                    <th className="pb-2 font-medium">
                      <span className="inline-flex items-center gap-1">Harmful influence <InfoTip term="harmful_influence" /></span>
                    </th>
                    <th className="pb-2 font-medium">
                      <span className="inline-flex items-center gap-1">Self-influence pct <InfoTip term="self_influence" /></span>
                    </th>
                    <th className="pb-2 font-medium">Failures pushed</th>
                    <th className="pb-2 font-medium">Affected classes</th>
                    <th className="pb-2 font-medium">Case</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((r: any) => (
                    <tr key={r.sample.id} className="border-t hairline">
                      <td className="py-2 pr-3">
                        <button onClick={() => setOpen(r.sample.id)} className="flex items-center gap-2" aria-label={`Open ${r.sample.name}`}>
                          <SampleThumb s={r.sample} size={48} caption={false} />
                          <span className="text-xs">
                            <span className="font-medium">{r.sample.label}</span>
                            <br />
                            <span className="text-muted">{r.sample.name}</span>
                          </span>
                        </button>
                      </td>
                      <td className="w-40 py-2 pr-3">
                        <div className="text-xs tabular">{fixed(r.harmful_influence, 4)}</div>
                        <Meter value={r.harmful_influence / maxHarm} tone="bad" label="harmful influence" />
                      </td>
                      <td className="py-2 pr-3 tabular">{pct(r.self_influence_pct, 1)}</td>
                      <td className="py-2 pr-3 tabular">{r.failures_harmed}</td>
                      <td className="py-2 pr-3 text-xs text-muted">{r.affected_classes.join(", ") || "—"}</td>
                      <td className="py-2">
                        {r.case ? (
                          <Link href={caseHref(versionId, r.case.id)} className="text-xs hover:underline">
                            #{r.case.number} <VerdictBadge verdict={r.case.verdict} />
                          </Link>
                        ) : (
                          "—"
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        </div>
      )}
      <SampleModal sampleId={open} versionId={versionId} onClose={() => setOpen(null)} />
    </RequireAudit>
  );
}
