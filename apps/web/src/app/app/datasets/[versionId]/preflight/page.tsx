"use client";

import Link from "next/link";
import { type FormEvent, useState } from "react";
import { Ban, CheckCircle2, TriangleAlert } from "lucide-react";
import { RequireAudit } from "@/components/audit-progress";
import { Button, Card, Field, InfoTip, inputCls, Loading, Notice, PageHeader, PreflightBadge, Select, StatusBadge } from "@/components/ui";
import { api } from "@/lib/api";
import { useDataset } from "@/lib/dataset";
import { ago } from "@/lib/format";
import { useVersionApi } from "@/lib/hooks";
import { useSession } from "@/lib/session";

function CheckIcon({ status }: { status: string }) {
  if (status === "pass") return <CheckCircle2 className="size-4 text-ok" aria-label="pass" />;
  if (status === "warn") return <TriangleAlert className="size-4 text-warn" aria-label="warning" />;
  return <Ban className="size-4 text-bad" aria-label="blocking" />;
}

export default function PreflightPage() {
  const { version, versionId } = useDataset();
  const { isAdmin } = useSession();
  const { data, mutate } = useVersionApi<any>("/preflight");
  const [status, setStatus] = useState("READY_WITH_WARNINGS");
  const [msg, setMsg] = useState<{ tone: "ok" | "bad"; text: string } | null>(null);
  const override = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const reason = String(new FormData(e.currentTarget).get("reason") ?? "");
    try {
      await api(`/versions/${versionId}/preflight/override`, { json: { status, reason } });
      setMsg({ tone: "ok", text: "Override recorded in the evidence ledger." });
      mutate();
    } catch (err: any) {
      setMsg({ tone: "bad", text: err.message });
    }
  };
  return (
    <RequireAudit>
      <PageHeader
        title="Training preflight gate"
        description="A versioned, rule-based answer to “is this dataset safe and ready enough to train on?” Blocking conditions stop known-bad data; warnings flag what deserves attention."
      />
      {!data ? (
        <Loading />
      ) : (
        <div className="space-y-5">
          <Card>
            <div className="flex flex-wrap items-center gap-6">
              <div>
                <div className="flex items-center gap-1 text-xs text-muted">
                  Effective status <InfoTip term="preflight" />
                </div>
                <div className="mt-1 origin-left scale-125">
                  <PreflightBadge status={data.effective_status} />
                </div>
              </div>
              <div className="text-sm text-muted">
                Live rules: <PreflightBadge status={data.live.status} /> · {data.live.rules_version}
              </div>
              {data.stored?.override_status && (
                <Notice tone="warn" title={`Overridden to ${data.stored.override_status} ${ago(data.stored.override_at)}`}>
                  {data.stored.override_reason}
                </Notice>
              )}
            </div>
          </Card>
          <Card title="Checks">
            <ul className="divide-y divide-border">
              {data.live.checks.map((c: any) => (
                <li key={c.id} className="flex items-start gap-3 py-2.5">
                  <CheckIcon status={c.status} />
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2 text-sm font-medium">
                      {c.title} <StatusBadge status={c.status} />
                    </div>
                    <p className="text-xs text-muted">{c.message}</p>
                  </div>
                  {c.threshold !== null && c.threshold !== undefined && !Array.isArray(c.threshold) && (
                    <span className="shrink-0 text-xs tabular text-subtle">
                      measured {String(c.measured)} · limit {String(c.threshold)}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </Card>
          {data.contract && (
            <Card title={`Data contract · ${data.contract.name} v${data.contract.version}`} actions={<StatusBadge status={data.contract.passed ? "pass" : "failed"} />}>
              <ul className="divide-y divide-border text-sm">
                {data.contract.results.map((r: any) => (
                  <li key={r.metric} className="flex items-center gap-3 py-2">
                    <CheckIcon status={r.passed ? "pass" : "block"} />
                    <span className="flex-1">{r.title}</span>
                    <span className="font-mono text-xs text-muted">
                      {JSON.stringify(r.actual)} {r.op} {JSON.stringify(r.expected)}
                    </span>
                  </li>
                ))}
              </ul>
              {version?.project && (
                <p className="mt-3 text-xs text-subtle">
                  Edit the contract in the{" "}
                  <Link href={`/app/projects/${version.project.id}`} className="underline">
                    project settings
                  </Link>
                  . CI pipelines can query <code className="font-mono">GET /api/v1/ci/contract?dataset_version_id={versionId}</code>.
                </p>
              )}
            </Card>
          )}
          {isAdmin && data.live.status !== "READY" && (
            <Card title="Authorised override" subtitle="Owners and admins may accept a warning or block with a written reason. The override is recorded in the hash-chained evidence ledger.">
              <form onSubmit={override} className="space-y-3">
                <Select label="Override status" value={status} onChange={setStatus} options={[{ value: "READY_WITH_WARNINGS", label: "Ready with warnings" }, { value: "READY", label: "Ready" }]} />
                <Field label="Reason (min. 10 characters)">
                  <textarea name="reason" required minLength={10} rows={2} className={inputCls} />
                </Field>
                <Button type="submit" variant="danger" size="sm">
                  Record override
                </Button>
              </form>
              {msg && <div className="mt-3"><Notice tone={msg.tone}>{msg.text}</Notice></div>}
            </Card>
          )}
        </div>
      )}
    </RequireAudit>
  );
}
