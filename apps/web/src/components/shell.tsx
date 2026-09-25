"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Command } from "cmdk";
import {
  BookOpen,
  Boxes,
  ChevronDown,
  Cpu,
  FileClock,
  LogOut,
  Search,
  Settings,
} from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";
import { api, clearApiCache, useApi } from "@/lib/api";
import { useSession } from "@/lib/session";
import { Badge, Button, cx } from "@/components/ui";

export function Logo({ compact = false }: { compact?: boolean }) {
  return (
    <span className="inline-flex items-center gap-2">
      <svg viewBox="0 0 32 32" className="size-6" aria-hidden>
        <rect width="32" height="32" rx="7" fill="var(--accent)" />
        <path d="M16 6v20M9 11h14M9 11l-3.5 7h7zM23 11l-3.5 7h7zM11 26h10" stroke="var(--accent-ink)" strokeWidth="1.8" fill="none" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      {!compact && (
        <span className="font-display text-[17px] font-semibold tracking-tight">
          DataCourt<span className="text-subtle"> AI</span>
        </span>
      )}
    </span>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const { me, loading, org, setOrgId } = useSession();
  const router = useRouter();
  const [paletteOpen, setPaletteOpen] = useState(false);

  useEffect(() => {
    if (!loading && !me) router.replace(`/login?next=${encodeURIComponent(window.location.pathname)}`);
  }, [loading, me, router]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((o) => !o);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  if (!me) {
    return (
      <div className="flex min-h-dvh items-center justify-center text-sm text-muted" role="status">
        Loading workspace…
      </div>
    );
  }

  const logout = async () => {
    await api("/auth/logout", { method: "POST" });
    await clearApiCache();
    router.replace("/");
  };

  return (
    <div className="min-h-dvh">
      {me.user.is_demo && (
        <div className="bg-accent px-4 py-1.5 text-center text-xs text-accent-ink">
          You are exploring a read-only demo workspace with real pipeline outputs.{" "}
          <Link href="/register" className="underline underline-offset-2">
            Create an account
          </Link>{" "}
          to audit your own data.
        </div>
      )}
      <header className="sticky top-0 z-40 border-b hairline bg-bg/85 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-[1500px] items-center gap-3 px-4">
          <Link href="/app" aria-label="DataCourt workspace home">
            <Logo />
          </Link>
          <span className="text-border-strong" aria-hidden>
            /
          </span>
          <label className="relative flex items-center">
            <span className="sr-only">Workspace</span>
            <select
              value={org?.id}
              onChange={(e) => {
                setOrgId(e.target.value);
                router.push("/app");
              }}
              className="h-8 appearance-none rounded-lg border border-transparent bg-transparent pl-2 pr-7 text-sm font-medium hover:border-border"
            >
              {me.organizations.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.name}
                  {o.is_demo ? " (demo)" : ""}
                </option>
              ))}
            </select>
            <ChevronDown className="pointer-events-none absolute right-2 size-3.5 text-subtle" aria-hidden />
          </label>
          {org && <Badge>{org.role}</Badge>}
          <div className="flex-1" />
          <button
            onClick={() => setPaletteOpen(true)}
            className="hidden h-8 items-center gap-2 rounded-lg border border-border bg-surface px-2.5 text-[13px] text-subtle hover:text-ink sm:flex"
            aria-label="Open command palette"
          >
            <Search className="size-3.5" aria-hidden />
            Search or jump to…
            <kbd className="ml-4 rounded border border-border px-1 font-mono text-[10px]">⌘K</kbd>
          </button>
          <nav className="flex items-center gap-0.5" aria-label="Workspace">
            <NavIcon href="/app/ledger" label="Evidence ledger" icon={<FileClock className="size-4" />} />
            <NavIcon href="/app/models" label="Models & algorithms" icon={<Cpu className="size-4" />} />
            <NavIcon href="/docs" label="Methodology & docs" icon={<BookOpen className="size-4" />} />
            <NavIcon href="/app/settings" label="Settings" icon={<Settings className="size-4" />} />
            <button onClick={logout} className="rounded-lg p-2 text-muted hover:bg-surface-2 hover:text-ink" aria-label="Sign out" title="Sign out">
              <LogOut className="size-4" aria-hidden />
            </button>
          </nav>
        </div>
      </header>
      <CommandPalette open={paletteOpen} onOpenChange={setPaletteOpen} />
      <div id="main">{children}</div>
    </div>
  );
}

