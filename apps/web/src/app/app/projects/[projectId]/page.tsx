"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { Boxes, Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { api, useApi } from "@/lib/api";
import { ago, humanize } from "@/lib/format";
import { useSession } from "@/lib/session";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorState,
  inputCls,
  LevelBadge,
  Loading,
  Notice,
  PageHeader,
  PreflightBadge,
  Select,
  StatusBadge,
  Tabs,
} from "@/components/ui";

type Rule = { metric: string; op: string; value: any };

export default function ProjectPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const { data, error, mutate } = useApi<any>(`/projects/${projectId}`, {
    refreshInterval: (d) => (d?.datasets?.some((x: any) => x.latest_audit?.status === "running" || x.latest_audit?.status === "queued") ? 3000 : 0),
  });
  const { canWrite } = useSession();
  const [tab, setTab] = useState<"datasets" | "contract" | "config">("datasets");
  if (error) return <div className="mx-auto max-w-6xl px-4 py-8"><ErrorState error={error} /></div>;
  if (!data) return <div className="mx-auto max-w-6xl px-4 py-8"><Loading /></div>;
  return (
    <div className="mx-auto max-w-6xl px-4 py-8">
      <PageHeader
        eyebrow={<Link href="/app" className="hover:text-ink">Projects</Link>}
        title={data.name}
        description={data.description || "Datasets, data contract and audit configuration for this project."}
        actions={
          canWrite && (
            <Button variant="primary" href={`/app/projects/${projectId}/datasets/new`}>
              <Plus className="size-4" aria-hidden /> Upload dataset
            </Button>
          )
        }
      />
      <Tabs
        value={tab}
        onChange={setTab}
        tabs={[
          { value: "datasets", label: "Datasets", count: data.datasets.length },
          { value: "contract", label: "Data contract & CI" },
          { value: "config", label: "Audit configuration" },
        ]}
      />
      <div className="mt-5">
        {tab === "datasets" && <DatasetList data={data} projectId={projectId} canWrite={canWrite} />}
        {tab === "contract" && <ContractEditor projectId={projectId} contract={data.contract} onSaved={() => mutate()} />}
        {tab === "config" && <ConfigEditor projectId={projectId} />}
      </div>
    </div>
  );
}

