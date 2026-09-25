"use client";

import { CheckCircle2, Circle, Loader2, MinusCircle, XCircle } from "lucide-react";
import { type ReactNode, useState } from "react";
import { api, useApi } from "@/lib/api";
import type { VersionDetail } from "@/lib/dataset";
import { useDataset } from "@/lib/dataset";
import { duration, humanize } from "@/lib/format";
import { useSession } from "@/lib/session";
import { Button, Card, EmptyState, Progress, StatusBadge } from "@/components/ui";
import { type JobExecution, WorkerStatus } from "@/components/worker-status";

type ProgressResp = {
  status: string;
  progress: number;
  current_stage: string | null;
  elapsed_seconds: number | null;
  warnings: string[];
  error: string | null;
  job: JobExecution | null;
  steps: { stage: string; status: string; duration_ms: number | null; error: string | null; attempts: number; warnings: string[]; reason?: string }[];
};

export function AuditProgress({ version }: { version: VersionDetail }) {
  const audit = version.audits.find((a) => a.status === "queued" || a.status === "running") ?? version.audits[0];
  const ingesting = ["uploaded", "ingesting"].includes(version.status);
  const { canWrite } = useSession();
  const { data, mutate } = useApi<ProgressResp>(audit && !ingesting ? `/audits/${audit.id}/progress` : null, { refreshInterval: 2000 });
  const cancel = async () => {
    if (!audit) return;
    await api(`/audits/${audit.id}/cancel`, { method: "POST" }).catch(() => undefined);
    mutate();
  };
  if (ingesting) {
    const j = version.ingest_job;
    return (
      <div className="card mb-6 p-4" aria-live="polite">
        <div className="mb-2 flex items-center justify-between text-sm">
          <span className="flex items-center gap-2 font-medium">
            <Loader2 className="size-4 animate-spin text-accent" aria-hidden /> {j?.status === "queued" ? "Queued for ingestion" : "Ingesting archive safely"}
          </span>
          <span className="tabular text-muted">{Math.round((j?.progress ?? 0) * 100)}%</span>
        </div>
        <Progress value={j?.progress ?? 0} label="Ingestion progress" />
        <p className="mt-2 text-xs text-muted">
          Validating paths, decoding every image, hashing (SHA-256 + perceptual), creating previews and the manifest. The original archive is stored immutably.
        </p>
        <WorkerStatus job={j} />
      </div>
    );
  }
  if (!data || !audit) return null;
  return (
    <div className="card mb-6 p-4" aria-live="polite">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-sm">
        <span className="flex items-center gap-2 font-medium">
          <Loader2 className="size-4 animate-spin text-accent" aria-hidden />
          {humanize(audit.profile)} audit · {humanize(data.current_stage ?? "queued")}
        </span>
        <span className="flex items-center gap-3 tabular text-muted">
          <span>elapsed {duration(data.elapsed_seconds)}</span>
          <span>{Math.round(data.progress * 100)}%</span>
        </span>
      </div>
      <Progress value={data.progress} label="Audit progress" />
      <ol className="mt-3 flex flex-wrap gap-x-3 gap-y-1 text-[11.5px]">
        {data.steps.map((s) => (
          <li key={s.stage} className="flex items-center gap-1 text-muted" title={s.error ?? s.reason ?? undefined}>
            <StepIcon status={s.status} />
            <span className={s.status === "running" ? "font-medium text-ink" : undefined}>{humanize(s.stage)}</span>
          </li>
        ))}
      </ol>
      <WorkerStatus job={data.job} onCancel={canWrite ? cancel : undefined} />
      {data.warnings.length > 0 && (
        <ul className="mt-3 list-disc pl-5 text-xs text-warn">
          {data.warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function StepIcon({ status }: { status: string }) {
  if (status === "completed") return <CheckCircle2 className="size-3.5 text-ok" aria-label="completed" />;
  if (status === "running") return <Loader2 className="size-3.5 animate-spin text-accent" aria-label="running" />;
  if (status === "failed") return <XCircle className="size-3.5 text-bad" aria-label="failed" />;
  if (status === "skipped") return <MinusCircle className="size-3.5 text-subtle" aria-label="skipped" />;
  return <Circle className="size-3.5 text-border-strong" aria-label="pending" />;
}

/** Renders children only when the version has a completed audit; otherwise explains what to do. */
export function RequireAudit({ children }: { children: ReactNode }) {
  const { version, hasAudit, busy, refresh } = useDataset();
  const { canWrite } = useSession();
  const [starting, setStarting] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  if (!version) return null;
  if (hasAudit) return <>{children}</>;
  if (busy) return <EmptyState title="Audit in progress">Results appear here as soon as the audit completes. You can leave this page — processing continues in the background.</EmptyState>;
  const latest = version.audits[0];
  const start = async (profile: "fast" | "deep") => {
    setStarting(profile);
    setError(null);
    try {
      await api("/audits", { json: { dataset_version_id: version.id, profile } });
      refresh();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setStarting(null);
    }
  };
  const retry = async () => {
    if (!latest) return;
    await api(`/audits/${latest.id}/retry`, { method: "POST" }).catch((e) => setError(e.message));
    refresh();
  };
  return (
    <Card>
      <EmptyState
        title={version.status !== "ready" ? `Dataset version is ${humanize(version.status)}` : latest?.status === "failed" ? "The last audit failed" : "No completed audit yet"}
        action={
          version.status === "failed" && canWrite ? (
            <RetryIngestButton versionId={version.id} onDone={refresh} />
          ) : version.status === "ready" && canWrite ? (
            <div className="flex flex-wrap justify-center gap-2">
              {latest?.status === "failed" && (
                <Button variant="primary" onClick={retry}>
                  Resume failed audit
                </Button>
              )}
              <Button variant={latest?.status === "failed" ? "secondary" : "primary"} loading={starting === "fast"} onClick={() => start("fast")}>
                Run FAST audit
              </Button>
              <Button loading={starting === "deep"} onClick={() => start("deep")}>
                Run DEEP audit
              </Button>
            </div>
          ) : undefined
        }
      >
        {version.error ?? (latest?.status === "failed" ? "Completed stages are kept; resuming re-runs only what failed." : "FAST covers quality, hashing, embeddings, duplicates, leakage and model-based label evidence. DEEP adds training dynamics, influence and shortcut perturbation tests.")}
        {latest && (
          <span className="mt-2 block">
            Last audit: <StatusBadge status={latest.status} />
          </span>
        )}
        {error && <span className="mt-2 block text-bad">{error}</span>}
      </EmptyState>
    </Card>
  );
}

/** Re-runs ingestion of a failed version from the archive it already stored (no new upload). */
export function RetryIngestButton({ versionId, onDone }: { versionId: string; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const retry = async () => {
    setBusy(true);
    setError(null);
    try {
      await api(`/versions/${versionId}/retry-ingest`, { method: "POST" });
      onDone();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <span className="inline-flex flex-col items-center gap-1">
      <Button size="sm" variant="primary" loading={busy} onClick={retry}>
        Retry ingestion
      </Button>
      {error && <span className="text-xs text-bad">{error}</span>}
    </span>
  );
}
