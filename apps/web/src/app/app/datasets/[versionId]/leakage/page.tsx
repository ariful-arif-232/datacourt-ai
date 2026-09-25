"use client";

import { useState } from "react";
import { RequireAudit } from "@/components/audit-progress";
import { Histogram } from "@/components/charts";
import { Badge, Button, Card, EmptyState, KindTag, Loading, PageHeader, SampleThumb, Select, Stat } from "@/components/ui";
import { qs } from "@/lib/api";
import { useDataset } from "@/lib/dataset";
import { fixed, humanize, num, pct } from "@/lib/format";
import { useVersionApi } from "@/lib/hooks";

const RISK_TONE: Record<string, "bad" | "warn" | "info"> = { critical: "bad", high: "bad", medium: "warn", low: "info" };

export default function LeakagePage() {
  const { versionId } = useDataset();
  const [risk, setRisk] = useState("");
  const { data } = useVersionApi<any>(`/leakage${qs({ risk })}`);
  const splits = data ? Object.entries(data.integrity.splits ?? {}) : [];
  const presence = data?.integrity.class_presence ?? {};
  const presenceSplits = Object.keys(presence);
  const classes = presenceSplits.length ? Object.keys(presence[presenceSplits[0]]) : [];
  return (
    <RequireAudit>
      <PageHeader
        title="Train / validation / test leakage"
        description="Evaluation samples with a byte-identical, transformed or near-identical counterpart in another split. Visual similarity alone is never presented as proof of the same subject."
      />
      {!data ? (
        <Loading />
      ) : (
        <div className="space-y-5">
          <div className="grid gap-3 md:grid-cols-4">
            {["critical", "high", "medium", "low"].map((r) => (
              <Stat key={r} label={`${humanize(r)} risk`} info="leakage_risk" value={num(data.summary.by_risk?.[r] ?? 0)} tone={data.summary.by_risk?.[r] && r !== "low" ? "bad" : undefined} sub={r === "low" ? `similar-subject threshold cos ≥ ${fixed(data.summary.similar_subject_threshold, 3)}` : undefined} />
            ))}
          </div>
          {splits.length > 0 && (
            <div className="grid gap-5 lg:grid-cols-2">
              {splits.map(([sp, m]: [string, any]) => (
                <Card key={sp} title={`${sp} split integrity`} subtitle={`${num(m.count)} samples · ${pct(m.contaminated_fraction, 1)} linked to training data`}>
                  <div className="mb-3 grid grid-cols-3 gap-2 text-center text-xs">
                    <div className="rounded-lg bg-surface-2 p-2">
                      <div className="text-lg font-semibold tabular">{num(m.exact_in_train)}</div>byte-identical in train
                    </div>
                    <div className="rounded-lg bg-surface-2 p-2">
                      <div className="text-lg font-semibold tabular">{num(m.family_linked_to_train)}</div>in a family with train
                    </div>
                    <div className="rounded-lg bg-surface-2 p-2">
                      <div className="text-lg font-semibold tabular">{fixed(m.nearest_train_similarity_median, 3)}</div>median nearest-train cos
                    </div>
                  </div>
                  {m.nearest_train_similarity_hist && <Histogram {...m.nearest_train_similarity_hist} xLabel="cosine similarity to nearest training image" format={(v) => v.toFixed(2)} height={90} />}
                </Card>
              ))}
            </div>
          )}
          {classes.length > 0 && presenceSplits.length > 1 && (
            <Card title="Class presence per split">
              <div className="overflow-x-auto">
                <table className="text-xs">
                  <thead>
                    <tr>
                      <th className="p-1.5 text-left font-medium text-subtle">class</th>
                      {presenceSplits.map((s) => (
                        <th key={s} className="p-1.5 font-medium text-subtle">
                          {s}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {classes.map((c) => (
                      <tr key={c} className="border-t hairline">
                        <td className="p-1.5">{c}</td>
                        {presenceSplits.map((s) => (
                          <td key={s} className="p-1.5 text-center">
                            {presence[s][c] ? <span className="text-ok">✓ present</span> : <span className="font-medium text-bad">✕ missing</span>}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          )}
          <Card
            title="Leakage findings"
            actions={<Select label="Risk" value={risk} onChange={setRisk} options={["", "critical", "high", "medium", "low"].map((v) => ({ value: v, label: v ? humanize(v) : "All risks" }))} />}
          >
            {data.findings.length === 0 ? (
              <EmptyState title="No leakage found">No cross-split duplicate families or suspiciously similar cross-split clusters.</EmptyState>
            ) : (
              <ul className="divide-y divide-border">
                {data.findings.map((f: any) => (
                  <li key={f.id} className="py-4 first:pt-0">
                    <div className="mb-2 flex flex-wrap items-center gap-2 text-sm">
                      <Badge tone={RISK_TONE[f.risk]}>{humanize(f.risk)} risk</Badge>
                      <span className="font-medium">{humanize(f.kind)}</span>
                      <KindTag kind={f.kind === "exact_cross_split" ? "measured" : "heuristic"} />
                      <span className="text-xs text-subtle">
                        {f.splits.join(" ↔ ")} · {f.size} samples · max cos {fixed(f.max_similarity, 3)}
                      </span>
                      <div className="flex-1" />
                      {f.family_id && (
                        <Button size="sm" href={`/app/datasets/${versionId}/lineage?family=${f.family_id}`}>
                          Lineage
                        </Button>
                      )}
                    </div>
                    <p className="mb-2 text-[13px] text-muted">{f.explanation}</p>
                    <ul className="flex gap-3 overflow-x-auto pb-1">
                      {f.samples.map((s: any) => (
                        <li key={s.id} className="shrink-0">
                          <SampleThumb s={s} size={84} />
                        </li>
                      ))}
                    </ul>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>
      )}
    </RequireAudit>
  );
}
