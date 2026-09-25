"use client";

import { RequireAudit } from "@/components/audit-progress";
import { Badge, Card, EmptyState, KindTag, Loading, Notice, PageHeader, SampleThumb } from "@/components/ui";
import { humanize } from "@/lib/format";
import { useVersionApi } from "@/lib/hooks";

export default function PrivacyPage() {
  const { data } = useVersionApi<any>("/privacy");
  return (
    <RequireAudit>
      <PageHeader
        title="Privacy contamination scan"
        description="Detector-based flags for potential faces and readable text regions. No identity recognition is ever performed. The scan is configurable per workspace because many datasets legitimately contain people."
      />
      {!data ? (
        <Loading />
      ) : !data.enabled ? (
        <EmptyState title="Privacy scan disabled for this audit">Enable it in workspace settings and re-run the audit.</EmptyState>
      ) : (
        <div className="space-y-4">
          <Notice tone="info">{data.note}</Notice>
          {data.items.length === 0 ? (
            <EmptyState title="No potential privacy-sensitive content detected" />
          ) : (
            <Card title={`${data.items.length} flagged samples`} subtitle={`Algorithm ${data.algorithm}`}>
              <ul className="grid grid-cols-[repeat(auto-fill,minmax(140px,1fr))] gap-4">
                {data.items.map((f: any, i: number) => (
                  <li key={i}>
                    <SampleThumb s={f.sample} size={140} />
                    <div className="mt-1 flex items-center gap-1">
                      <Badge tone="warn">
                        {humanize(f.kind)} · {f.count}
                      </Badge>
                      <KindTag kind="heuristic" />
                    </div>
                  </li>
                ))}
              </ul>
            </Card>
          )}
        </div>
      )}
    </RequireAudit>
  );
}
