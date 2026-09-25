"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Logo } from "@/components/shell";
import { Button } from "@/components/ui";
import { api } from "@/lib/api";
import { type BackendState, backendDown, useBackend } from "@/lib/backend";

export const GITHUB_URL = process.env.NEXT_PUBLIC_GITHUB_URL || "https://github.com/ariful-arif-232/datacourt-ai";

export function SiteHeader() {
  return (
    <header className="sticky top-0 z-30 border-b hairline bg-bg/85 backdrop-blur">
      <div className="mx-auto flex h-14 max-w-6xl items-center gap-6 px-4">
        <Link href="/" aria-label="DataCourt AI home">
          <Logo />
        </Link>
        <nav className="hidden items-center gap-5 text-sm text-muted md:flex" aria-label="Site">
          <Link href="/#workflow" className="hover:text-ink">
            Workflow
          </Link>
          <Link href="/docs/methodology" className="hover:text-ink">
            Methodology
          </Link>
          <Link href="/docs/evaluation" className="hover:text-ink">
            Evaluation
          </Link>
          <Link href="/docs/security" className="hover:text-ink">
            Security
          </Link>
          <Link href="/docs" className="hover:text-ink">
            Docs
          </Link>
        </nav>
        <div className="flex-1" />
        <Link href="/login" className="text-sm text-muted hover:text-ink">
          Sign in
        </Link>
        <Button href="/register" variant="primary" size="sm">
          Audit a dataset
        </Button>
      </div>
    </header>
  );
}

export function SiteFooter() {
  return (
    <footer className="border-t hairline">
      <div className="mx-auto grid max-w-6xl gap-8 px-4 py-10 text-sm md:grid-cols-4">
        <div className="md:col-span-2">
          <Logo />
          <p className="mt-2 max-w-sm text-muted">Evidence-driven dataset forensics for image classification. Put your dataset on trial before your model pays the price.</p>
          <p className="mt-3 text-xs text-subtle">
            DataCourt reports are technical audit aids, not legal or regulatory certifications. Nothing is deleted or relabelled without a human decision.
          </p>
        </div>
        <div>
          <div className="mb-2 text-xs font-medium uppercase tracking-wide text-subtle">Product</div>
          <ul className="space-y-1.5 text-muted">
            <li>
              <Link href="/register" className="hover:text-ink">
                Create a workspace
              </Link>
            </li>
            <li>
              <Link href="/login" className="hover:text-ink">
                Sign in / live demo
              </Link>
            </li>
            <li>
              <a href={GITHUB_URL} className="hover:text-ink" rel="noopener noreferrer" target="_blank">
                Source code
              </a>
            </li>
          </ul>
        </div>
        <div>
          <div className="mb-2 text-xs font-medium uppercase tracking-wide text-subtle">Documentation</div>
          <ul className="space-y-1.5 text-muted">
            <li>
              <Link href="/docs/methodology" className="hover:text-ink">
                Methodology
              </Link>
            </li>
            <li>
              <Link href="/docs/evaluation" className="hover:text-ink">
                Evaluation
              </Link>
            </li>
            <li>
              <Link href="/docs/security" className="hover:text-ink">
                Security & privacy
              </Link>
            </li>
            <li>
              <Link href="/docs/dataset-formats" className="hover:text-ink">
                Dataset formats
              </Link>
            </li>
            <li>
              <Link href="/docs/api" className="hover:text-ink">
                CI & API
              </Link>
            </li>
          </ul>
        </div>
      </div>
    </footer>
  );
}

export function BackendNotice({ state }: { state: BackendState }) {
  return (
    <span className="max-w-md text-xs text-muted">
      {state === "unconfigured"
        ? "The DataCourt API on this deployment is not configured yet (database, storage and secrets are set in the hosting environment). "
        : "The DataCourt API is not reachable right now; please try again shortly. "}
      The{" "}
      <Link href="/docs" className="underline">
        documentation
      </Link>{" "}
      is available, and the{" "}
      <a href={GITHUB_URL} className="underline" target="_blank" rel="noopener noreferrer">
        README
      </a>{" "}
      explains how to run DataCourt yourself.
    </span>
  );
}

export function DemoButton({ className }: { className?: string }) {
  const router = useRouter();
  const backend = useBackend();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const go = async () => {
    setBusy(true);
    setError(null);
    try {
      await api("/auth/demo", { method: "POST" });
      router.push("/app");
    } catch (e: any) {
      setError(e.status === 404 ? "The demo workspace is not available on this deployment." : e.message);
      setBusy(false);
    }
  };
  if (backendDown(backend))
    return (
      <span className={className}>
        <BackendNotice state={backend} />
      </span>
    );
  return (
    <span className={className}>
      <Button onClick={go} loading={busy}>
        Explore the read-only demo
      </Button>
      {error && <span className="ml-3 text-xs text-bad">{error}</span>}
    </span>
  );
}
