"use client";

import Link from "next/link";
import { useParams, usePathname, useRouter } from "next/navigation";
import {
  Activity,
  BadgeCheck,
  Boxes,
  ChartScatter,
  CopyCheck,
  Download,
  FileText,
  FlaskConical,
  Gavel,
  Gem,
  GitCompare,
  Image as ImageIcon,
  LayoutDashboard,
  ListChecks,
  Network,
  Radar,
  Scale,
  ScanEye,
  ShieldAlert,
  Sparkles,
  Tags,
  TrendingDown,
  Waypoints,
} from "lucide-react";
import type { ReactNode } from "react";
import { AuditProgress } from "@/components/audit-progress";
import { Badge, cx, StatusBadge } from "@/components/ui";
import { DatasetProvider, useDataset } from "@/lib/dataset";

const NAV: { group: string; items: [string, string, ReactNode][] }[] = [
  {
    group: "Dataset",
    items: [
      ["overview", "Overview", <LayoutDashboard key="i" className="size-4" />],
      ["samples", "Samples", <ImageIcon key="i" className="size-4" />],
    ],
  },
  {
    group: "Find",
    items: [
      ["quality", "Visual quality", <ScanEye key="i" className="size-4" />],
      ["duplicates", "Duplicates", <CopyCheck key="i" className="size-4" />],
      ["lineage", "Lineage graph", <Network key="i" className="size-4" />],
      ["leakage", "Leakage", <ShieldAlert key="i" className="size-4" />],
      ["labels", "Label forensics", <Tags key="i" className="size-4" />],
      ["rare-or-wrong", "Rare or wrong?", <Gem key="i" className="size-4" />],
      ["shortcuts", "Shortcuts", <Sparkles key="i" className="size-4" />],
      ["coverage", "Coverage & collection", <Radar key="i" className="size-4" />],
    ],
  },
  {
    group: "Explain",
    items: [
      ["cartography", "Training dynamics", <Activity key="i" className="size-4" />],
      ["influence", "Model influence", <ChartScatter key="i" className="size-4" />],
      ["failures", "Blame map & replay", <Waypoints key="i" className="size-4" />],
    ],
  },
  {
    group: "Decide",
    items: [
      ["court", "Court cases", <Gavel key="i" className="size-4" />],
      ["review", "Review & budget", <ListChecks key="i" className="size-4" />],
      ["what-if", "What-if lab", <FlaskConical key="i" className="size-4" />],
    ],
  },
  {
    group: "Govern",
    items: [
      ["debt", "Dataset debt", <TrendingDown key="i" className="size-4" />],
      ["preflight", "Preflight & contract", <BadgeCheck key="i" className="size-4" />],
      ["versions", "Versions & DNA", <GitCompare key="i" className="size-4" />],
      ["privacy", "Privacy scan", <Scale key="i" className="size-4" />],
    ],
  },
  {
    group: "Deliver",
    items: [
      ["export", "Clean export", <Download key="i" className="size-4" />],
      ["report", "Audit report", <FileText key="i" className="size-4" />],
    ],
  },
];

function Sidebar() {
  const { versionId, version } = useDataset();
  const path = usePathname();
  const router = useRouter();
  return (
    <aside className="lg:sticky lg:top-14 lg:h-[calc(100dvh-3.5rem)] lg:w-60 lg:shrink-0 lg:overflow-y-auto lg:border-r hairline">
      <div className="space-y-3 border-b hairline p-4">
        <div className="min-w-0">
          {version?.project && (
            <Link href={`/app/projects/${version.project.id}`} className="block truncate text-xs text-subtle hover:text-ink">
              {version.project.name}
            </Link>
          )}
          <div className="flex items-center gap-2">
            <Boxes className="size-4 shrink-0 text-subtle" aria-hidden />
            <span className="truncate font-semibold" title={version?.dataset.name}>
              {version?.dataset.name ?? "…"}
            </span>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <label className="sr-only" htmlFor="version-select">
            Version
          </label>
          <select
            id="version-select"
            value={versionId}
            onChange={(e) => router.push(path.replace(versionId, e.target.value))}
            className="h-7 rounded-md border border-border bg-surface px-1.5 text-xs"
          >
            {(version?.versions ?? []).map((v) => (
              <option key={v.id} value={v.id}>
                v{v.version_number}
                {v.origin === "export" ? " (export)" : ""}
              </option>
            ))}
          </select>
          {version && <StatusBadge status={version.status} />}
        </div>
      </div>
      <nav aria-label="Dataset" className="flex gap-4 overflow-x-auto p-2 lg:block lg:space-y-4 lg:p-3">
        {NAV.map((g) => (
          <div key={g.group} className="shrink-0">
            <div className="px-2 pb-1 text-[10.5px] font-semibold uppercase tracking-wider text-subtle">{g.group}</div>
            <ul className="flex gap-0.5 lg:block lg:space-y-0.5">
              {g.items.map(([slug, label, icon]) => {
                const href = `/app/datasets/${versionId}/${slug}`;
                const active = path === href || path.startsWith(`${href}/`);
                return (
                  <li key={slug}>
                    <Link
                      href={href}
                      aria-current={active ? "page" : undefined}
                      className={cx(
                        "flex items-center gap-2 whitespace-nowrap rounded-md px-2 py-1.5 text-[13px]",
                        active ? "bg-surface font-medium text-ink shadow-sm ring-1 ring-border" : "text-muted hover:bg-surface-2 hover:text-ink",
                      )}
                    >
                      <span className={active ? "text-accent" : "text-subtle"} aria-hidden>
                        {icon}
                      </span>
                      {label}
                    </Link>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
      </nav>
    </aside>
  );
}

function Frame({ children }: { children: ReactNode }) {
  const { version, busy } = useDataset();
  const latest = version?.audits[0];
  return (
    <div className="mx-auto flex max-w-[1500px] flex-col lg:flex-row">
      <Sidebar />
      <main className="min-w-0 flex-1 px-4 py-6 lg:px-8">
        {busy && version && <AuditProgress version={version} />}
        {latest?.status === "failed" && (
          <div className="mb-4">
            <Badge tone="bad">Latest audit failed</Badge>
          </div>
        )}
        {children}
      </main>
    </div>
  );
}

export default function DatasetLayout({ children }: { children: ReactNode }) {
  const { versionId } = useParams<{ versionId: string }>();
  return (
    <DatasetProvider versionId={versionId}>
      <Frame>{children}</Frame>
    </DatasetProvider>
  );
}
