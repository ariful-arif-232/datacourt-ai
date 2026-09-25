"use client";

import { useState } from "react";
import { RequireAudit } from "@/components/audit-progress";
import { Badge, Button, Card, InfoTip, LevelBadge, Loading, Notice, PageHeader } from "@/components/ui";
import { api } from "@/lib/api";
import { useDataset } from "@/lib/dataset";
import { ago, fixed } from "@/lib/format";
import { useVersionApi } from "@/lib/hooks";
import { useSession } from "@/lib/session";

const LEVELS = ["LOW", "MODERATE", "HIGH", "CRITICAL"];

export default function DebtPage() {
  const { versionId } = useDataset();
  const { canWrite } = useSession();
  const { data, mutate } = useVersionApi<any>("/debt");
  const [msg, setMsg] = useState<string | null>(null);
  const snapshot = async () => {
    const r = await api(`/versions/${versionId}/debt/snapshot`, { method: "POST" });
    setMsg(`Snapshot recorded: ${r.overall}.`);
    mutate();
  };
  return (
    <RequireAudit>
      <PageHeader
        title="Dataset Debt"
        description="Measurable data problems that make a dataset riskier to train on, in eight dimensions. Every value has a written formula over audited facts; nothing is a mystery score."
        actions={canWrite && <Button size="sm" onClick={snapshot}>Snapshot current debt</Button>}
      />
      {msg && <div className="mb-4"><Notice tone="ok">{msg}</Notice></div>}
      {!data ? (
        <Loading />
      ) : (
        <div className="space-y-5">
          <Card>
            <div className="flex flex-wrap items-center gap-4">
              <div>
                <div className="flex items-center gap-1 text-xs text-muted">
                  Overall (live, includes review progress) <InfoTip term="debt" />
                </div>
                <div className="mt-1 scale-125 origin-left">
                  <LevelBadge level={data.live.overall} />
                </div>
              </div>
              {data.at_audit && (
                <div className="text-sm text-muted">
                  At audit time: <LevelBadge level={data.at_audit.overall} />
                </div>
              )}
              <p className="flex-1 text-xs text-subtle">{data.live.overall_rule}</p>
            </div>
          </Card>
          <div className="grid gap-4 md:grid-cols-2">
            {Object.entries(data.live.dimensions).map(([key, d]: [string, any]) => (
              <Card
                key={key}
                title={
                  <span className="flex items-center gap-2">
                    {d.name} <LevelBadge level={d.level} />
                    {!d.included_in_overall && <Badge>tracked only</Badge>}
                  </span>
                }
              >
                <div className="mb-2 flex items-baseline gap-2">
                  <span className="text-2xl font-semibold tabular">{d.unit === "rate" ? `${(d.value * 100).toFixed(2)}%` : fixed(d.value, 2)}</span>
                  <span className="text-xs text-subtle">
                    thresholds: moderate {d.unit === "rate" ? `${d.thresholds.moderate * 100}%` : d.thresholds.moderate} · high {d.unit === "rate" ? `${d.thresholds.high * 100}%` : d.thresholds.high} · critical{" "}
                    {d.unit === "rate" ? `${d.thresholds.critical * 100}%` : d.thresholds.critical}
                  </span>
                </div>
                <div className="relative mb-3 h-2 rounded-full bg-surface-3">
                  <div className="absolute inset-y-0 left-0 rounded-full" style={{ width: `${Math.min(100, d.score)}%`, background: d.level === "LOW" ? "var(--ok)" : d.level === "MODERATE" ? "var(--warn)" : "var(--bad)" }} />
                </div>
                <p className="text-xs text-muted">
                  <b>Formula:</b> {d.formula}
                </p>
                <p className="mt-1 font-mono text-[11px] text-subtle">
                  {Object.entries(d.metrics)
                    .filter(([, v]) => typeof v !== "object" || v === null)
                    .map(([k, v]) => `${k}=${v}`)
                    .join(" · ")}
                </p>
                <p className="mt-2 text-sm">{d.recommendation}</p>
                {d.contributors?.length > 0 && (
                  <p className="mt-1 text-xs text-subtle">
                    Top contributors:{" "}
                    {d.contributors.map((c: any) => (c.case_number ? `case #${c.case_number}` : `${c.risk} ${c.kind} (${c.size})`)).join(", ")}
                  </p>
                )}
              </Card>
            ))}
          </div>
          <Card title="Debt across versions" subtitle="Latest snapshot per version (audit or manual snapshot).">
            {data.trend.length < 2 ? (
              <p className="text-sm text-muted">Trends appear once the dataset has more than one audited version (for example after a clean export).</p>
            ) : null}
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-left text-xs text-subtle">
                  <tr>
                    <th className="py-1.5 font-medium">Version</th>
                    <th className="py-1.5 font-medium">Overall</th>
                    {Object.keys(data.live.dimensions).map((k) => (
                      <th key={k} className="py-1.5 font-medium">
                        {k}
                      </th>
                    ))}
                    <th className="py-1.5 font-medium">When</th>
                  </tr>
                </thead>
                <tbody>
                  {data.trend.map((t: any) => (
                    <tr key={t.version_id} className="border-t hairline">
                      <td className="py-1.5">v{t.version}</td>
                      <td className="py-1.5">
                        <LevelBadge level={t.overall} />
                      </td>
                      {Object.keys(data.live.dimensions).map((k) => (
                        <td key={k} className="py-1.5 text-xs" title={`value ${t.values[k]}`}>
                          <span className="sr-only">{t.levels[k]}</span>
                          <span aria-hidden className="inline-flex gap-0.5">
                            {LEVELS.map((l, i) => (
                              <span key={l} className="h-2.5 w-2 rounded-sm" style={{ background: i <= LEVELS.indexOf(t.levels[k]) ? (t.levels[k] === "LOW" ? "var(--ok)" : t.levels[k] === "MODERATE" ? "var(--warn)" : "var(--bad)") : "var(--surface-3)" }} />
                            ))}
                          </span>
                        </td>
                      ))}
                      <td className="py-1.5 text-xs text-muted">{ago(t.at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="mt-2 text-xs text-subtle">{data.note}</p>
          </Card>
        </div>
      )}
    </RequireAudit>
  );
}
