"use client";

import Link from "next/link";
import { Download, PackageCheck } from "lucide-react";
import { useState } from "react";
import { RequireAudit } from "@/components/audit-progress";
import { WorkerStatus } from "@/components/worker-status";
import { Badge, Button, Card, EmptyState, KV, Loading, Notice, PageHeader, Progress, StatusBadge } from "@/components/ui";
import { api } from "@/lib/api";
import { useDataset } from "@/lib/dataset";
import { ago, bytes, num } from "@/lib/format";
import { useVersionApi } from "@/lib/hooks";
import { useSession } from "@/lib/session";

export default function ExportPage() {
  const { versionId } = useDataset();
  const { isAdmin } = useSession();
  const { data: exports, mutate } = useVersionApi<any[]>("/exports", { refreshInterval: 3000 });
  const { data: cases } = useVersionApi<any>("/cases?limit=1");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const statusCounts: Record<string, number> = {};
  (cases?.counts ?? []).forEach((c: any) => (statusCounts[c.status] = (statusCounts[c.status] ?? 0) + c.count));

  const start = async () => {
    setBusy(true);
    setErr(null);
    try {
      await api("/exports", { json: { dataset_version_id: versionId, auto_audit: "fast" } });
      mutate();
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };
  const download = async (id: string) => {
    const r = await api(`/exports/${id}/download`);
    window.location.assign(r.url);
  };

  return (
    <RequireAudit>
      <PageHeader
        title="Clean dataset export"
        description="Build a new dataset version from the immutable original plus final human decisions. The source version is never modified; nothing is removed or relabelled on AI suggestion alone."
      />
      <div className="grid gap-5 lg:grid-cols-[380px_1fr]">
        <Card title="What will be applied">
          <KV
            items={[
              ["Decided cases", num(statusCounts.decided ?? 0)],
              ["Adjudicated (resolved)", num(statusCounts.resolved ?? 0)],
              ["Disputed (not applied)", num(statusCounts.disputed ?? 0)],
              ["Open (not applied)", num(statusCounts.open ?? 0)],
            ]}
          />
          <ul className="mt-3 list-disc space-y-1 pl-4 text-xs text-muted">
            <li>REMOVE → excluded; RELABEL → moved to the target class folder; KEEP/UNSURE/ESCALATE → unchanged.</li>
            <li>Only consensus decisions or expert adjudications are applied.</li>
            <li>The archive includes manifest.json, action_log.json, removed_samples.csv, relabel_mapping.csv and SHA256SUMS.</li>
            <li>The export is registered as the next version and audited, so Version Intelligence can compare them.</li>
          </ul>
          {isAdmin ? (
            <Button variant="primary" className="mt-4 w-full" onClick={start} loading={busy}>
              <PackageCheck className="size-4" aria-hidden /> Build export
            </Button>
          ) : (
            <p className="mt-4 text-xs text-muted">Exports require the admin role.</p>
          )}
          {(statusCounts.disputed ?? 0) > 0 && (
            <div className="mt-3">
              <Notice tone="warn">
                {statusCounts.disputed} disputed case(s) will not be applied until adjudicated. <Link className="underline" href={`/app/datasets/${versionId}/review`}>Resolve disputes</Link>
              </Notice>
            </div>
          )}
          {err && <div className="mt-3"><Notice tone="bad">{err}</Notice></div>}
        </Card>
        <Card title="Exports">
          {!exports ? (
            <Loading rows={1} />
          ) : exports.length === 0 ? (
            <EmptyState title="No exports yet" />
          ) : (
            <ul className="divide-y divide-border">
              {exports.map((e) => (
                <li key={e.id} className="space-y-1.5 py-3">
                  <div className="flex flex-wrap items-center gap-2 text-sm">
                    <span className="font-medium">{e.name === "pending" ? "Export" : e.name}</span>
                    <StatusBadge status={e.status} />
                    <span className="text-xs text-subtle">{ago(e.created_at)}</span>
                    <div className="flex-1" />
                    {e.expired && <Badge>archive expired (retention)</Badge>}
                    {e.status === "completed" && !e.expired && (
                      <Button size="sm" onClick={() => download(e.id)}>
                        <Download className="size-4" aria-hidden /> Download ({bytes(e.byte_size)})
                      </Button>
                    )}
                  </div>
                  {["queued", "running"].includes(e.status) && (
                    <>
                      <Progress value={e.progress ?? 0} label="Export progress" />
                      <WorkerStatus job={e.execution} />
                    </>
                  )}
                  {e.status === "completed" && (
                    <p className="text-xs text-muted">
                      kept {num(e.summary.kept)} · removed {num(e.summary.removed)} · relabelled {num(e.summary.relabelled)} · sha256 <span className="font-mono">{e.sha256?.slice(0, 16)}…</span>
                      {e.new_version_id && (
                        <>
                          {" "}
                          ·{" "}
                          <Link href={`/app/datasets/${e.new_version_id}/overview`} className="text-accent hover:underline">
                            open v{e.summary.new_version_number}
                          </Link>
                        </>
                      )}
                    </p>
                  )}
                  {e.error && <Notice tone="bad">{e.error}</Notice>}
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </RequireAudit>
  );
}