function DatasetList({ data, projectId, canWrite }: { data: any; projectId: string; canWrite: boolean }) {
  if (!data.datasets.length)
    return (
      <EmptyState
        title="No datasets yet"
        icon={<Boxes className="size-6" aria-hidden />}
        action={canWrite ? <Button variant="primary" href={`/app/projects/${projectId}/datasets/new`}>Upload a ZIP</Button> : undefined}
      >
        Upload an image-classification archive (train/val/test/class folders or class folders only).
      </EmptyState>
    );
  return (
    <div className="card overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="text-left text-xs text-subtle">
          <tr className="border-b hairline">
            <th className="px-4 py-2.5 font-medium">Dataset</th>
            <th className="px-4 py-2.5 font-medium">Latest version</th>
            <th className="px-4 py-2.5 font-medium">Audit</th>
            <th className="px-4 py-2.5 font-medium">Debt</th>
            <th className="px-4 py-2.5 font-medium">Preflight</th>
            <th className="px-4 py-2.5 font-medium">Cases</th>
          </tr>
        </thead>
        <tbody>
          {data.datasets.map((d: any) => (
            <tr key={d.id} className="border-b hairline last:border-0 hover:bg-surface-2/60">
              <td className="px-4 py-3">
                {d.latest_version ? (
                  <Link href={`/app/datasets/${d.latest_version.id}/overview`} className="font-medium hover:text-accent">
                    {d.name}
                  </Link>
                ) : (
                  <span className="font-medium">{d.name}</span>
                )}
                <div className="text-xs text-subtle">created {ago(d.created_at)}</div>
              </td>
              <td className="px-4 py-3">
                {d.latest_version ? (
                  <span className="flex items-center gap-2">
                    v{d.latest_version.version_number} <StatusBadge status={d.latest_version.status} />
                    <span className="text-xs text-subtle tabular">{d.latest_version.samples?.toLocaleString() ?? ""}</span>
                  </span>
                ) : (
                  "—"
                )}
              </td>
              <td className="px-4 py-3">{d.latest_audit ? <StatusBadge status={d.latest_audit.status} /> : <span className="text-subtle">none</span>}</td>
              <td className="px-4 py-3">{d.latest_audit?.debt ? <LevelBadge level={d.latest_audit.debt} /> : "—"}</td>
              <td className="px-4 py-3">{d.latest_audit?.preflight ? <PreflightBadge status={d.latest_audit.preflight} /> : "—"}</td>
              <td className="px-4 py-3 tabular">{d.latest_audit?.cases ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ContractEditor({ projectId, contract, onSaved }: { projectId: string; contract: any; onSaved: () => void }) {
  const { data: meta } = useApi<any>("/contract-metrics");
  const { isAdmin } = useSession();
  const [edited, setRules] = useState<Rule[] | null>(null);
  const [msg, setMsg] = useState<{ tone: "ok" | "bad"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const rules: Rule[] = edited ?? contract?.rules ?? meta?.default_rules ?? [];
  if (!meta) return <Loading rows={2} />;
  const metrics = Object.entries(meta.metrics as Record<string, string>);
  const save = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const parsed = rules.map((r) => ({
        ...r,
        value: r.op === "includes" ? (Array.isArray(r.value) ? r.value : String(r.value).split(",").map((x) => x.trim()).filter(Boolean)) : r.metric === "debt_level" || r.metric === "preflight_status" ? r.value : Number(r.value),
      }));
      await api(`/projects/${projectId}/contract`, { method: "PUT", json: { name: "Project contract", rules: parsed } });
      setMsg({ tone: "ok", text: "Contract saved as a new version. It is evaluated at the end of every audit and by the CI endpoint." });
      onSaved();
    } catch (e: any) {
      setMsg({ tone: "bad", text: e.message });
    } finally {
      setBusy(false);
    }
  };
  const apiHost = typeof window !== "undefined" ? window.location.origin : "https://app.example.com";
  return (
    <div className="grid gap-5 lg:grid-cols-[1fr_380px]">
      <Card title="Data contract" subtitle={contract ? `Version ${contract.version} · active` : "Using the DataCourt default rules until you save a contract."}>
        <div className="space-y-2">
          {rules.map((r, i) => (
            <div key={i} className="flex flex-wrap items-center gap-2">
              <Select label="Metric" className="min-w-72" value={r.metric} onChange={(v) => setRules(rules.map((x, j) => (j === i ? { ...x, metric: v } : x)))} options={metrics.map(([k, v]) => ({ value: k, label: v }))} />
              <Select label="Operator" value={r.op} onChange={(v) => setRules(rules.map((x, j) => (j === i ? { ...x, op: v } : x)))} options={meta.operators.map((o: string) => ({ value: o, label: o }))} />
              <input aria-label="Value" className={`${inputCls} h-8 w-40 py-1`} value={Array.isArray(r.value) ? r.value.join(", ") : String(r.value)} onChange={(e) => setRules(rules.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)))} />
              {isAdmin && (
                <Button size="sm" variant="ghost" aria-label="Remove rule" onClick={() => setRules(rules.filter((_, j) => j !== i))}>
                  <Trash2 className="size-4" aria-hidden />
                </Button>
              )}
            </div>
          ))}
        </div>
        {isAdmin && (
          <div className="mt-4 flex gap-2">
            <Button size="sm" onClick={() => setRules([...rules, { metric: "min_images_per_class", op: ">=", value: 50 }])}>
              <Plus className="size-4" aria-hidden /> Add rule
            </Button>
            <Button size="sm" variant="primary" onClick={save} loading={busy}>
              Save contract
            </Button>
          </div>
        )}
        {msg && <div className="mt-3"><Notice tone={msg.tone}>{msg.text}</Notice></div>}
      </Card>
      <Card title="CI gate" subtitle="Fail a data pipeline when the contract or preflight fails.">
        <p className="mb-2 text-sm text-muted">Create an API token with <code className="font-mono text-xs">contracts:read</code> in Settings, then:</p>
        <pre className="overflow-x-auto rounded-lg bg-surface-2 p-3 font-mono text-[11px] leading-relaxed">{`curl -s -H "Authorization: Bearer $DATACOURT_TOKEN" \\
  "${apiHost}/api/v1/ci/contract?dataset_version_id=<VERSION_ID>" \\
  | jq -e '.passed'`}</pre>
        <p className="mt-3 text-xs text-subtle">
          Or use the bundled client: <code className="font-mono">datacourt-ci --version-id &lt;id&gt;</code> (exits non-zero on failure). A GitHub Actions example is in the docs.
        </p>
      </Card>
    </div>
  );
}

function ConfigEditor({ projectId }: { projectId: string }) {
  const { data, mutate } = useApi<any>(`/projects/${projectId}/audit-config`);
  const { isAdmin } = useSession();
  const [edited, setText] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ tone: "ok" | "bad"; text: string } | null>(null);
  const text = edited ?? (data ? JSON.stringify(data.overrides, null, 2) : "");
  if (!data) return <Loading rows={2} />;
  const save = async () => {
    setMsg(null);
    try {
      const overrides = JSON.parse(text || "{}");
      const r = await api(`/projects/${projectId}/audit-config`, { method: "PUT", json: { overrides } });
      setMsg({ tone: "ok", text: `Saved as version ${r.version} (config hash ${r.config_hash.slice(0, 12)}…). Applies to new audits.` });
      mutate();
    } catch (e: any) {
      setMsg({ tone: "bad", text: e instanceof SyntaxError ? "Invalid JSON." : e.message });
    }
  };
  return (
    <div className="grid gap-5 lg:grid-cols-2">
      <Card title="Threshold overrides" subtitle={`Version ${data.version} · resolved config hash ${data.config_hash.slice(0, 16)}…`}>
        <p className="mb-2 text-sm text-muted">Every threshold is versioned and stored with each audit. Unknown keys are rejected.</p>
        <textarea aria-label="Audit configuration overrides (JSON)" value={text} onChange={(e) => setText(e.target.value)} rows={14} readOnly={!isAdmin} className={`${inputCls} font-mono text-xs`} />
        {isAdmin && (
          <div className="mt-3">
            <Button variant="primary" size="sm" onClick={save}>
              Save overrides
            </Button>
          </div>
        )}
        {msg && <div className="mt-3"><Notice tone={msg.tone}>{msg.text}</Notice></div>}
      </Card>
      <Card title="Resolved configuration (read-only)">
        <pre className="max-h-[420px] overflow-auto rounded-lg bg-surface-2 p-3 font-mono text-[11px]">{JSON.stringify(data.resolved, null, 2)}</pre>
        <p className="mt-2 text-xs text-subtle">{humanize(data.resolved.config_version)}</p>
        <Badge className="mt-2">defaults shipped with DataCourt</Badge>
      </Card>
    </div>
  );
}
