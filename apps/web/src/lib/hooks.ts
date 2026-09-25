"use client";

import { useState } from "react";
import { useApi } from "@/lib/api";
import { useDataset } from "@/lib/dataset";

/** Fetch a dataset-version scoped endpoint, only once a completed audit exists. */
export function useVersionApi<T = any>(path: string | null, opts?: { requireAudit?: boolean; refreshInterval?: number }) {
  const { versionId, hasAudit } = useDataset();
  const ready = opts?.requireAudit === false || hasAudit;
  return useApi<T>(path !== null && ready ? `/versions/${versionId}${path}` : null, opts?.refreshInterval ? { refreshInterval: opts.refreshInterval } : undefined);
}

export function caseHref(versionId: string, caseId: string) {
  return `/app/datasets/${versionId}/court/${caseId}`;
}

/**
 * Pagination offset that resets to 0 whenever any filter value changes, derived during render
 * (no effect), so the first request after a filter change already uses offset 0.
 */
export function useFilteredOffset(...filters: unknown[]): [number, (offset: number) => void] {
  const key = JSON.stringify(filters);
  const [state, setState] = useState({ key, offset: 0 });
  const offset = state.key === key ? state.offset : 0;
  return [offset, (o: number) => setState({ key, offset: o })];
}
