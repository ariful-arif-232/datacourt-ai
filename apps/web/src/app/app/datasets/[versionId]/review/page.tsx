"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { Play, Timer } from "lucide-react";
import { type FormEvent, useState } from "react";
import { RequireAudit } from "@/components/audit-progress";
import { Badge, Button, Card, cx, EmptyState, Field, inputCls, Loading, Notice, PageHeader, Progress, SampleThumb, Select, Stat, VerdictBadge } from "@/components/ui";
import { api, useApi } from "@/lib/api";
import { useDataset } from "@/lib/dataset";
import { ago, fixed, humanize, num, pct } from "@/lib/format";
import { useVersionApi } from "@/lib/hooks";
import { useSession } from "@/lib/session";

const OBJECTIVES = [
  ["balanced", "Balanced (risk × impact × diversity)"],
  ["label_errors", "Prioritise likely label errors"],
  ["leakage", "Prioritise evaluation leakage"],
  ["model_impact", "Prioritise expected model impact"],
  ["quality", "Prioritise quality problems"],
];

export default function ReviewPage() {
  const { version, versionId } = useDataset();
  const { canWrite } = useSession();
  const [mode, setMode] = useState<"items" | "minutes">("minutes");
  const [amount, setAmount] = useState(45);
  const [objective, setObjective] = useState("balanced");
  const [result, setResult] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const { data: sessions, mutate } = useVersionApi<any[]>("/review-sessions");
  const auditId = version?.audits.find((a) => a.status === "completed")?.id;
  const { data: agree } = useApi<any>(auditId ? `/audits/${auditId}/agreement` : null);

  const run = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      const r = await api("/review-budget", {
        json: { dataset_version_id: versionId, objective, create_session: canWrite, ...(mode === "items" ? { max_items: amount } : { max_minutes: amount }) },
      });
      setResult(r);
      mutate();
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <RequireAudit>
      <PageHeader
        title="Review budget optimizer"
        description="You rarely need to review every finding. Tell DataCourt how much time you have and what matters most; it selects the cases with the highest expected value, with diminishing returns so one duplicate family or class cannot fill the queue."
      />
      <div className="grid gap-5 xl:grid-cols-[380px_1fr]">
        <Card title="Budget">
          <form onSubmit={run} className="space-y-4">
            <div className="flex gap-2" role="radiogroup" aria-label="Budget type">
              {(["minutes", "items"] as const).map((m) => (
                <label key={m} className={cx("flex-1 cursor-pointer rounded-lg border px-3 py-2 text-center text-sm", mode === m ? "border-accent bg-accent-soft" : "border-border")}>
                  <input type="radio" className="sr-only" checked={mode === m} onChange={() => { setMode(m); setAmount(m === "minutes" ? 45 : 25); }} />
                  {m === "minutes" ? "I have N minutes" : "I can review N samples"}
                </label>
              ))}
            </div>
            <Field label={mode === "minutes" ? "Minutes available" : "Number of samples"}>
              <input type="number" min={1} max={mode === "minutes" ? 6000 : 2000} value={amount} onChange={(e) => setAmount(Number(e.target.value))} className={inputCls} />
            </Field>
            <Field label="Objective">
              <Select label="Objective" className="h-9 w-full" value={objective} onChange={setObjective} options={OBJECTIVES.map(([v, l]) => ({ value: v, label: l }))} />
            </Field>
            <Button type="submit" variant="primary" className="w-full" loading={busy}>
              <Timer className="size-4" aria-hidden /> Build review queue
            </Button>
            {!canWrite && <p className="text-xs text-muted">Read-only: the queue is computed but not saved as a session.</p>}
            {err && <Notice tone="bad">{err}</Notice>}
          </form>
        </Card>
        <div className="min-w-0 space-y-5">
          {result ? (
            <>
              <Notice tone="info" title={`${num(result.total_open_findings)} open cases — reviewing all of them is unnecessary.`}>
                With this budget, start with these {result.selected_count} cases (≈{fixed(result.estimated_minutes, 0)} minutes).
              </Notice>
              <div className="grid gap-3 sm:grid-cols-3">
                <Stat label="Expected issue coverage" info="budget_coverage" value={pct(result.expected_issue_coverage)} sub="share of total case value" />
                <Stat label="High-impact coverage" value={pct(result.high_impact_coverage)} sub="relabel / strong review / leakage / remove" />
                <Stat label="Diversity" value={`${result.distinct_classes} classes`} sub={`${result.distinct_families} duplicate families`} />
              </div>
              <Card
                title="Selected queue"
                actions={
                  result.session_id && (
                    <Button size="sm" variant="primary" href={`/app/datasets/${versionId}/court/${result.selected[0].id}?session=${result.session_id}`}>
                      <Play className="size-4" aria-hidden /> Start keyboard review
                    </Button>
                  )
                }
              >
                <ol className="divide-y divide-border">
                  {result.selected.map((c: any) => (
                    <li key={c.id} className="flex items-start gap-3 py-2 text-sm">
                      <span className="w-6 shrink-0 text-right font-mono text-xs text-subtle">{c.rank}</span>
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <Link href={`/app/datasets/${versionId}/court/${c.id}${result.session_id ? `?session=${result.session_id}` : ""}`} className="font-medium hover:underline">
                            Case #{c.case_number}
                          </Link>
                          <VerdictBadge verdict={c.verdict} />
                          <span className="text-xs text-muted">
                            {c.label} · {c.split}
                          </span>
                        </div>
                        <p className="text-xs text-muted">{c.reason}</p>
                      </div>
                      <span className="shrink-0 text-xs tabular text-subtle">~{fixed(c.est_minutes, 1)} min</span>
                    </li>
                  ))}
                </ol>
                <p className="mt-3 text-xs text-subtle">{result.disclaimer}</p>
              </Card>
              <Card title="Coverage by category">
                <ul className="grid gap-2 sm:grid-cols-2">
                  {Object.entries(result.coverage_by_category).map(([k, v]: [string, any]) => (
                    <li key={k} className="text-sm">
                      <div className="flex justify-between">
                        <span>{humanize(k)}</span>
                        <span className="tabular text-muted">
                          {v.selected} / {v.open}
                        </span>
                      </div>
                      <Progress value={v.open ? v.selected / v.open : 0} label={`${k} coverage`} />
                    </li>
                  ))}
                </ul>
              </Card>
            </>
          ) : (
            <EmptyState title="Set a budget to build a prioritised queue">
              Example: “I have 45 minutes — prioritise expected model impact.” Each case carries an effort estimate (relabels and duplicate families take longer).
            </EmptyState>
          )}
        </div>
      </div>

      <div className="mt-6 grid gap-5 lg:grid-cols-2">
        <Card title="Review sessions">
          {!sessions ? (
            <Loading rows={1} />
          ) : sessions.length === 0 ? (
            <p className="text-sm text-muted">No sessions yet.</p>
          ) : (
            <ul className="space-y-3">
              {sessions.map((s) => (
                <li key={s.id}>
                  <div className="flex items-center justify-between gap-2 text-sm">
                    <span className="font-medium">{s.name}</span>
                    <span className="text-xs text-subtle">{ago(s.created_at)}</span>
                  </div>
                  <div className="mt-1 flex items-center gap-3">
                    <Progress value={s.items ? s.reviewed / s.items : 0} label={`${s.name} progress`} />
                    <span className="shrink-0 text-xs tabular text-muted">
                      {s.reviewed}/{s.items}
                    </span>
                    <SessionResume sessionId={s.id} />
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Card>
        <Card title="Reviewer consensus" subtitle="Disagreements become disputed cases that an owner or admin adjudicates.">
          {!agree ? (
            <Loading rows={1} />
          ) : (
            <div className="space-y-3 text-sm">
              <div className="grid grid-cols-3 gap-2 text-center">
                <div className="rounded-lg bg-surface-2 p-2">
                  <div className="text-lg font-semibold tabular">{agree.reviewers}</div>
                  <div className="text-xs text-muted">reviewers</div>
                </div>
                <div className="rounded-lg bg-surface-2 p-2">
                  <div className="text-lg font-semibold tabular">{agree.percent_agreement !== undefined ? pct(agree.percent_agreement) : "—"}</div>
                  <div className="text-xs text-muted">agreement</div>
                </div>
                <div className="rounded-lg bg-surface-2 p-2">
                  <div className="text-lg font-semibold tabular">{agree.cohens_kappa !== undefined ? fixed(agree.cohens_kappa, 2) : "—"}</div>
                  <div className="text-xs text-muted">Cohen&apos;s κ</div>
                </div>
              </div>
              <p className="text-xs text-muted">{agree.cases_with_multiple_reviews} case(s) reviewed by more than one person.</p>
              {agree.reviewer_activity?.length > 0 && (
                <div className="flex flex-wrap gap-1.5">
                  {agree.reviewer_activity.map((r: any) => (
                    <Badge key={r.reviewer}>
                      {r.reviewer}: {r.decisions}
                    </Badge>
                  ))}
                </div>
              )}
              {agree.disputed.length > 0 ? (
                <ul className="space-y-2">
                  {agree.disputed.map((c: any) => (
                    <li key={c.id} className="flex items-center gap-2">
                      <SampleThumb s={c.sample} size={40} caption={false} />
                      <Link href={`/app/datasets/${versionId}/court/${c.id}`} className="text-sm font-medium hover:underline">
                        Case #{c.case_number}
                      </Link>
                      <Badge tone="bad">disputed</Badge>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-xs text-subtle">No disputed cases.</p>
              )}
            </div>
          )}
        </Card>
      </div>
    </RequireAudit>
  );
}

function SessionResume({ sessionId }: { sessionId: string }) {
  const { versionId } = useDataset();
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const resume = async () => {
    setBusy(true);
    const s = await api(`/review-sessions/${sessionId}`);
    const next = s.items.find((i: any) => i.status === "open") ?? s.items[0];
    if (next) router.push(`/app/datasets/${versionId}/court/${next.id}?session=${sessionId}`);
    setBusy(false);
  };
  return (
    <Button size="sm" variant="ghost" onClick={resume} loading={busy}>
      Resume
    </Button>
  );
}

