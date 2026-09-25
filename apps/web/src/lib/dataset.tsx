"use client";

import { createContext, type ReactNode, useContext } from "react";
import { useApi } from "@/lib/api";
import type { JobExecution } from "@/components/worker-status";

export type VersionDetail = {
  id: string;
  dataset_id: string;
  version_number: number;
  status: string;
  origin: string;
  source_filename: string | null;
  source_sha256: string | null;
  source_bytes: number | null;
  manifest_sha256: string | null;
  layout: any;
  stats: any;
  notes: string;
  error: string | null;
  created_at: string;
  ingested_at: string | null;
  dataset: { id: string; name: string; provenance: Record<string, string> };
  project: { id: string; name: string; org_id: string } | null;
  versions: { id: string; version_number: number; status: string; origin: string }[];
  audits: { id: string; status: string; profile: string; progress: number; current_stage: string | null; created_at: string; finished_at: string | null; summary: any }[];
  ingest_job: JobExecution | null;
  rejected_files: { path: string; reason: string }[];
};

type Ctx = { versionId: string; version: VersionDetail | undefined; refresh: () => void; hasAudit: boolean; busy: boolean };
const DatasetContext = createContext<Ctx | null>(null);

export function DatasetProvider({ versionId, children }: { versionId: string; children: ReactNode }) {
  const { data, mutate } = useApi<VersionDetail>(`/versions/${versionId}`, {
    refreshInterval: (d) => {
      const running = d && (["awaiting_upload", "uploaded", "ingesting"].includes(d.status) || d.audits.some((a) => a.status === "queued" || a.status === "running"));
      return running ? 2500 : 0;
    },
  });
  const hasAudit = !!data?.audits.some((a) => a.status === "completed");
  const busy = !!data && (["uploaded", "ingesting"].includes(data.status) || data.audits.some((a) => a.status === "queued" || a.status === "running"));
  return <DatasetContext.Provider value={{ versionId, version: data, refresh: () => void mutate(), hasAudit, busy }}>{children}</DatasetContext.Provider>;
}

export function useDataset(): Ctx {
  const ctx = useContext(DatasetContext);
  if (!ctx) throw new Error("useDataset outside DatasetProvider");
  return ctx;
}
