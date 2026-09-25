"use client";

import useSWR, { mutate, type SWRConfiguration } from "swr";

export const API_BASE = "/api/v1";

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, message: string, detail?: unknown) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

async function parse(res: Response): Promise<unknown> {
  const text = await res.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

export async function api<T = any>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const { json, headers, ...rest } = init;
  const method = (rest.method || (json !== undefined ? "POST" : "GET")).toUpperCase();
  const h: Record<string, string> = { ...(headers as Record<string, string>) };
  if (json !== undefined) h["content-type"] = "application/json";
  // Custom header required by the API for cookie-authenticated state changes (CSRF defense).
  if (method !== "GET") h["x-datacourt-csrf"] = "1";
  const res = await fetch(path.startsWith("http") ? path : `${API_BASE}${path}`, {
    ...rest,
    method,
    headers: h,
    credentials: "include",
    body: json !== undefined ? JSON.stringify(json) : rest.body,
  });
  const body = await parse(res);
  if (!res.ok) {
    const detail = (body as any)?.detail;
    const message =
      typeof detail === "string"
        ? detail
        : res.status === 422
          ? "Some fields are invalid."
          : res.status >= 502 && res.status <= 504
            ? "The DataCourt API is not reachable right now. Please try again shortly."
            : `Request failed (${res.status})`;
    throw new ApiError(res.status, message, body);
  }
  return body as T;
}

export function useApi<T = any>(path: string | null, config?: SWRConfiguration<T, ApiError>) {
  return useSWR<T, ApiError>(path, (p: string) => api<T>(p), { revalidateOnFocus: false, ...config });
}

export function qs(params: Record<string, string | number | boolean | null | undefined | string[]>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === null || v === undefined || v === "") continue;
    if (Array.isArray(v)) v.forEach((x) => sp.append(k, x));
    else sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}

/** Drops every cached API response (after sign-out or deleting a workspace/account). */
export async function clearApiCache(): Promise<void> {
  await mutate(() => true, undefined, { revalidate: false });
}
