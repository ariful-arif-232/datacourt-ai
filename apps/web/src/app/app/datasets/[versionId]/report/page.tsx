"use client";

import { ExternalLink, FileJson, FileText } from "lucide-react";
import { useState } from "react";
import { RequireAudit } from "@/components/audit-progress";
import { Badge, Button, Card, EmptyState, Loading, Notice, PageHeader, StatusBadge } from "@/components/ui";
import { api, useApi } from "@/lib/api";
import { useDataset } from "@/lib/dataset";
import { ago } from "@/lib/format";
import { useVersionApi } from "@/lib/hooks";
import { useSession } from "@/lib/session";

function ReportRow({ report }: { report: any }) {
  const { data } = useApi<any>(`/reports/${report.id}`, { refreshInterval: (d) => (d && ["queued", "running"].includes(d.status) ? 2500 : 0) });
  const r = data ?? report;
  return (
    <li className="flex flex-wrap items-center gap-2 py-3 text-sm">
      <FileText className="size-4 text-subtle" aria-hidden />
      <span className="font-medium">DataCourt Audit Report</span>
      <StatusBadge status={r.status} />
      <span className="text-xs text-subtle">{ago(r.created_at)}</span>
      <div className="flex-1" />
      {r.status === "completed" && data?.html_url && (
        <>
          <Button size="sm" onClick={() => window.open(data.html_url, "_blank", "noopener")}>
            <ExternalLink className="size-4" aria-hidden /> View
          </Button>
          <Button size="sm" variant="ghost" onClick={() => window.location.assign(data.html_download_url)}>
            HTML
          </Button>
          <Button size="sm" variant="ghost" onClick={() => window.location.assign(data.json_url)}>
            <FileJson className="size-4" aria-hidden /> JSON
          </Button>
        </>
      )}
      {r.expired && <Badge>files expired (retention)</Badge>}
      {r.error && <Notice tone="bad">{r.error}</Notice>}
    </li>
  );
}

export default function ReportPage() {
  const { versionId } = useDataset();
  const { canWrite } = useSession();
  const { data: reports, mutate } = useVersionApi<any[]>("/reports");
  const [err, setErr] = useState<string | null>(null);
  const create = async () => {
    setErr(null);
    try {
      await api("/reports", { json: { dataset_version_id: versionId } });
      mutate();
    } catch (e: any) {
      setErr(e.message);
    }
  };
  return (
    <RequireAudit>
      <PageHeader
        title="DataCourt Audit Report"
        description="A self-contained training-readiness report: dataset metadata and hashes, methodology and algorithm versions, findings, Dataset Debt, human decisions, what-if results, preflight and limitations. It is a technical audit aid — not a legal or compliance certification."
        actions={
          canWrite && (
            <Button variant="primary" onClick={create}>
              Generate report
            </Button>
          )
        }
      />
      {err && <Notice tone="bad">{err}</Notice>}
      <Card title="Reports">
        {!reports ? (
          <Loading rows={1} />
        ) : reports.length === 0 ? (
          <EmptyState title="No reports yet">Generate one after reviewing — it captures the current review state and experiment results. Print the HTML view to PDF from your browser.</EmptyState>
        ) : (
          <ul className="divide-y divide-border">
            {reports.map((r) => (
              <ReportRow key={r.id} report={r} />
            ))}
          </ul>
        )}
      </Card>
    </RequireAudit>
  );
}
