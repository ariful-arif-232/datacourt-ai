"use client";

import { ExternalLink, Hourglass, Loader2, RotateCw, TriangleAlert } from "lucide-react";
import type { ReactNode } from "react";
import { ago, duration, humanize, secondsSince } from "@/lib/format";
import { Button, Notice } from "@/components/ui";

/** Execution state of a background job, as returned by the API (`execution.status_for`). */
export type JobExecution = {
  job_id: string;
  type: string;
  status: string;
  progress: number;
  stage: string | null;
  attempts: number;
  max_attempts: number;
  backend: string;
  waiting: "dispatch_failed" | "retry_scheduled" | "starting_worker" | "waiting_for_worker" | null;
  dispatch_error: string | null;
  dispatched_at: string | null;
  runner_url: string | null;
  retry_at: string | null;
  error_class: string | null;
  error: string | null;
  timeout_seconds: number;
  cancel_requested: boolean;
  started_at: string | null;
  finished_at: string | null;
};

function RunLink({ url }: { url: string | null }) {
  if (!url) return null;
  return (
    <a href={url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-accent underline-offset-2 hover:underline">
      view run <ExternalLink className="size-3" aria-hidden />
    </a>
  );
}

/**
 * Where a queued or running job stands: waiting for a worker to start (GitHub Actions runs take a
 * little while to begin), running on a specific run, retrying after a recoverable failure, or not
 * starting because the worker could not be dispatched.
 */
export function WorkerStatus({ job, onCancel }: { job: JobExecution | null | undefined; onCancel?: () => void }) {
  if (!job || !["queued", "running"].includes(job.status)) return null;
  const onActions = job.backend === "github_actions";
  const cancel =
    onCancel && !job.cancel_requested ? (
      <Button size="sm" onClick={onCancel}>
        Cancel
      </Button>
    ) : null;

  if (job.status === "queued" && job.waiting === "dispatch_failed") {
    return (
      <div className="mt-3">
        <Notice tone="warn" title="A background worker could not be started">
          {job.dispatch_error ?? "The worker dispatch failed."} DataCourt keeps retrying while this page is open. If it persists, an
          administrator should check the GitHub Actions setup described in the deployment guide.
        </Notice>
      </div>
    );
  }

  let icon = <Hourglass className="size-3.5 text-subtle" aria-hidden />;
  let text: ReactNode;
  if (job.cancel_requested) {
    icon = <Loader2 className="size-3.5 animate-spin text-subtle" aria-hidden />;
    text = "Cancelling — the worker stops at its next checkpoint.";
  } else if (job.status === "running") {
    icon = <Loader2 className="size-3.5 animate-spin text-accent" aria-hidden />;
    text = (
      <>
        Running {onActions ? "on GitHub Actions" : "on a worker"}
        {job.attempts > 1 && ` · attempt ${job.attempts} of ${job.max_attempts}`}
        {job.started_at && ` · started ${ago(job.started_at)}`} <RunLink url={job.runner_url} />
      </>
    );
  } else if (job.waiting === "retry_scheduled") {
    icon = <RotateCw className="size-3.5 text-warn" aria-hidden />;
    text = (
      <>
        Attempt {job.attempts} of {job.max_attempts} failed ({humanize(job.error_class ?? "error")}); retrying automatically
        {job.retry_at && ` at ${new Date(job.retry_at).toLocaleTimeString()}`}. Completed work is kept.
      </>
    );
  } else if (job.waiting === "starting_worker") {
    const waited = secondsSince(job.dispatched_at);
    text = (
      <>
        Starting a worker on GitHub Actions{waited > 5 && ` (${duration(waited)})`} — this usually takes under a minute.
        {waited > 300 && " It is taking longer than usual; GitHub may be busy. DataCourt re-dispatches automatically."}
      </>
    );
  } else {
    text = "Queued — waiting for a worker.";
  }
  return (
    <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs text-muted" role="status">
      <span className="flex items-center gap-1.5">
        {icon}
        <span>{text}</span>
      </span>
      {cancel}
    </div>
  );
}

/** Terminal failure of a job, with the reason the worker recorded. */
export function JobFailure({ job }: { job: JobExecution | null | undefined }) {
  if (!job || job.status !== "failed") return null;
  return (
    <Notice tone="bad" title={job.error_class === "timeout" ? "The job ran out of time" : "The job failed"}>
      <span className="flex items-start gap-1.5">
        <TriangleAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden />
        <span>
          {job.error ?? "No reason was recorded."} <RunLink url={job.runner_url} />
        </span>
      </span>
    </Notice>
  );
}