function NavIcon({ href, label, icon }: { href: string; label: string; icon: ReactNode }) {
  const path = usePathname();
  return (
    <Link href={href} title={label} aria-label={label} className={cx("rounded-lg p-2 hover:bg-surface-2", path.startsWith(href) ? "text-ink" : "text-muted")}>
      {icon}
    </Link>
  );
}

/* ---------------------------------------------------------------- command palette */

const DATASET_PAGES: [string, string][] = [
  ["overview", "Overview"],
  ["samples", "Sample explorer"],
  ["quality", "Visual quality"],
  ["duplicates", "Duplicate families"],
  ["lineage", "Lineage graph"],
  ["leakage", "Train/val/test leakage"],
  ["labels", "Label forensics"],
  ["rare-or-wrong", "Rare or wrong?"],
  ["shortcuts", "Shortcut detective"],
  ["coverage", "Coverage map & collection planner"],
  ["cartography", "Training dynamics"],
  ["influence", "Model influence"],
  ["failures", "Blame map & failure replay"],
  ["court", "Court cases"],
  ["review", "Review & budget optimizer"],
  ["what-if", "What-if lab"],
  ["debt", "Dataset debt"],
  ["preflight", "Preflight & data contract"],
  ["versions", "Versions, DNA & drift"],
  ["privacy", "Privacy scan"],
  ["export", "Clean export"],
  ["report", "Audit report"],
];

