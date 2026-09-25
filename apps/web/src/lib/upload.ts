"use client";

import { api, ApiError } from "@/lib/api";

/**
 * Browser → object storage uploads with presigned URLs. The archive never passes through the app
 * servers (Vercel functions): small archives use one PUT, large ones a multipart upload whose
 * parts are sent in parallel and retried individually. The server completes a multipart upload
 * from its own list of received parts, so the bucket's CORS rules do not need to expose ETags.
 */

export type UploadPlan =
  | { mode: "single"; url: string; method: "PUT"; headers: Record<string, string>; expires_in: number }
  | { mode: "multipart"; part_bytes: number; parts: { part_number: number; url: string }[]; expires_in: number };

const PARALLEL_PARTS = 3;
const MAX_ATTEMPTS = 4;

type PutOptions = { headers?: Record<string, string>; signal?: AbortSignal; onProgress?: (loaded: number) => void };

/** PUT with upload progress (XHR reports upload progress; fetch does not). */
export function putWithProgress(url: string, body: Blob, { headers = {}, signal, onProgress }: PutOptions = {}): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(new DOMException("Upload cancelled", "AbortError"));
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url);
    Object.entries(headers).forEach(([k, v]) => xhr.setRequestHeader(k, v));
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress?.(e.loaded);
    xhr.onload = () =>
      xhr.status >= 200 && xhr.status < 300 ? resolve() : reject(new ApiError(xhr.status, `Upload failed (HTTP ${xhr.status})`));
    xhr.onerror = () => reject(new ApiError(0, "Network error during upload"));
    xhr.onabort = () => reject(new DOMException("Upload cancelled", "AbortError"));
    signal?.addEventListener("abort", () => xhr.abort(), { once: true });
    xhr.send(body);
  });
}

const sleep = (ms: number, signal?: AbortSignal) =>
  new Promise<void>((resolve, reject) => {
    const t = setTimeout(resolve, ms);
    signal?.addEventListener(
      "abort",
      () => {
        clearTimeout(t);
        reject(new DOMException("Upload cancelled", "AbortError"));
      },
      { once: true },
    );
  });

function isAbort(e: unknown): boolean {
  return e instanceof DOMException && e.name === "AbortError";
}

/** Upload `file` for dataset version `versionId` following the server's plan; reports 0..1 progress. */
export async function uploadArchive(
  versionId: string,
  file: File,
  plan: UploadPlan,
  onProgress: (fraction: number) => void,
  signal?: AbortSignal,
): Promise<void> {
  if (plan.mode === "single") {
    await putWithProgress(plan.url, file, { headers: plan.headers, signal, onProgress: (n) => onProgress(n / file.size) });
    onProgress(1);
    return;
  }

  const urls = new Map(plan.parts.map((p) => [p.part_number, p.url]));
  const loaded = new Map<number, number>();
  const report = () => onProgress(Math.min(1, [...loaded.values()].reduce((a, b) => a + b, 0) / file.size));
  const refresh = async (partNumbers: number[]) => {
    const fresh = await api<{ parts: { part_number: number; url: string }[] }>(`/versions/${versionId}/upload/parts`, {
      json: { part_numbers: partNumbers },
    });
    fresh.parts.forEach((p) => urls.set(p.part_number, p.url));
  };

  const sendPart = async (n: number) => {
    const start = (n - 1) * plan.part_bytes;
    const blob = file.slice(start, Math.min(file.size, start + plan.part_bytes));
    for (let attempt = 1; ; attempt++) {
      try {
        loaded.set(n, 0);
        await putWithProgress(urls.get(n)!, blob, {
          signal,
          onProgress: (b) => {
            loaded.set(n, b);
            report();
          },
        });
        loaded.set(n, blob.size);
        report();
        return;
      } catch (e) {
        if (isAbort(e) || attempt >= MAX_ATTEMPTS) throw e;
        // An expired or rejected URL is replaced; network hiccups back off and retry.
        if (e instanceof ApiError && (e.status === 403 || e.status === 400)) await refresh([n]);
        await sleep(1000 * 2 ** (attempt - 1), signal);
      }
    }
  };

  const queue = [...urls.keys()].sort((a, b) => a - b);
  const runners = Array.from({ length: Math.min(PARALLEL_PARTS, queue.length) }, async () => {
    for (let n = queue.shift(); n !== undefined; n = queue.shift()) await sendPart(n);
  });
  await Promise.all(runners);
}

/** Finalize; if the server reports missing parts (e.g. a part silently lost), send them and retry once. */
export async function finalizeUpload(versionId: string, file: File, plan: UploadPlan, signal?: AbortSignal): Promise<any> {
  try {
    return await api(`/versions/${versionId}/finalize-upload`, { method: "POST" });
  } catch (e) {
    const missing: number[] | undefined = (e as ApiError)?.status === 409 ? (e as any).detail?.missing_parts : undefined;
    if (plan.mode !== "multipart" || !missing?.length) throw e;
    const fresh = await api<{ parts: { part_number: number; url: string }[] }>(`/versions/${versionId}/upload/parts`, {
      json: { part_numbers: missing },
    });
    await uploadArchive(versionId, file, { ...plan, parts: fresh.parts }, () => {}, signal);
    return api(`/versions/${versionId}/finalize-upload`, { method: "POST" });
  }
}

/** Abandon an upload: discards received parts server-side and marks the version failed. */
export async function abortUpload(versionId: string): Promise<void> {
  await api(`/versions/${versionId}/upload/abort`, { method: "POST" }).catch(() => undefined);
}
