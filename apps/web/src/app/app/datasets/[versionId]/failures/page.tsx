"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { ArrowDown, Play } from "lucide-react";
import { Suspense, useState } from "react";
import { RequireAudit } from "@/components/audit-progress";
import { WhatIfSummary } from "@/components/whatif-result";
import { Badge, Button, Card, cx, EmptyState, KindTag, Loading, Notice, PageHeader, SampleThumb, SplitTag, VerdictBadge } from "@/components/ui";
import { api, useApi } from "@/lib/api";
import { useDataset } from "@/lib/dataset";
import { fixed, pct } from "@/lib/format";
import { caseHref, useVersionApi } from "@/lib/hooks";
import { useSession } from "@/lib/session";

function FailuresInner() {
  const { versionId } = useDataset();
  const params = useSearchParams();
  const router = useRouter();
  const { data } = useVersionApi<any>("/failures");
  const selected = params.get("failure") ?? data?.items?.[0]?.id ?? null;
  return (
    <>
      <PageHeader
        title="Model-to-Data Blame Map & Failure Replay"
        description="Pick a misclassified evaluation sample. DataCourt traces likely contributing training evidence — nearest examples, influence estimates, suspicious labels, duplicates, shortcuts — then lets you replay the failure with a proposed data change."
      />
      {!data ? (
        <Loading />
      ) : data.items.length === 0 ? (
        <EmptyState title="No failures to trace">The baseline model classified every evaluation sample correctly.</EmptyState>
      ) : (
        <div className="grid gap-5 xl:grid-cols-[260px_1fr]">
          <Card title={`Failures (${data.items.length})`} subtitle={data.eval_mode === "holdout" ? "Misclassified val/test samples, most confident first" : "Out-of-fold misclassifications (no eval split)"} className="xl:max-h-[80vh] xl:overflow-y-auto">
            <ul className="space-y-1">
              {data.items.map((f: any) => (
                <li key={f.id}>
                  <button
                    onClick={() => router.replace(`/app/datasets/${versionId}/failures?failure=${f.id}`)}
                    aria-current={selected === f.id}
                    className={cx("flex w-full items-center gap-2 rounded-md p-1.5 text-left", selected === f.id ? "bg-accent-soft" : "hover:bg-surface-2")}
                  >
                    <SampleThumb s={f.sample} size={40} caption={false} />
                    <span className="min-w-0 text-xs">
                      <span className="block truncate font-medium">
                        {f.actual} → {f.predicted}
                      </span>
                      <span className="text-muted tabular">
                        {f.split} · p={fixed(f.confidence, 2)}
                      </span>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </Card>
          {selected && <BlameMap key={selected} failureId={selected} />}
        </div>
      )}
    </>
  );
}

function BlameMap({ failureId }: { failureId: string }) {
  const { versionId } = useDataset();
  const { canWrite } = useSession();
  const { data } = useApi<any>(`/failures/${failureId}/blame-map`);
  const [pickedChoice, setPicked] = useState<Set<string> | null>(null);
  const [dropLeak, setDropLeak] = useState(true);
  const [runId, setRunId] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const { data: runs, mutate: refetchRuns } = useVersionApi<any[]>("/what-if");
  const { data: run } = useApi<any>(runId ? `/what-if/${runId}` : null, { refreshInterval: (r) => (r && ["queued", "running"].includes(r.status) ? 2000 : 0) });
  // Pre-select the proposed removals until the user edits the selection. Per-failure state resets
  // because the component is keyed by failureId.
  const picked: Set<string> =
    pickedChoice ?? new Set<string>((data?.proposed_actions ?? []).filter((a: any) => a.sample_id).map((a: any) => a.sample_id));
  if (!data) return <Loading />;
  const f = data.failure;
  const previous = (runs ?? []).filter((r) => r.failure_event_id === failureId);
  const contributors: any[] = [...(data.links.harmful_influence ?? []), ...(data.links.nearest_neighbor ?? [])].filter(
    (x, i, arr) => arr.findIndex((y) => y.sample.id === x.sample.id) === i,
  );

  const replay = async () => {
    setErr(null);
    const actions: any[] = [];
    if (picked.size) actions.push({ type: "remove_samples", sample_ids: Array.from(picked) });
    if (dropLeak) actions.push({ type: "move_leakage_out_of_eval" });
    actions.push({ type: "preserve_rare" });
    try {
      const r = await api("/what-if", {
        json: { dataset_version_id: versionId, name: `Replay: ${f.actual} → ${f.predicted}`, actions, seeds: 3, failure_event_id: failureId, source: "failure_replay" },
      });
      setRunId(r.id);
      refetchRuns();
    } catch (e: any) {
      setErr(e.message);
    }
  };

  return (
    <div className="min-w-0 space-y-5">
      <Card title="1 · What failed?">
        <div className="flex flex-wrap gap-5">
          <SampleThumb s={f.sample} size={148} />
          <div className="space-y-2 text-sm">
            <div>
              Labelled <b>{f.actual}</b>, predicted <b className="text-bad">{f.predicted}</b> at {pct(f.confidence)} <KindTag kind="model_prediction" />
            </div>
            <ul className="text-xs text-muted">
              {f.top_probs.map(([c, p]: [string, number]) => (
                <li key={c} className="tabular">
                  {c}: {pct(p, 1)}
                </li>
              ))}
            </ul>
            {f.case && (
              <Link href={caseHref(versionId, f.case.id)} className="inline-flex items-center gap-1 text-xs text-accent hover:underline">
                This sample is case #{f.case.number} <VerdictBadge verdict={f.case.verdict} />
              </Link>
            )}
            {f.leakage.length > 0 && <Notice tone="bad">This evaluation sample itself is in {f.leakage.length} leakage finding(s) ({f.leakage.map((l: any) => l.risk).join(", ")}).</Notice>}
          </div>
        </div>
      </Card>

      <Card title="2 · Evidence chain (likely contributing training evidence)">
        <ol className="space-y-1">
          {data.chain.map((c: any, i: number) => (
            <li key={c.step}>
              <div className="flex items-center gap-3 rounded-lg border hairline bg-surface-2/60 px-3 py-2 text-sm">
                <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-accent text-[11px] font-semibold text-accent-ink">{i + 1}</span>
                <span className="flex-1">{c.title}</span>
                <KindTag kind={c.kind} />
              </div>
              {i < data.chain.length - 1 && <ArrowDown className="mx-auto my-0.5 size-3.5 text-subtle" aria-hidden />}
            </li>
          ))}
        </ol>
        <p className="mt-3 text-xs text-subtle">{data.disclaimer}</p>
      </Card>

      <Card title="Contributing training samples" subtitle="Nearest neighbours (measured) and largest harmful TracIn estimates (model estimate). Select samples to remove in the replay.">
        <ul className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {contributors.map((c) => (
            <li key={c.sample.id} className={cx("flex gap-2 rounded-lg border p-2", picked.has(c.sample.id) ? "border-accent bg-accent-soft" : "hairline")}>
              <SampleThumb s={c.sample} size={72} caption={false} />
              <div className="min-w-0 text-xs">
                <div className="font-medium">{c.label}</div>
                <div className="text-muted">
                  {c.link_type === "harmful_influence" ? `TracIn ${fixed(c.score, 4)}` : `cos ${fixed(c.score, 3)}`}
                </div>
                <div className="mt-0.5 flex flex-wrap gap-1">
                  {c.matches_failure_prediction && <Badge tone="warn">has predicted label</Badge>}
                  {c.label_action && c.label_action !== "NO_ACTION" && <Badge tone="bad">label suspicious</Badge>}
                  {c.shortcut_cues.length > 0 && <Badge>shortcut cue</Badge>}
                  {c.duplicate_family && <Badge>duplicate</Badge>}
                </div>
                {canWrite && (
                  <label className="mt-1 flex items-center gap-1">
                    <input
                      type="checkbox"
                      checked={picked.has(c.sample.id)}
                      onChange={(e) => {
                        const n = new Set(picked);
                        if (e.target.checked) n.add(c.sample.id);
                        else n.delete(c.sample.id);
                        setPicked(n);
                      }}
                    />
                    remove in replay
                  </label>
                )}
                {c.case && (
                  <Link href={caseHref(versionId, c.case.id)} className="text-accent hover:underline">
                    case #{c.case.number}
                  </Link>
                )}
              </div>
            </li>
          ))}
        </ul>
      </Card>

      <Card title="3–5 · Replay: propose, test, compare">
        <div className="space-y-3 text-sm">
          <p className="text-muted">
            Runs an isolated experiment: the baseline is retrained without the selected samples{dropLeak ? ", leaked evaluation copies are dropped" : ""}, rare-valid samples are protected, and before/after are compared on the same evaluation sets. Nothing in the dataset is changed.
          </p>
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={dropLeak} onChange={(e) => setDropLeak(e.target.checked)} /> Also drop leaked evaluation copies
          </label>
          {canWrite ? (
            <Button variant="primary" onClick={replay} disabled={!picked.size && !dropLeak}>
              <Play className="size-4" aria-hidden /> Replay with {picked.size} removal(s)
            </Button>
          ) : (
            <Notice>Read-only workspace: replays require reviewer access.</Notice>
          )}
          {err && <Notice tone="bad">{err}</Notice>}
          {run && <WhatIfSummary run={run} />}
          {!run && previous.length > 0 && (
            <div>
              <div className="mb-1 text-xs text-subtle">Previous replays of this failure</div>
              <ul className="space-y-1">
                {previous.map((r) => (
                  <li key={r.id}>
                    <button className="text-xs text-accent hover:underline" onClick={() => setRunId(r.id)}>
                      {r.name} · {r.status}
                      {r.summary?.failure ? (r.summary.failure.fixed ? " · fixed" : " · not fixed") : ""}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </Card>
      <p className="flex items-center gap-2 text-xs text-subtle">
        Failure sample split: <SplitTag split={f.split} />
      </p>
    </div>
  );
}

export default function FailuresPage() {
  return (
    <RequireAudit>
      <Suspense>
        <FailuresInner />
      </Suspense>
    </RequireAudit>
  );
}