function CommandPalette({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const router = useRouter();
  const path = usePathname();
  const { org } = useSession();
  const [query, setQuery] = useState("");
  const versionId = path.match(/\/app\/datasets\/([0-9a-f-]{36})/)?.[1];
  const { data: projects } = useApi<any[]>(open && org ? `/orgs/${org.id}/projects` : null);
  const { data: versions } = useApi<any[]>(open && org ? `/orgs/${org.id}/versions` : null);

  const go = (href: string) => {
    onOpenChange(false);
    setQuery("");
    router.push(href);
  };

  const jumpToCase = async () => {
    const n = query.replace(/[^0-9]/g, "");
    if (!versionId || !n) return;
    const res = await api(`/versions/${versionId}/cases?q=${n}&limit=1`).catch(() => null);
    if (res?.items?.[0]) go(`/app/datasets/${versionId}/court/${res.items[0].id}`);
  };

  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/30 p-4 pt-[12vh]" onClick={() => onOpenChange(false)}>
      <Command
        label="Command palette"
        className="card w-full max-w-xl overflow-hidden"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === "Escape") onOpenChange(false);
        }}
      >
        <div className="flex items-center gap-2 border-b hairline px-3">
          <Search className="size-4 text-subtle" aria-hidden />
          <Command.Input
            autoFocus
            value={query}
            onValueChange={setQuery}
            placeholder={versionId ? "Jump to a page, case #, sample name or command…" : "Jump to a project, dataset or page…"}
            className="h-11 w-full bg-transparent text-sm outline-none placeholder:text-subtle"
          />
        </div>
        <Command.List className="max-h-[55vh] overflow-y-auto p-1.5 text-sm">
          <Command.Empty className="px-3 py-6 text-center text-muted">No matches.</Command.Empty>
          {versionId && /\d/.test(query) && (
            <Command.Group heading="Cases" className="px-1 py-1 text-xs text-subtle [&_[cmdk-group-items]]:text-sm [&_[cmdk-group-items]]:text-ink">
              <Item onSelect={jumpToCase}>Open case #{query.replace(/[^0-9]/g, "")}</Item>
            </Command.Group>
          )}
          {versionId && query.length > 1 && (
            <Command.Group heading="Search" className="px-1 py-1 text-xs text-subtle [&_[cmdk-group-items]]:text-sm [&_[cmdk-group-items]]:text-ink">
              <Item onSelect={() => go(`/app/datasets/${versionId}/samples?q=${encodeURIComponent(query)}`)}>Search samples for “{query}”</Item>
              <Item onSelect={() => go(`/app/datasets/${versionId}/court?q=${encodeURIComponent(query)}`)}>Search cases for “{query}”</Item>
            </Command.Group>
          )}
          {versionId && (
            <Command.Group heading="This dataset" className="px-1 py-1 text-xs text-subtle [&_[cmdk-group-items]]:text-sm [&_[cmdk-group-items]]:text-ink">
              <Item onSelect={() => go(`/app/datasets/${versionId}/review`)}>Start a review session</Item>
              <Item onSelect={() => go(`/app/datasets/${versionId}/what-if`)}>Launch a what-if experiment</Item>
              <Item onSelect={() => go(`/app/datasets/${versionId}/versions`)}>Compare versions</Item>
              <Item onSelect={() => go(`/app/datasets/${versionId}/court?verdict=POSSIBLE_RELABEL`)}>Filter: possible relabels</Item>
              <Item onSelect={() => go(`/app/datasets/${versionId}/court?verdict=LEAKAGE_ACTION_NEEDED`)}>Filter: leakage cases</Item>
              {DATASET_PAGES.map(([slug, label]) => (
                <Item key={slug} onSelect={() => go(`/app/datasets/${versionId}/${slug}`)}>
                  {label}
                </Item>
              ))}
            </Command.Group>
          )}
          <Command.Group heading="Datasets" className="px-1 py-1 text-xs text-subtle [&_[cmdk-group-items]]:text-sm [&_[cmdk-group-items]]:text-ink">
            {(versions ?? []).map((v) => (
              <Item key={v.id} onSelect={() => go(`/app/datasets/${v.id}/overview`)}>
                <Boxes className="size-3.5 text-subtle" aria-hidden /> {v.dataset} v{v.version_number}
              </Item>
            ))}
          </Command.Group>
          <Command.Group heading="Projects" className="px-1 py-1 text-xs text-subtle [&_[cmdk-group-items]]:text-sm [&_[cmdk-group-items]]:text-ink">
            {(projects ?? []).map((p) => (
              <Item key={p.id} onSelect={() => go(`/app/projects/${p.id}`)}>
                {p.name}
              </Item>
            ))}
          </Command.Group>
          <Command.Group heading="Navigate" className="px-1 py-1 text-xs text-subtle [&_[cmdk-group-items]]:text-sm [&_[cmdk-group-items]]:text-ink">
            <Item onSelect={() => go("/app")}>Workspace home</Item>
            <Item onSelect={() => go("/app/ledger")}>Evidence ledger</Item>
            <Item onSelect={() => go("/app/settings")}>Settings</Item>
            <Item onSelect={() => go("/docs/methodology")}>Methodology</Item>
          </Command.Group>
        </Command.List>
        <div className="flex justify-between border-t hairline px-3 py-1.5 text-[11px] text-subtle">
          <span>↑↓ navigate · ↵ open · esc close</span>
          <Button size="sm" variant="ghost" onClick={() => onOpenChange(false)}>
            Close
          </Button>
        </div>
      </Command>
    </div>
  );
}

function Item({ children, onSelect }: { children: ReactNode; onSelect: () => void }) {
  return (
    <Command.Item onSelect={onSelect} className="flex cursor-pointer items-center gap-2 rounded-md px-2.5 py-1.5 data-[selected=true]:bg-surface-2">
      {children}
    </Command.Item>
  );
}
