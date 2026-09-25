"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { type FormEvent, useState } from "react";
import { api } from "@/lib/api";
import { backendDown, useBackend } from "@/lib/backend";
import { Logo } from "@/components/shell";
import { BackendNotice } from "@/components/site";
import { Button, Field, inputCls } from "@/components/ui";

export function AuthForm({ mode }: { mode: "login" | "register" }) {
  const router = useRouter();
  const params = useSearchParams();
  const next = params.get("next") || "/app";
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [demoBusy, setDemoBusy] = useState(false);
  const backend = useBackend();
  const down = backendDown(backend);

  const submit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    setBusy(true);
    setError(null);
    try {
      await api(mode === "login" ? "/auth/login" : "/auth/register", {
        json:
          mode === "login"
            ? { email: f.get("email"), password: f.get("password") }
            : { email: f.get("email"), password: f.get("password"), name: f.get("name"), workspace_name: f.get("workspace") || undefined },
      });
      router.push(next.startsWith("/") ? next : "/app");
    } catch (err: any) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const demo = async () => {
    setDemoBusy(true);
    setError(null);
    try {
      await api("/auth/demo", { method: "POST" });
      router.push("/app");
    } catch (err: any) {
      setError(err.message);
    } finally {
      setDemoBusy(false);
    }
  };

  return (
    <main id="main" className="flex min-h-dvh items-center justify-center px-4 py-12">
      <div className="w-full max-w-sm">
        <Link href="/" className="mb-8 inline-block" aria-label="DataCourt home">
          <Logo />
        </Link>
        <h1 className="font-display text-3xl">{mode === "login" ? "Welcome back" : "Create your workspace"}</h1>
        <p className="mt-1 text-sm text-muted">
          {mode === "login" ? "Sign in to continue auditing." : "Datasets are private to your workspace by default."}
        </p>
        {down && (
          <div role="status" className="mt-6 rounded-lg border border-border bg-surface-2 p-3 text-sm text-muted">
            Sign-in is unavailable right now. <BackendNotice state={backend} />
          </div>
        )}
        <form onSubmit={submit} className="mt-6 space-y-4">
          {mode === "register" && (
            <Field label="Your name">
              <input name="name" required autoComplete="name" className={inputCls} />
            </Field>
          )}
          <Field label="Email">
            <input name="email" type="email" required autoComplete="email" className={inputCls} />
          </Field>
          <Field label="Password" hint={mode === "register" ? "At least 10 characters." : undefined}>
            <input name="password" type="password" required minLength={mode === "register" ? 10 : 1} autoComplete={mode === "login" ? "current-password" : "new-password"} className={inputCls} />
          </Field>
          {mode === "register" && (
            <Field label="Workspace name (optional)">
              <input name="workspace" className={inputCls} placeholder="e.g. Plant Pathology Lab" />
            </Field>
          )}
          {error && (
            <p role="alert" className="text-sm text-bad">
              {error}
            </p>
          )}
          <Button variant="primary" className="w-full" loading={busy} type="submit" disabled={down}>
            {mode === "login" ? "Sign in" : "Create account"}
          </Button>
        </form>
        <div className="my-6 flex items-center gap-3 text-xs text-subtle">
          <span className="h-px flex-1 bg-border" /> or <span className="h-px flex-1 bg-border" />
        </div>
        <Button className="w-full" onClick={demo} loading={demoBusy} disabled={down}>
          Explore the live demo (read-only)
        </Button>
        <p className="mt-6 text-center text-sm text-muted">
          {mode === "login" ? (
            <>
              New here?{" "}
              <Link href="/register" className="text-accent underline-offset-2 hover:underline">
                Create an account
              </Link>
            </>
          ) : (
            <>
              Already have an account?{" "}
              <Link href="/login" className="text-accent underline-offset-2 hover:underline">
                Sign in
              </Link>
            </>
          )}
        </p>
      </div>
    </main>
  );
}
