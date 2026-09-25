"use client";

import { useMemo, useState } from "react";
import { RequireAudit } from "@/components/audit-progress";
import { colorFor, Legend, LineChart, type Point, ScatterCanvas } from "@/components/charts";
import { SampleModal } from "@/components/sample-detail";
import { Card, EmptyState, KindTag, Loading, PageHeader, SampleThumb, Select } from "@/components/ui";
import { qs } from "@/lib/api";
import { useDataset } from "@/lib/dataset";
import { fixed, humanize, num } from "@/lib/format";
import { useVersionApi } from "@/lib/hooks";

const CATS = ["easy", "ambiguous", "unstable", "forgotten", "consistently_hard"];

export default function CartographyPage() {
  const { versionId } = useDataset();
  const [category, setCategory] = useState("consistently_hard");
  const [open, setOpen] = useState<string | null>(null);
  const { data } = useVersionApi<any>(`/cartography${qs({ category })}`);
  const points: Point[] = useMemo(
    () =>
      (data?.points ?? []).map((p: any) => ({
        id: p.id,
        x: p.variability,
        y: p.confidence,
        group: p.category,
        size: 2.5,
        meta: (
          <span>
            <b>{p.label}</b> · {humanize(p.category)}
            <br />
            confidence {fixed(p.confidence, 2)} · variability {fixed(p.variability, 2)}
            <br />
            correct {fixed(p.correctness * 100, 0)}% of epochs · forgotten {p.forgetting}×
          </span>
        ),
      })),
    [data],
  );
  const counts: Record<string, number> = data?.summary?.categories ?? {};
  return (
    <RequireAudit>
      <PageHeader
        title="Training dynamics (dataset cartography)"
        description="How each training sample behaves while the baseline head learns: easy, ambiguous, consistently hard, forgotten or unstable. Hard-to-learn samples are over-represented among label errors — but also include rare valid cases."
      />
      {!data ? (
        <Loading />
      ) : !data.available ? (
        <EmptyState title="Training dynamics not computed">{data.reason}. Run a DEEP audit from the overview page.</EmptyState>
      ) : (
        <div className="space-y-5">
          <Card
            title={
              <span className="flex items-center gap-2">
                Data map <KindTag kind="model_estimate" />
              </span>
            }
            subtitle={`x: variability of p(given label) across ${data.summary.epochs} epochs · y: mean confidence. Click a point to inspect it.`}
          >
            <div className="mb-3">
              <Legend items={CATS.map((c) => ({ key: c, label: humanize(c), color: colorFor(c, CATS), count: counts[c] ?? 0 }))} />
            </div>
            <ScatterCanvas points={points} groups={CATS} onPick={(p) => setOpen(p.id)} xLabel="variability →" yLabel="↑ confidence" domain={{ x: [0, Math.max(0.5, ...points.map((p) => p.x))], y: [0, 1] }} />
          </Card>
          <Card
            title="Examples"
            actions={<Select label="Category" value={category} onChange={setCategory} options={CATS.map((c) => ({ value: c, label: `${humanize(c)} (${num(counts[c] ?? 0)})` }))} />}
          >
            {data.examples.length === 0 ? (
              <EmptyState title="No samples in this category" />
            ) : (
              <ul className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
                {data.examples.map((e: any) => (
                  <li key={e.sample.id} className="flex gap-3">
                    <button onClick={() => setOpen(e.sample.id)} aria-label={`Open ${e.sample.name}`}>
                      <SampleThumb s={e.sample} size={88} />
                    </button>
                    <div className="min-w-0 flex-1">
                      <div className="text-xs text-muted">p(given label) per epoch</div>
                      <LineChart height={70} series={[{ key: "p", label: "p(given label)", values: e.trajectory ?? [] }]} xLabels={(e.trajectory ?? []).map((_: any, i: number) => `epoch ${i + 1}`)} />
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </Card>
          <p className="text-xs text-subtle">
            Thresholds: {Object.entries(data.thresholds).map(([k, v]) => `${k}=${v}`).join(", ")}. Trajectories come from the seeded SGD run of the linear baseline head (dynamics are in-sample by design).
          </p>
        </div>
      )}
      <SampleModal sampleId={open} versionId={versionId} onClose={() => setOpen(null)} />
    </RequireAudit>
  );
}
