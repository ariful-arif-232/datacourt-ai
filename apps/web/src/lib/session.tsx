"use client";

import { createContext, type ReactNode, useCallback, useContext, useMemo, useState, useSyncExternalStore } from "react";
import { useApi } from "@/lib/api";

export type Org = { id: string; name: string; slug: string; role: "owner" | "admin" | "reviewer" | "viewer"; is_demo: boolean };
export type Me = { user: { id: string; email: string; name: string; is_demo: boolean }; organizations: Org[] };

type Ctx = {
  me: Me | undefined;
  loading: boolean;
  org: Org | undefined;
  setOrgId: (id: string) => void;
  refresh: () => void;
  canWrite: boolean;
  isAdmin: boolean;
};

const SessionContext = createContext<Ctx | null>(null);
const KEY = "dc.org";

function readStoredOrg(): string | null {
  try {
    return localStorage.getItem(KEY);
  } catch {
    return null; // storage unavailable (private mode, blocked site data)
  }
}

function subscribeStorage(onChange: () => void): () => void {
  window.addEventListener("storage", onChange);
  return () => window.removeEventListener("storage", onChange);
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const { data, isLoading, mutate, error } = useApi<Me>("/auth/me", { shouldRetryOnError: false });
  // The remembered workspace lives in localStorage (a per-browser convenience). Read it as an
  // external store so server and first client render agree (null), then pick it up after hydration.
  const stored = useSyncExternalStore(subscribeStorage, readStoredOrg, () => null);
  const [chosen, setOrgIdState] = useState<string | null>(null);
  const orgId = chosen ?? stored;
  const setOrgId = useCallback((id: string) => {
    setOrgIdState(id);
    try {
      localStorage.setItem(KEY, id);
    } catch {
      /* ignore */
    }
  }, []);
  const org = useMemo(() => {
    const orgs = data?.organizations ?? [];
    return orgs.find((o) => o.id === orgId) ?? orgs.find((o) => !o.is_demo) ?? orgs[0];
  }, [data, orgId]);
  const value: Ctx = {
    me: error ? undefined : data,
    loading: isLoading,
    org,
    setOrgId,
    refresh: () => void mutate(),
    canWrite: !!org && org.role !== "viewer" && !data?.user.is_demo,
    isAdmin: !!org && (org.role === "owner" || org.role === "admin") && !data?.user.is_demo,
  };
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): Ctx {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error("useSession outside SessionProvider");
  return ctx;
}
