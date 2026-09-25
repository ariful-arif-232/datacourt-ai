"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { RequireAudit } from "@/components/audit-progress";
import { LineageGraph, LineageLegend } from "@/components/lineage";
import { Badge, Card, EmptyState, Loading, Notice, PageHeader } from "@/components/ui";
import { useApi } from "@/lib/api";
import { useDataset } from "@/lib/dataset";
import { cx } from "@/components/ui";
import { useVersionApi } from "@/lib/hooks";

function LineageInner() {
  const params = useSearchParams();
  const router = useRouter();
  const { versionId } = useDataset();
  const { data: list } = useVersionApi<any>(`/duplicates?limit=100`);
  const selected = params.get("family") ?? list?.families?.[0]?.id ?? null;
  const { data } = useApi<any>(selected ? `/families/${selected}/lineage` : null);
  return (
    <>
      <PageHeader
        title="Data lineage graph"
        description="How visually matching samples relate: which copy looks like the source, what transformation links each member, which split each lives in — and where a family crosses into evaluation data."
      />
      {!list ? (
        <Loading />
      ) : list.families.length === 0 ? (
        <EmptyState title="No duplicate families in this dataset" />
      ) : (
        <div className="grid gap-5 lg:grid-cols-[240px_1fr]">
          <Card title="Families" className="lg:max-h-[75vh] lg:overflow-y-auto">
            <ul className="space-y-1">
              {list.families.map((f: any) => (
                <li key={f.id}>
                  <button
                    onClick={() => router.replace(`/app/datasets/${versionId}/lineage?family=${f.id}`)}
                    className={cx("flex w-full items-center justify-between rounded-md px-2 py-1.5 text-left text-[13px]", selected === f.id ? "bg-accent-soft" : "hover:bg-surface-2")}
                    aria-current={selected === f.id}
                  >
                    <span>
                      #{f.number} · {f.size}
                    </span>
                    <span className="flex gap-1">
                      {f.crosses_splits && <Badge tone="bad">leak</Badge>}
                      {f.label_conflict && <Badge tone="warn">labels</Badge>}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </Card>
          <div className="min-w-0 space-y-4">
            {!data ? (
              <Loading />
            ) : (
              <>
                <div className="flex flex-wrap items-center gap-2">
                  <h2 className="font-display text-xl">Family #{data.family.number}</h2>
                  <Badge>{data.family.kind}</Badge>
                  <Badge>{data.family.splits.join(" · ")}</Badge>
                  {data.family.label_conflict && <Badge tone="warn">labels: {data.family.labels.join(" / ")}</Badge>}
                </div>
                {data.leakage.map((lk: any, i: number) => (
                  <Notice key={i} tone={lk.risk === "low" ? "info" : "bad"} title={`Possible evaluation leakage · ${lk.risk} risk`}>
                    {lk.explanation}
                  </Notice>
                ))}
                <LineageLegend legend={data.legend} />
                <LineageGraph nodes={data.nodes} />
                <Card title="Why these samples were grouped">
                  <table className="w-full text-xs">
                    <thead className="text-left text-subtle">
                      <tr>
                        <th className="py-1 font-medium">Member</th>
                        <th className="py-1 font-medium">Relation</th>
                        <th className="py-1 font-medium">Rule</th>
                        <th className="py-1 font-medium">Evidence</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.nodes.map((n: any) => (
                        <tr key={n.id} className="border-t hairline align-top">
                          <td className="py-1.5 pr-3">{n.sample.name}</td>
                          <td className="py-1.5 pr-3">{n.relation ?? "root"}</td>
                          <td className="py-1.5 pr-3 text-muted">{n.evidence?.rule ?? n.evidence?.role}</td>
                          <td className="py-1.5 font-mono text-[10.5px] text-muted">
                            {["cosine", "phash_distance", "detail_ncc", "detail_ncc_mirror", "aspect_ratio_diff", "photometric_delta"]
                              .filter((k) => n.evidence?.[k] !== undefined)
                              .map((k) => `${k}=${n.evidence[k]}`)
                              .join(" ")}
                            {n.evidence?.keypoints && ` inliers=${n.evidence.keypoints.inliers} overlap_ncc=${n.evidence.keypoints.overlap_detail_ncc}`}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </Card>
              </>
            )}
          </div>
        </div>
      )}
    </>
  );
}

export default function LineagePage() {
  return (
    <RequireAudit>
      <Suspense>
        <LineageInner />
      </Suspense>
    </RequireAudit>
  );
}
