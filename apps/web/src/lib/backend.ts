"use client";

import useSWR from "swr";

/**
 * Whether the DataCourt API behind this deployment can serve requests, checked at runtime via
 * `/api/v1/health` (the API answers 503 with `configured: false` until its production settings are
 * in place). Only a definite answer disables sign-in: an unexpected response counts as ready, so a
 * transient hiccup never locks users out; real errors surface on the action itself.
 */
export type BackendState = "checking" | "ready" | "unconfigured" | "unreachable";

export async function probeBackend(): Promise<BackendState> {
  try {
    const res = await fetch("/api/v1/health", { cache: "no-store", credentials: "omit" });
    let body: any = null;
    try {
      body = await res.json();
    } catch {
      body = null;
    }
    if (body && body.configured === false) return "unconfigured";
    if (res.status === 404 || res.status === 502 || res.status === 504) return "unreachable";
    if (res.status === 503 && body?.database === "unreachable") return "unreachable";
    return "ready";
  } catch {
    return "unreachable";
  }
}

export function useBackend(): BackendState {
  const { data } = useSWR<BackendState>("datacourt-backend-health", probeBackend, {
    revalidateOnFocus: false,
    dedupingInterval: 60_000,
    shouldRetryOnError: false,
  });
  return data ?? "checking";
}

export const backendDown = (s: BackendState) => s === "unconfigured" || s === "unreachable";
