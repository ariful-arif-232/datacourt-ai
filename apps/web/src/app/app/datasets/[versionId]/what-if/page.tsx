"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { FlaskConical, Plus, Trash2 } from "lucide-react";
import { Suspense, useState } from "react";
import { RequireAudit } from "@/components/audit-progress";
import { ConfusionMatrix } from "@/components/charts";
import { WhatIfSummary } from "@/components/whatif-result";
import { WorkerStatus } from "@/components/worker-status";
import { Badge, Button, Card, cx, EmptyState, Field, inputCls, Loading, Notice, PageHeader, Select, StatusBadge } from "@/components/ui";
import { api, useApi } from "@/lib/api";
import { useDataset } from "@/lib/dataset";
import { ago, humanize } from "@/lib/format";
import { useVersionApi } from "@/lib/hooks";
import { useSession } from "@/lib/session";

type Action = { type: string; [k: string]: any };

const PRESETS: { name: string; actions: Action[] }[] = [
  { name: "Apply my review decisions", actions: [{ type: "apply_review_decisions" }, { type: "preserve_rare" }] },
  { name: "Remove duplicate copies + fix leakage", actions: [{ type: "remove_duplicate_copies" }, { type: "move_leakage_out_of_eval", min_risk: "medium" }] },
  { name: "Exclude high-severity quality problems", actions: [{ type: "exclude_quality", min_severity: "high" }, { type: "preserve_rare" }] },
  { name: "Accept jury suggestions (unreviewed)", actions: [{ type: "apply_jury_suggestions" }, { type: "preserve_rare" }] },
  { name: "Rebalance: cap each class", actions: [{ type: "rebalance", max_per_class: 200 }] },
];

