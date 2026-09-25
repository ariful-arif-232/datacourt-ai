"use client";

import { useState } from "react";
import { RequireAudit } from "@/components/audit-progress";
import { SampleModal } from "@/components/sample-detail";
import { Card, EmptyState, Loading, Notice, PageHeader, Pager, SampleThumb, Tabs } from "@/components/ui";
import { qs } from "@/lib/api";
import { useDataset } from "@/lib/dataset";
import { fixed, humanize } from "@/lib/format";
import { useVersionApi, useFilteredOffset } from "@/lib/hooks";

const HYPS = ["rare_valid", "likely_mislabeled", "ambiguous", "domain_shifted", "poor_quality", "uncertain"];
const HYP_COLOR: Record<string, string> = {
  likely_mislabeled: "var(--series-8)",
  rare_valid: "var(--series-1)",
  poor_quality: "var(--series-4)",
  domain_shifted: "var(--series-7)",
  ambiguous: "var(--series-2)",
};

export default function RareOrWrongPage() {
  const { versionId } = useDataset();
  const [hyp, setHyp] = useState("rare_valid");
  const [offset, setOffset] = useFilteredOffset(hyp);
  const [open, setOpen] = useState<string | null>(null);
  const { data } = useVersionApi<any>(`/rare-or-wrong${qs({ hypothesis: hyp, offset, limit: 24 })}`);
  return (
    <RequireAudit>
      <PageHeader
        title="Rare or wrong?"
        description="Unusual data is not automatically bad data. For every outlier or suspicious sample, competing hypotheses are scored from the evidence — so valuable rare examples are protected instead of deleted."
      />
      {!data ? (
        <Loading />
      ) : (
        <div className="space-y-4">
          <Notice tone="info">{data.note}</Notice>
          <Tabs value={hyp} onChange={setHyp} tabs={HYPS.map((h) => ({ value: h, label: humanize(h), count: data.counts[h] ?? 0 }))} />
          {data.items.length === 0 ? (
            <EmptyState title={`No samples with the ${humanize(hyp).toLowerCase()} hypothesis`} />
          ) : (
            <ul className="grid gap-4 md:grid-cols-2">
              {data.items.map((r: any) => (
                <li key={r.sample.id}>
                  <Card>
                    <div className="flex gap-4">
                      <button onClick={() => setOpen(r.sample.id)} aria-label={`Open ${r.sample.name}`}>
                        <SampleThumb s={r.sample} size={112} />
                      </button>
                      <div className="min-w-0 flex-1 space-y-2 text-xs">
                        <ul className="space-y-1" aria-label="Hypothesis evidence scores">
                          {Object.entries(r.scores as Record<string, number>)
                            .sort((a, b) => b[1] - a[1])
                            .map(([k, v]) => (
                              <li key={k} className="grid grid-cols-[110px_1fr_36px] items-center gap-2">
                                <span className={k === r.hypothesis ? "font-semibold" : "text-muted"}>{humanize(k)}</span>
                                <span className="h-1.5 rounded-full bg-surface-3">
                                  <span className="block h-full rounded-full" style={{ width: `${v * 100}%`, background: HYP_COLOR[k] ?? "var(--series-other)" }} />
                                </span>
                                <span className="tabular text-right">{fixed(v, 2)}</span>
                              </li>
                            ))}
                        </ul>
                        {r.reasons.length > 0 && (
                          <ul className="list-disc space-y-0.5 pl-4 text-muted">
                            {r.reasons.map((x: string) => (
                              <li key={x}>{x}</li>
                            ))}
                          </ul>
                        )}
                        {r.valuable_reasons.length > 0 && (
                          <div className="rounded-md bg-accent-soft p-2 text-accent">
                            <div className="mb-0.5 font-semibold">Why it may be valuable</div>
                            <ul className="list-disc space-y-0.5 pl-4">
                              {r.valuable_reasons.map((x: string) => (
                                <li key={x}>{x}</li>
                              ))}
                            </ul>
                          </div>
                        )}
                      </div>
                    </div>
                  </Card>
                </li>
              ))}
            </ul>
          )}
          <Pager offset={offset} limit={24} total={data.total} onChange={setOffset} />
        </div>
      )}
      <SampleModal sampleId={open} versionId={versionId} onClose={() => setOpen(null)} />
    </RequireAudit>
  );
}
