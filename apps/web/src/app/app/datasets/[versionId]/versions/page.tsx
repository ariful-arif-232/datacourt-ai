"use client";

import Link from "next/link";
import { useState } from "react";
import { RequireAudit } from "@/components/audit-progress";
import { DnaStrip } from "@/components/charts";
import { Badge, Button, Card, EmptyState, InfoTip, KV, LevelBadge, Loading, Notice, PageHeader, Select, Stat, StatusBadge } from "@/components/ui";
import { api, qs, useApi } from "@/lib/api";
import { useDataset } from "@/lib/dataset";
import { ago, fixed, humanize, num, pct, signed } from "@/lib/format";
import { useVersionApi } from "@/lib/hooks";
import { useSession } from "@/lib/session";

export default function VersionsPage() {
  const { version, versionId } = useDataset();
  const { org, canWrite } = useSession();
  const others = (version?.versions ?? []).filter((v) => v.id !== versionId);
  const [other, setOther] = useState<string>("");
  const { data: diff } = useVersionApi<any>(`/compare${qs({ other: other || undefined })}`);
  const { data: dna } = useVersionApi<any>(`/dna${qs({ compare_to: other || undefined })}`);
  const { data: refs } = useApi<any[]>(org ? `/orgs/${org.id}/versions` : null);
  const { data: checks, mutate } = useVersionApi<any[]>("/contamination-checks", { refreshInterval: 4000 });
  const [ref, setRef] = useState("");
  const [err, setErr] = useState<string | null>(null);

  const runCheck = async () => {
    setErr(null);
    try {
      await api(`/versions/${versionId}/contamination-checks`, { json: { reference_version_id: ref } });
      mutate();
    } catch (e: any) {
      setErr(e.message);
    }
  };

  return (
    <RequireAudit>
      <PageHeader
        title="Version intelligence, Dataset DNA & drift"
        description="What changed between dataset versions — files, labels, splits, duplicates, leakage, label issues, shortcuts, debt and model metrics — and whether the data itself drifted."
        actions={
          others.length > 0 && (
            <Select
              label="Compare with"
              value={other}
              onChange={setOther}
              options={[{ value: "", label: "Compare with previous version" }, ...others.map((v) => ({ value: v.id, label: `Compare with v${v.version_number}` }))]}
            />
          )
        }
      />
      <div className="space-y-5">
        {dna && (
          <Card title="Dataset DNA" info="dna" subtitle={dna.note}>
            <div className="grid gap-4 md:grid-cols-2">
              <div>
                <div className="mb-1 flex items-center justify-between text-xs">
                  <span className="font-medium">This version (v{version?.version_number})</span>
                  <span className="font-mono text-muted">{dna.fingerprint}</span>
                </div>
                <DnaStrip bands={dna.profile.strip} />
              </div>
              {dna.drift && (
                <div>
                  <div className="mb-1 flex items-center justify-between text-xs">
                    <span className="font-medium">v{dna.drift.compared_to.version}</span>
                    <span className="font-mono text-muted">{dna.drift.compared_to.fingerprint}</span>
                  </div>
                  <p className="text-xs text-muted">Compare the strips band by band; drift signals below quantify the differences.</p>
                </div>
              )}
            </div>
          </Card>
        )}
        {dna?.drift && (
          <Card title="Drift watch" subtitle={dna.drift.method}>
            {dna.drift.headline.length > 0 ? (
              <div className="mb-3 space-y-1">
                {dna.drift.headline.map((h: string) => (
                  <Notice key={h} tone="warn">
                    {h}
                  </Notice>
                ))}
              </div>
            ) : (
              <Notice tone="ok">No material drift between these versions.</Notice>
            )}
            <table className="mt-2 w-full text-sm">
              <thead className="text-left text-xs text-subtle">
                <tr>
                  <th className="py-1 font-medium">Signal</th>
                  <th className="py-1 font-medium">Scope</th>
                  <th className="py-1 font-medium">Value</th>
                  <th className="py-1 font-medium">Severity</th>
                </tr>
              </thead>
              <tbody>
                {dna.drift.signals.slice(0, 14).map((s: any, i: number) => (
                  <tr key={i} className="border-t hairline">
                    <td className="py-1.5">{s.message}</td>
                    <td className="py-1.5 text-muted">{s.scope}</td>
                    <td className="py-1.5 tabular">{fixed(s.value, 4)}</td>
                    <td className="py-1.5">
                      <Badge tone={s.severity === "strong" ? "bad" : s.severity === "moderate" ? "warn" : "neutral"}>{s.severity}</Badge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
        )}
        {!diff ? (
          <Loading />
        ) : !diff.available ? (
          <EmptyState title="Nothing to compare yet">{diff.reason} Export a cleaned version or upload a new one to see version intelligence.</EmptyState>
        ) : (
          <>
            <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
              <Stat label={`Samples v${diff.from.version} → v${diff.to.version}`} value={`${num(diff.from.samples)} → ${num(diff.to.samples)}`} />
              <Stat label="Files added / removed" value={`+${num(diff.files.added)} / −${num(diff.files.removed)}`} sub={`${num(diff.files.unchanged)} unchanged (by SHA-256)`} />
              <Stat label="Labels changed" value={num(diff.labels_changed.count)} />
              <Stat label="Split changes" value={num(diff.splits_changed.count)} />
            </div>
            <div className="grid gap-5 lg:grid-cols-2">
              <Card title="Issues: fixed vs new">
                <KV
                  items={[
                    ["Leakage families", `${diff.leakage.fixed} fixed · ${diff.leakage.new} new · ${diff.leakage.persisting} persisting`],
                    ["Label issues (REVIEW)", `${diff.label_issues.resolved} resolved · ${diff.label_issues.new} new · ${diff.label_issues.persisting} persisting`],
                    ["Duplicate families", `${diff.duplicates.resolved_families} resolved · ${diff.duplicates.new_families} new`],
                    ["Shortcut risks", `${diff.shortcuts.resolved.length} resolved · ${diff.shortcuts.new.length} new`],
                    ["Classes", diff.classes.added.length || diff.classes.removed.length ? `+${diff.classes.added.join(", ") || "none"} / −${diff.classes.removed.join(", ") || "none"}` : "unchanged"],
                  ]}
                />
                {!diff.audits_available.from && <p className="mt-2 text-xs text-warn">The older version has no completed audit; issue comparisons are partial.</p>}
              </Card>
              <Card title="Debt & model metrics trend">
                <KV
                  items={[
                    ["Dataset Debt", <span key="d" className="flex items-center gap-2">{diff.debt.from ? <LevelBadge level={diff.debt.from.overall} /> : "—"} → {diff.debt.to ? <LevelBadge level={diff.debt.to.overall} /> : "—"}</span>],
                    [
                      "Cross-validated macro F1",
                      `${fixed(diff.model_metrics.from?.cross_validation?.macro_f1, 3)} → ${fixed(diff.model_metrics.to?.cross_validation?.macro_f1, 3)} (${signed((diff.model_metrics.to?.cross_validation?.macro_f1 ?? NaN) - (diff.model_metrics.from?.cross_validation?.macro_f1 ?? NaN))})`,
                    ],
                    ["Evaluation macro F1", `${fixed(diff.model_metrics.from?.eval?.macro_f1, 3)} → ${fixed(diff.model_metrics.to?.eval?.macro_f1, 3)}`],
                  ]}
                />
                <p className="mt-2 text-xs text-subtle">{diff.model_metrics.note}</p>
              </Card>
            </div>
            {diff.labels_changed.examples.length > 0 && (
              <Card title="Label changes (same file, different label)">
                <ul className="max-h-64 space-y-1 overflow-y-auto text-xs">
                  {diff.labels_changed.examples.map((e: any) => (
                    <li key={e.sha256} className="flex justify-between gap-3">
                      <span className="truncate font-mono text-muted">{e.path}</span>
                      <span>
                        {e.from.join(", ")} → <b>{e.to.join(", ")}</b>
                      </span>
                    </li>
                  ))}
                </ul>
              </Card>
            )}
          </>
        )}
        <Card title="Evaluation contamination check" subtitle="Check this version against another dataset version you own (e.g. an internal benchmark). DataCourt never looks at data outside your workspace.">
          {canWrite && (
            <div className="mb-3 flex flex-wrap items-center gap-2">
              <Select
                label="Reference version"
                value={ref}
                onChange={setRef}
                options={[{ value: "", label: "Choose a reference dataset version…" }, ...(refs ?? []).filter((r) => r.id !== versionId).map((r) => ({ value: r.id, label: `${r.dataset} v${r.version_number} (${num(r.samples)})` }))]}
              />
              <Button size="sm" onClick={runCheck} disabled={!ref}>
                Run check
              </Button>
            </div>
          )}
          {err && <Notice tone="bad">{err}</Notice>}
          {(checks ?? []).length === 0 ? (
            <p className="text-sm text-muted">No checks yet.</p>
          ) : (
            <ul className="space-y-3">
              {checks!.map((c) => (
                <li key={c.id} className="rounded-lg border hairline p-3 text-sm">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium">vs {c.reference}</span>
                    <StatusBadge status={c.status} />
                    {c.result?.verdict && <Badge tone={c.result.verdict === "clean" ? "ok" : "bad"}>{humanize(c.result.verdict)}</Badge>}
                    <span className="text-xs text-subtle">{ago(c.created_at)}</span>
                  </div>
                  {c.status === "completed" && (
                    <p className="mt-1 text-xs text-muted">
                      {c.result.exact_overlap} byte-identical and {c.result.near_overlap} near-duplicate overlaps ({pct(c.result.contaminated_fraction, 2)} of this version). Method: {c.result.near_duplicate_method}.
                    </p>
                  )}
                </li>
              ))}
            </ul>
          )}
        </Card>
        <Card title="All versions">
          <ul className="space-y-1 text-sm">
            {(version?.versions ?? []).map((v) => (
              <li key={v.id} className="flex items-center gap-2">
                <Link href={`/app/datasets/${v.id}/overview`} className={v.id === versionId ? "font-semibold" : "hover:underline"}>
                  v{v.version_number}
                </Link>
                <StatusBadge status={v.status} />
                {v.origin === "export" && <Badge tone="accent">DataCourt export</Badge>}
              </li>
            ))}
          </ul>
          <p className="mt-2 text-xs text-subtle">
            DNA fingerprints are descriptive <InfoTip term="dna" />; the manifest SHA-256 identifies exact contents.
          </p>
        </Card>
      </div>
    </RequireAudit>
  );
}
