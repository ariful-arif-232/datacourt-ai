"use client";

import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { FileArchive, UploadCloud } from "lucide-react";
import { type DragEvent, type FormEvent, Suspense, useState } from "react";
import { api, useApi } from "@/lib/api";
import { abortUpload, finalizeUpload, uploadArchive } from "@/lib/upload";
import { bytes } from "@/lib/format";
import { Button, Card, cx, Field, inputCls, Notice, PageHeader, Progress } from "@/components/ui";

const MAX = 2 * 1024 ** 3;

function NewDatasetInner() {
  const { projectId } = useParams<{ projectId: string }>();
  const params = useSearchParams();
  const router = useRouter();
  const { data: project } = useApi<any>(`/projects/${projectId}`);
  const [target, setTarget] = useState<string>(params.get("dataset") ?? "new");
  const [file, setFile] = useState<File | null>(null);
  const [drag, setDrag] = useState(false);
  const [profile, setProfile] = useState<"fast" | "deep">("fast");
  const [stage, setStage] = useState<string | null>(null);
  const [progress, setProgress] = useState(0);
  const [uploading, setUploading] = useState(false);
  const [controller, setController] = useState<AbortController | null>(null);
  const [error, setError] = useState<string | null>(null);

  const pick = (f: File | undefined | null) => {
    setError(null);
    if (!f) return;
    if (!f.name.toLowerCase().endsWith(".zip")) return setError("Please choose a .zip archive.");
    if (f.size > MAX) return setError(`Archive is ${bytes(f.size)}; the limit is ${bytes(MAX)}.`);
    setFile(f);
  };

  const submit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!file) return setError("Choose a ZIP archive first.");
    const f = new FormData(e.currentTarget);
    setError(null);
    let versionId: string | null = null;
    const ctl = new AbortController();
    setController(ctl);
    try {
      let datasetId = target;
      if (target === "new") {
        setStage("Creating dataset");
        const provenance = Object.fromEntries(
          ["source", "source_url", "license", "collection_method", "collector", "capture_date_range", "consent_note"]
            .map((k) => [k, String(f.get(k) ?? "").trim()])
            .filter(([, v]) => v),
        );
        const d = await api(`/projects/${projectId}/datasets`, {
          json: { name: f.get("name"), description: f.get("description") ?? "", provenance: Object.keys(provenance).length ? provenance : undefined },
        });
        datasetId = d.id;
      }
      setStage("Preparing secure upload");
      const v = await api(`/datasets/${datasetId}/versions`, { json: { filename: file.name, size_bytes: file.size, notes: f.get("notes") ?? "", auto_audit: profile } });
      versionId = v.version.id as string;
      // Straight to private object storage with presigned URLs; large archives go in parts.
      setStage(v.upload.mode === "multipart" ? `Uploading archive (${v.upload.parts.length} parts, resumable)` : "Uploading archive");
      setProgress(0);
      setUploading(true);
      await uploadArchive(versionId, file, v.upload, setProgress, ctl.signal);
      setUploading(false);
      setStage("Verifying upload");
      await finalizeUpload(versionId, file, v.upload, ctl.signal);
      router.push(`/app/datasets/${versionId}/overview`);
    } catch (err: any) {
      const cancelled = err?.name === "AbortError";
      // The archive cannot be completed any more: discard received bytes and close the version.
      if (versionId) await abortUpload(versionId);
      setError(cancelled ? "Upload cancelled." : err.message);
      setStage(null);
      setUploading(false);
    } finally {
      setController(null);
    }
  };

  const existing = project?.datasets ?? [];
  return (
    <div className="mx-auto max-w-3xl px-4 py-8">
      <PageHeader
        eyebrow={project ? <Link href={`/app/projects/${projectId}`} className="hover:text-ink">{project.name}</Link> : "Project"}
        title="Upload a dataset"
        description="The original archive is stored immutably. DataCourt extracts it safely, validates every image and starts the audit automatically."
      />
      <form onSubmit={submit} className="space-y-5">
        <Card title="1 · Destination">
          <div className="space-y-4">
            <div className="flex flex-wrap gap-2" role="radiogroup" aria-label="Destination">
              <label className={cx("cursor-pointer rounded-lg border px-3 py-2 text-sm", target === "new" ? "border-accent bg-accent-soft" : "border-border")}>
                <input type="radio" className="sr-only" checked={target === "new"} onChange={() => setTarget("new")} /> New dataset
              </label>
              {existing.map((d: any) => (
                <label key={d.id} className={cx("cursor-pointer rounded-lg border px-3 py-2 text-sm", target === d.id ? "border-accent bg-accent-soft" : "border-border")}>
                  <input type="radio" className="sr-only" checked={target === d.id} onChange={() => setTarget(d.id)} /> New version of {d.name}
                </label>
              ))}
            </div>
            {target === "new" && (
              <>
                <Field label="Dataset name">
                  <input name="name" required maxLength={160} className={inputCls} placeholder="e.g. tomato-leaf-disease" />
                </Field>
                <Field label="Description (optional)">
                  <input name="description" className={inputCls} />
                </Field>
                <details className="rounded-lg border border-border p-3">
                  <summary className="cursor-pointer text-sm font-medium">Provenance & license (optional, recommended)</summary>
                  <p className="mt-2 text-xs text-muted">
                    DataCourt never invents provenance. You can also include a <code className="font-mono">provenance.csv</code> in the archive root (columns: path, source, license, collector, collection_method, capture_date, consent_note).
                  </p>
                  <div className="mt-3 grid gap-3 sm:grid-cols-2">
                    {[
                      ["source", "Source / origin"],
                      ["source_url", "Source URL"],
                      ["license", "License"],
                      ["collector", "Collector"],
                      ["collection_method", "Collection method"],
                      ["capture_date_range", "Capture date range"],
                    ].map(([k, l]) => (
                      <Field key={k} label={l}>
                        <input name={k} className={inputCls} />
                      </Field>
                    ))}
                    <div className="sm:col-span-2">
                      <Field label="Consent / provenance note">
                        <textarea name="consent_note" rows={2} className={inputCls} />
                      </Field>
                    </div>
                  </div>
                </details>
              </>
            )}
          </div>
        </Card>
        <Card title="2 · Archive">
          <label
            onDragOver={(e: DragEvent) => {
              e.preventDefault();
              setDrag(true);
            }}
            onDragLeave={() => setDrag(false)}
            onDrop={(e: DragEvent) => {
              e.preventDefault();
              setDrag(false);
              pick(e.dataTransfer.files?.[0]);
            }}
            className={cx("flex cursor-pointer flex-col items-center justify-center rounded-xl border-2 border-dashed px-6 py-10 text-center", drag ? "border-accent bg-accent-soft" : "border-border-strong hover:bg-surface-2")}
          >
            <input type="file" accept=".zip,application/zip" className="sr-only" onChange={(e) => pick(e.target.files?.[0])} />
            {file ? (
              <>
                <FileArchive className="size-7 text-accent" aria-hidden />
                <span className="mt-2 font-medium">{file.name}</span>
                <span className="text-xs text-muted">{bytes(file.size)} · click to change</span>
              </>
            ) : (
              <>
                <UploadCloud className="size-7 text-subtle" aria-hidden />
                <span className="mt-2 font-medium">Drop a .zip here or click to browse</span>
                <span className="mt-1 max-w-md text-xs text-muted">
                  Supported: <code className="font-mono">train/val/test/&lt;class&gt;/</code> (aliases valid, validation, dev, testing), <code className="font-mono">&lt;class&gt;/&lt;split&gt;/</code>, or class folders only. JPEG, PNG, WebP, BMP, TIFF, GIF.
                </span>
              </>
            )}
          </label>
          <div className="mt-4">
            <Field label="Version notes (optional)">
              <input name="notes" className={inputCls} placeholder="What changed in this version?" />
            </Field>
          </div>
        </Card>
        <Card title="3 · Audit profile">
          <div className="grid gap-3 sm:grid-cols-2" role="radiogroup" aria-label="Audit profile">
            {(
              [
                ["fast", "FAST", "Quality, hashing, embeddings, duplicates, leakage, cross-validated label evidence, shortcuts, coverage, cases, debt and preflight."],
                ["deep", "DEEP", "Everything in FAST plus training dynamics, TracIn influence, counterfactual estimates and shortcut perturbation tests."],
              ] as const
            ).map(([v, t, d]) => (
              <label key={v} className={cx("cursor-pointer rounded-xl border p-4", profile === v ? "border-accent bg-accent-soft" : "border-border hover:bg-surface-2")}>
                <input type="radio" className="sr-only" checked={profile === v} onChange={() => setProfile(v)} />
                <div className="font-semibold">{t}</div>
                <p className="mt-1 text-xs text-muted">{d}</p>
              </label>
            ))}
          </div>
          <p className="mt-3 text-xs text-subtle">Profiles are compute choices, not feature tiers. You can run a DEEP audit later on the same version.</p>
        </Card>
        {stage && (
          <div className="card space-y-2 p-4" aria-live="polite">
            <div className="flex justify-between text-sm">
              <span>{stage}…</span>
              {uploading && <span className="tabular text-muted">{Math.round(progress * 100)}%</span>}
            </div>
            <Progress value={uploading ? progress : stage === "Verifying upload" ? 1 : 0.05} />
            {uploading && (
              <p className="text-xs text-muted">
                {bytes(Math.round(progress * (file?.size ?? 0)))} of {bytes(file?.size ?? 0)} · sent directly to private storage; failed parts are retried automatically.
              </p>
            )}
          </div>
        )}
        {error && <Notice tone="bad">{error}</Notice>}
        <div className="flex justify-end gap-2">
          {controller && uploading && (
            <Button type="button" onClick={() => controller.abort()}>
              Cancel upload
            </Button>
          )}
          <Button type="submit" variant="primary" loading={!!stage} disabled={!file}>
            Upload & audit
          </Button>
        </div>
      </form>
    </div>
  );
}

export default function NewDatasetPage() {
  return (
    <Suspense>
      <NewDatasetInner />
    </Suspense>
  );
}
