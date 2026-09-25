"use client";

import Link from "next/link";
import { useState } from "react";
import { RequireAudit } from "@/components/audit-progress";
import { Badge, Button, Card, EmptyState, Loading, PageHeader, Pager, SampleThumb, Select, Stat } from "@/components/ui";
import { qs } from "@/lib/api";
import { useDataset } from "@/lib/dataset";
import { fixed, humanize, num } from "@/lib/format";
import { useVersionApi, useFilteredOffset } from "@/lib/hooks";

export default function DuplicatesPage() {
  const { versionId } = useDataset();
  const [kind, setKind] = useState("");
  const [cross, setCross] = useState("");
  const [conflict, setConflict] = useState("");
  const [offset, setOffset] = useFilteredOffset(kind, cross, conflict);
  const { data } = useVersionApi<any>(`/duplicates${qs({ kind, cross_split: cross, label_conflict: conflict, offset, limit: 20 })}`);
  const sm = data?.summary;
  return (
    <RequireAudit>
      <PageHeader
        title="Duplicate & near-duplicate forensics"
        description="Candidates come from SHA-256, perceptual hashes and embeddings; every non-identical match must also pass structural verification (fine-detail correlation or keypoint geometry). Matches are grouped into families with a source-like root."
      />
      {!data ? (
        <Loading />
      ) : (
        <div className="space-y-5">
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <Stat label="Families" value={num(sm.families)} sub={`${num(sm.exact_families)} byte-identical`} />
            <Stat label="Redundant samples" value={num(sm.redundant_samples)} sub="family size − 1, summed" />
            <Stat label="Label conflicts" value={num(sm.label_conflict_families)} sub="families with mixed labels" tone={sm.label_conflict_families ? "warn" : undefined} />
            <Stat label="Relations" value={num(sm.edges)} sub={Object.entries(sm.relations ?? {}).map(([k, v]) => `${humanize(k)} ${v}`).join(" · ")} />
          </div>
          <Card
            title="Families"
            subtitle="Cross-split families first. Open a family to see its lineage graph."
            actions={
              <>
                <Select label="Kind" value={kind} onChange={setKind} options={[{ value: "", label: "All kinds" }, { value: "exact", label: "Exact" }, { value: "transformed", label: "Transformed" }, { value: "near", label: "Near" }]} />
                <Select label="Split" value={cross} onChange={setCross} options={[{ value: "", label: "Any split" }, { value: "true", label: "Crosses splits" }, { value: "false", label: "Single split" }]} />
                <Select label="Labels" value={conflict} onChange={setConflict} options={[{ value: "", label: "Any labels" }, { value: "true", label: "Label conflict" }, { value: "false", label: "Consistent labels" }]} />
              </>
            }
          >
            {data.families.length === 0 ? (
              <EmptyState title="No duplicate families for this filter" />
            ) : (
              <ul className="divide-y divide-border">
                {data.families.map((f: any) => (
                  <li key={f.id} className="py-4 first:pt-0">
                    <div className="mb-2 flex flex-wrap items-center gap-2 text-sm">
                      <span className="font-semibold">Family #{f.number}</span>
                      <Badge>{f.size} members</Badge>
                      <Badge tone={f.kind === "exact" ? "info" : "neutral"}>{f.kind}</Badge>
                      {f.crosses_splits && <Badge tone="bad">crosses {f.splits.join(" · ")}</Badge>}
                      {f.label_conflict && <Badge tone="warn">labels: {f.labels.join(" / ")}</Badge>}
                      <span className="text-xs text-subtle tabular">max cos {fixed(f.max_similarity, 3)}</span>
                      <div className="flex-1" />
                      <Button size="sm" href={`/app/datasets/${versionId}/lineage?family=${f.id}`}>
                        Lineage graph
                      </Button>
                    </div>
                    <ul className="flex gap-3 overflow-x-auto pb-1">
                      {f.members.map((m: any) => (
                        <li key={m.sample.id} className="shrink-0">
                          <SampleThumb s={m.sample} size={96} />
                          <div className="mt-0.5 text-[10.5px] text-muted">{m.is_root ? "source-like" : `${humanize(m.relation)} · ${fixed(m.similarity, 3)}`}</div>
                        </li>
                      ))}
                    </ul>
                  </li>
                ))}
              </ul>
            )}
            <Pager offset={offset} limit={20} total={data.total} onChange={setOffset} />
          </Card>
          <p className="text-xs text-subtle">
            Thresholds: {Object.entries(sm.thresholds ?? {}).map(([k, v]) => `${k}=${v}`).join(", ")}.{" "}
            <Link href="/docs/methodology#duplicates" className="underline">
              How duplicates are verified
            </Link>
          </p>
        </div>
      )}
    </RequireAudit>
  );
}