function WhatIfInner() {
  const { versionId } = useDataset();
  const { canWrite } = useSession();
  const params = useSearchParams();
  const router = useRouter();
  const { data: meta } = useApi<any>("/what-if/action-types");
  const { data: runs, mutate } = useVersionApi<any[]>("/what-if", { refreshInterval: 4000 });
  // Without an explicit ?run=, open the most recent completed experiment.
  const selected = params.get("run") ?? runs?.find((r) => r.status === "completed")?.id ?? null;
  const { data: run } = useApi<any>(selected ? `/what-if/${selected}` : null, { refreshInterval: (r) => (r && ["queued", "running"].includes(r.status) ? 2000 : 0) });
  const [name, setName] = useState("My experiment");
  const [actions, setActions] = useState<Action[]>(PRESETS[0].actions);
  const [seeds, setSeeds] = useState(3);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const launch = async () => {
    setBusy(true);
    setErr(null);
    try {
      const r = await api("/what-if", { json: { dataset_version_id: versionId, name, actions, seeds } });
      mutate();
      router.replace(`/app/datasets/${versionId}/what-if?run=${r.id}`);
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <PageHeader
        title="What-if lab"
        description="Test a proposed change set before touching any exported data: DataCourt retrains the baseline with and without the change and compares them on the same evaluation sets — including a preserved holdout that no change touches."
      />
      <div className="grid gap-5 xl:grid-cols-[420px_1fr]">
        <div className="space-y-5">
          {canWrite ? (
            <Card title="New experiment">
              <div className="space-y-4">
                <div className="flex flex-wrap gap-1.5">
                  {PRESETS.map((p) => (
                    <button key={p.name} onClick={() => { setActions(p.actions); setName(p.name); }} className="rounded-full border border-border px-2.5 py-1 text-xs hover:bg-surface-2">
                      {p.name}
                    </button>
                  ))}
                </div>
                <Field label="Name">
                  <input value={name} onChange={(e) => setName(e.target.value)} className={inputCls} />
                </Field>
                <div className="space-y-2">
                  <div className="text-[13px] font-medium">Change set</div>
                  {actions.map((a, i) => (
                    <div key={i} className="rounded-lg border hairline p-2.5">
                      <div className="flex items-center gap-2">
                        <Select
                          label="Action type"
                          className="flex-1"
                          value={a.type}
                          onChange={(v) => setActions(actions.map((x, j) => (j === i ? { type: v } : x)))}
                          options={(meta?.types ?? []).map((t: string) => ({ value: t, label: humanize(t) }))}
                        />
                        <Button size="sm" variant="ghost" aria-label="Remove action" onClick={() => setActions(actions.filter((_, j) => j !== i))}>
                          <Trash2 className="size-4" aria-hidden />
                        </Button>
                      </div>
                      <p className="mt-1 text-xs text-muted">{meta?.descriptions?.[a.type]}</p>
                      <ActionParams a={a} onChange={(p) => setActions(actions.map((x, j) => (j === i ? { ...x, ...p } : x)))} />
                    </div>
                  ))}
                  <Button size="sm" onClick={() => setActions([...actions, { type: "preserve_rare" }])}>
                    <Plus className="size-4" aria-hidden /> Add action
                  </Button>
                </div>
                <Field label="Training-bootstrap replicates" hint="Paired Poisson resamples of the training data, used to estimate noise. More replicates = slower, tighter.">
                  <input type="number" min={1} max={10} value={seeds} onChange={(e) => setSeeds(Number(e.target.value))} className={inputCls} />
                </Field>
                <Button variant="primary" className="w-full" onClick={launch} loading={busy} disabled={!actions.length}>
                  <FlaskConical className="size-4" aria-hidden /> Run isolated experiment
                </Button>
                {err && <Notice tone="bad">{err}</Notice>}
              </div>
            </Card>
          ) : (
            <Notice>Read-only access: experiments require the reviewer role. Existing results are shown on the right.</Notice>
          )}
          <Card title="Experiments">
            {!runs ? (
              <Loading rows={1} />
            ) : runs.length === 0 ? (
              <p className="text-sm text-muted">No experiments yet.</p>
            ) : (
              <ul className="space-y-1">
                {runs.map((r) => (
                  <li key={r.id}>
                    <button onClick={() => router.replace(`/app/datasets/${versionId}/what-if?run=${r.id}`)} className={cx("flex w-full items-center justify-between gap-2 rounded-md px-2 py-1.5 text-left text-sm", selected === r.id ? "bg-accent-soft" : "hover:bg-surface-2")}>
                      <span className="min-w-0 truncate">{r.name}</span>
                      <span className="flex shrink-0 items-center gap-2">
                        {r.source !== "lab" && <Badge>{humanize(r.source)}</Badge>}
                        <StatusBadge status={r.status} />
                        <span className="text-xs text-subtle">{ago(r.created_at)}</span>
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>
        <div className="min-w-0">
          {!run ? (
            <EmptyState title="Select or launch an experiment">Results are labelled “Experimental result on this evaluation setup” — never a guaranteed production gain.</EmptyState>
          ) : (
            <RunDetail run={run} />
          )}
        </div>
      </div>
    </>
  );
}

function ActionParams({ a, onChange }: { a: Action; onChange: (p: Partial<Action>) => void }) {
  if (a.type === "exclude_quality")
    return (
      <div className="mt-2">
        <Select label="Minimum severity" value={a.min_severity ?? "high"} onChange={(v) => onChange({ min_severity: v })} options={["high", "medium", "low"].map((s) => ({ value: s, label: `severity ≥ ${s}` }))} />
      </div>
    );
  if (a.type === "move_leakage_out_of_eval")
    return (
      <div className="mt-2">
        <Select label="Minimum risk" value={a.min_risk ?? "medium"} onChange={(v) => onChange({ min_risk: v })} options={["critical", "high", "medium", "low"].map((s) => ({ value: s, label: `risk ≥ ${s}` }))} />
      </div>
    );
  if (a.type === "rebalance")
    return (
      <div className="mt-2 flex items-center gap-2 text-xs">
        <span>max per class</span>
        <input type="number" min={1} value={a.max_per_class ?? 200} onChange={(e) => onChange({ max_per_class: Number(e.target.value) })} className={`${inputCls} h-8 w-24 py-1`} aria-label="Maximum samples per class" />
      </div>
    );
  if (a.type === "remove_samples" || a.type === "relabel_samples")
    return <p className="mt-2 text-xs text-subtle">Sample-level actions are created from cases (“Verify in What-if”) or from Failure Replay.</p>;
  return null;
}

function RunDetail({ run }: { run: any }) {
  const [set, setSet] = useState("preserved_holdout");
  const after = run.results?.find((r: any) => r.variant === "after" && r.eval_set === set);
  const before = run.results?.find((r: any) => r.variant === "before" && r.eval_set === set);
  return (
    <div className="space-y-5">
      <Card title={run.name} subtitle={`${humanize(run.source)} · ${ago(run.created_at)}`}>
        <div className="mb-4 flex flex-wrap gap-1.5">
          {(run.actions ?? []).map((a: any, i: number) => (
            <Badge key={i}>
              {humanize(a.type)}
              {a.affected > 0 ? ` · ${a.affected}` : ""}
            </Badge>
          ))}
        </div>
        <WhatIfSummary run={run} />
        <WorkerStatus job={run.execution} />
      </Card>
      {run.status === "completed" && run.summary?.changes && (
        <Card title="What changed in training">
          <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
            {[
              ["Removed from training", run.summary.changes.removed_from_training],
              ["Relabelled", run.summary.changes.relabelled],
              ["Dropped from evaluation", run.summary.changes.dropped_from_evaluation],
              ["Protected rare samples", run.summary.changes.protected_rare],
            ].map(([k, v]) => (
              <div key={k as string} className="rounded-lg bg-surface-2 p-2 text-center">
                <div className="text-lg font-semibold tabular">{v as number}</div>
                <div className="text-xs text-muted">{k}</div>
              </div>
            ))}
          </div>
        </Card>
      )}
      {before && after && (
        <Card
          title="Per-class comparison"
          actions={
            <Select label="Evaluation set" value={set} onChange={setSet} options={["preserved_holdout", "original_eval", "cleaned_eval"].map((s) => ({ value: s, label: humanize(s) }))} />
          }
        >
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs text-subtle">
                <tr>
                  <th className="pb-1.5 font-medium">Class</th>
                  <th className="pb-1.5 font-medium">Precision</th>
                  <th className="pb-1.5 font-medium">Recall</th>
                  <th className="pb-1.5 font-medium">F1 before → after</th>
                  <th className="pb-1.5 font-medium">Support</th>
                </tr>
              </thead>
              <tbody>
                {Object.keys(after.metrics.per_class).map((k) => {
                  const b = before.metrics.per_class[k];
                  const a = after.metrics.per_class[k];
                  const d = a.f1 - b.f1;
                  return (
                    <tr key={k} className="border-t hairline">
                      <td className="py-1.5">{k}</td>
                      <td className="py-1.5 tabular">
                        {b.precision.toFixed(2)} → {a.precision.toFixed(2)}
                      </td>
                      <td className="py-1.5 tabular">
                        {b.recall.toFixed(2)} → {a.recall.toFixed(2)}
                      </td>
                      <td className="py-1.5 tabular">
                        {b.f1.toFixed(2)} → {a.f1.toFixed(2)} <span className={d > 0.005 ? "text-ok" : d < -0.005 ? "text-bad" : "text-muted"}>({d > 0 ? "+" : ""}{d.toFixed(2)})</span>
                      </td>
                      <td className="py-1.5 tabular">{a.support}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div className="mt-5 grid gap-5 lg:grid-cols-2">
            <div>
              <div className="mb-1 text-xs font-medium text-muted">Confusion — before</div>
              <ConfusionMatrix matrix={before.metrics.confusion_matrix} labels={before.metrics.class_names} />
            </div>
            <div>
              <div className="mb-1 text-xs font-medium text-muted">Confusion — after</div>
              <ConfusionMatrix matrix={after.metrics.confusion_matrix} labels={after.metrics.class_names} />
            </div>
          </div>
          <p className="mt-3 text-xs text-subtle">Calibration (ECE) before {before.metrics.ece} → after {after.metrics.ece}. Point estimates from converged fits on the full before/after training sets.</p>
        </Card>
      )}
    </div>
  );
}

export default function WhatIfPage() {
  return (
    <RequireAudit>
      <Suspense>
        <WhatIfInner />
      </Suspense>
    </RequireAudit>
  );
}
