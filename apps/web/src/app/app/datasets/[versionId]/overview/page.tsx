"use client";

import Link from "next/link";
import { useState } from "react";
import { RequireAudit, RetryIngestButton } from "@/components/audit-progress";
import { BarList, DnaStrip, Histogram, StackedBars } from "@/components/charts";
import { Badge, Button, Card, KV, LevelBadge, Notice, PageHeader, PreflightBadge, Stat, StatusBadge, VerdictBadge } from "@/components/ui";
import { api } from "@/lib/api";
import { useDataset } from "@/lib/dataset";
import { ago, bytes, humanize, num, pct } from "@/lib/format";
import { useVersionApi } from "@/lib/hooks";
import { useSession } from "@/lib/session";

const VERDICT_ORDER = ["LEAKAGE_ACTION_NEEDED", "POSSIBLE_RELABEL", "STRONG_REVIEW", "POSSIBLE_REMOVE", "REVIEW", "UNCERTAIN", "LIKELY_RARE", "KEEP"];

export default function OverviewPage() {
  const { version, versionId, refresh, busy } = useDataset();
  const { data: ov } = useVersionApi<any>("/overview");
  const { canWrite } = useSession();
  const [err, setErr] = useState<string | null>(null);
  if (!version) return null;
  const st = version.stats || {};
  const splits: string[] = Object.keys(st.splits || {}).sort((a, b) => ["train", "val", "test", "unsplit"].indexOf(a) - ["train", "val", "test", "unsplit"].indexOf(b));
  const classRows = Object.entries((st.class_split_counts || {}) as Record<string, Record<string, number>>)
    .map(([label, values]) => ({ label, values }))
    .sort((a, b) => Object.values(b.values).reduce((x, y) => x + y, 0) - Object.values(a.values).reduce((x, y) => x + y, 0));
  const verdicts = ov?.audit?.summary?.verdicts || {};
  const facts = ov?.facts;

  const start = async (profile: "fast" | "deep") => {
    setErr(null);
    try {
      await api("/audits", { json: { dataset_version_id: versionId, profile } });
      refresh();
    } catch (e: any) {
      setErr(e.message);
    }
  };

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow={`Version ${version.version_number}${version.origin === "export" ? " · DataCourt export" : ""} · uploaded ${ago(version.created_at)}`}
        title={version.dataset.name}
        description={version.notes || "Dataset workspace: what is in the data, what looks wrong, and what to do first."}
        actions={
          canWrite &&
          version.status === "ready" && (
            <>
              <Button size="sm" onClick={() => start("fast")} disabled={busy}>
                Re-run FAST audit
              </Button>
              <Button size="sm" variant="primary" onClick={() => start("deep")} disabled={busy}>
                Run DEEP audit
              </Button>
            </>
          )
        }
      />
      {err && <Notice tone="bad">{err}</Notice>}
      {version.status === "failed" && (
        <Notice tone="bad" title="Ingestion failed">
          {version.error}
          {canWrite && (
            <div className="mt-2">
              <RetryIngestButton versionId={versionId} onDone={refresh} />
            </div>
          )}
        </Notice>
      )}

      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <Stat label="Images" value={num(st.sample_count)} sub={`${num(st.unique_sha256)} unique files`} />
        <Stat label="Classes" value={num(st.class_count)} sub={st.imbalance_ratio ? `imbalance ${st.imbalance_ratio.toFixed(1)}×` : undefined} tone={st.imbalance_ratio > 10 ? "warn" : undefined} />
        <Stat label="Splits" value={splits.length ? splits.join(" · ") : "—"} sub={splits.map((s) => num(st.splits[s])).join(" / ")} />
        <Stat label="Unreadable files" value={num(st.files?.rejected ?? 0)} sub={`${num(st.files?.ignored ?? 0)} ignored (non-image)`} tone={(st.files?.rejected ?? 0) > 0 ? "warn" : undefined} />
        <Stat label="Storage" value={bytes(st.total_bytes)} sub={`archive ${bytes(version.source_bytes)}`} />
        <Stat label="Dataset Debt" info="debt" value={ov?.debt ? <LevelBadge level={ov.debt.overall} /> : "—"} sub={ov?.preflight ? <PreflightBadge status={ov.preflight.override ?? ov.preflight.status} /> : "no audit yet"} />
      </div>

      <div className="grid gap-5 lg:grid-cols-[1.4fr_1fr]">
        <Card title="Class distribution by split" subtitle="Largest classes first. Hover a segment for exact counts.">
          <StackedBars rows={classRows} keys={splits} />
        </Card>
        <div className="space-y-5">
          <Card title="Resolution (shortest side)">
            <BarList
              data={["<32", "32-63", "64-127", "128-255", "256-511", "512-1023", ">=1024"]
                .filter((k) => st.resolution_buckets?.[k])
                .map((k) => ({ label: `${k} px`, value: st.resolution_buckets[k] }))}
            />
          </Card>
          <Card title="Formats & colour modes">
            <div className="flex flex-wrap gap-1.5">
              {Object.entries(st.formats || {}).map(([k, v]) => (
                <Badge key={k}>
                  {k} · {num(v as number)}
                </Badge>
              ))}
              {Object.entries(st.modes || {}).map(([k, v]) => (
                <Badge key={k} tone="info">
                  {k} · {num(v as number)}
                </Badge>
              ))}
            </div>
          </Card>
        </div>
      </div>

      <RequireAudit>
        {ov?.audit && (
          <>
            <div className="grid gap-5 lg:grid-cols-3">
              <Card title="Cases by verdict" info="verdict" actions={<Button size="sm" href={`/app/datasets/${versionId}/court`}>Open court</Button>}>
                <ul className="space-y-1.5">
                  {VERDICT_ORDER.filter((v) => verdicts[v]).map((v) => (
                    <li key={v} className="flex items-center justify-between text-sm">
                      <Link href={`/app/datasets/${versionId}/court?verdict=${v}`} className="hover:underline">
                        <VerdictBadge verdict={v} />
                      </Link>
                      <span className="tabular text-muted">{num(verdicts[v])}</span>
                    </li>
                  ))}
                </ul>
                {facts && (
                  <p className="mt-3 text-xs text-muted">
                    {num(facts.unresolved_cases)} unresolved · {num(facts.decided_cases)} decided · {num(facts.disputed_cases)} disputed
                  </p>
                )}
              </Card>
              <Card title="Findings at a glance">
                <KV
                  items={[
                    ["Quality concerns", <Link key="q" className="hover:underline" href={`/app/datasets/${versionId}/quality`}>{num(facts?.quality_samples_medium_plus)} samples</Link>],
                    ["Duplicate families", <Link key="d" className="hover:underline" href={`/app/datasets/${versionId}/duplicates`}>{num(facts?.duplicate_families)} ({num(facts?.redundant_samples)} redundant)</Link>],
                    ["Leaking eval samples", <Link key="l" className="hover:underline" href={`/app/datasets/${versionId}/leakage`}>{num(facts?.eval_samples_leaking)} ({num(facts?.exact_eval_leaks)} identical)</Link>],
                    ["Label review", <Link key="lb" className="hover:underline" href={`/app/datasets/${versionId}/labels`}>{num(facts?.label_actions?.REVIEW ?? 0)} review · {num(facts?.label_actions?.LIKELY_RARE ?? 0)} likely rare</Link>],
                    ["Shortcut cues", <Link key="s" className="hover:underline" href={`/app/datasets/${versionId}/shortcuts`}>{facts?.shortcut_strong ? `${facts.shortcut_strong} strong` : `max V ${facts?.shortcut_max_strength ?? "—"}`}</Link>],
                    ["Coverage gaps", <Link key="c" className="hover:underline" href={`/app/datasets/${versionId}/coverage`}>{num(facts?.gaps_high)} high · {num(facts?.gaps_medium)} medium</Link>],
                  ]}
                />
              </Card>
              <Card title="Dataset DNA" info="dna" actions={<Button size="sm" href={`/app/datasets/${versionId}/versions`}>Compare</Button>}>
                {ov.dna ? (
                  <>
                    <DnaStrip bands={ov.dna.strip} label={`Dataset DNA ${ov.dna.fingerprint}`} />
                    <p className="mt-2 font-mono text-xs text-muted">{ov.dna.fingerprint}</p>
                    <p className="mt-1 text-xs text-subtle">Manifest SHA-256 {version.manifest_sha256?.slice(0, 16)}…</p>
                  </>
                ) : (
                  <p className="text-sm text-muted">Not computed.</p>
                )}
              </Card>
            </div>
            <div className="grid gap-5 lg:grid-cols-2">
              <Card title="Brightness distribution" subtitle="Mean luminance per image (0–255)">
                <Histogram {...ov.distributions.brightness} xLabel="mean luminance" />
              </Card>
              <Card title="Sharpness distribution" subtitle="log10(Laplacian variance + 1) — a sharpness proxy">
                <Histogram {...ov.distributions.log10_sharpness} xLabel="log10 sharpness" format={(v) => v.toFixed(1)} />
              </Card>
            </div>
            <Card title="Audit history">
              <table className="w-full text-sm">
                <thead className="text-left text-xs text-subtle">
                  <tr>
                    <th className="py-1.5 font-medium">Started</th>
                    <th className="py-1.5 font-medium">Profile</th>
                    <th className="py-1.5 font-medium">Status</th>
                    <th className="py-1.5 font-medium">Cases</th>
                    <th className="py-1.5 font-medium">Debt</th>
                  </tr>
                </thead>
                <tbody>
                  {version.audits.map((a) => (
                    <tr key={a.id} className="border-t hairline">
                      <td className="py-1.5">{ago(a.created_at)}</td>
                      <td className="py-1.5">{humanize(a.profile)}</td>
                      <td className="py-1.5">
                        <StatusBadge status={a.status} />
                      </td>
                      <td className="py-1.5 tabular">{a.summary?.cases ?? "—"}</td>
                      <td className="py-1.5">{a.summary?.debt ? <LevelBadge level={a.summary.debt} /> : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="mt-2 text-xs text-subtle">
                Embedding model: {ov.audit.embedding_model ?? "—"} · provenance coverage {pct(facts?.provenance_coverage)} · results shown are
                from the latest completed audit.
              </p>
            </Card>
          </>
        )}
      </RequireAudit>
    </div>
  );
}
