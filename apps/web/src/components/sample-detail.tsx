"use client";

import Link from "next/link";
import { useApi } from "@/lib/api";
import { bytes, fixed, humanize, pct } from "@/lib/format";
import { caseHref } from "@/lib/hooks";
import { Badge, KindTag, KV, Loading, Modal, SplitTag, VerdictBadge } from "@/components/ui";

export function SampleModal({ sampleId, versionId, onClose }: { sampleId: string | null; versionId: string; onClose: () => void }) {
  const { data } = useApi<any>(sampleId ? `/samples/${sampleId}` : null);
  return (
    <Modal open={!!sampleId} onClose={onClose} title={data?.name ?? "Sample"} wide>
      {!data ? (
        <Loading rows={2} />
      ) : (
        <div className="grid gap-5 md:grid-cols-[300px_1fr]">
          <div>
            <div className="thumb-checker overflow-hidden rounded-lg border hairline">
              <img src={data.image_url} alt={`${data.label} — ${data.name}`} className="w-full object-contain" />
            </div>
            <div className="mt-1.5 text-xs text-subtle">
              {data.attributes?.browser_copy && <span>Preview converted from {data.format}; the original file is unchanged. </span>}
              {data.original_url && (
                <a href={data.original_url} className="underline-offset-2 hover:underline">
                  Download original ({data.format})
                </a>
              )}
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-1.5">
              <Badge tone="accent">{data.label}</Badge>
              <SplitTag split={data.split} />
              {data.case && (
                <Link href={caseHref(versionId, data.case.id)}>
                  <VerdictBadge verdict={data.case.verdict} />
                </Link>
              )}
            </div>
          </div>
          <div className="space-y-4 text-sm">
            <KV
              items={[
                ["Path", <span key="p" className="font-mono text-xs">{data.path}</span>],
                ["Size", `${data.width}×${data.height} · ${data.format} · ${data.mode} · ${bytes(data.byte_size)}`],
                ["SHA-256", <span key="s" className="font-mono text-xs">{data.sha256}</span>],
                ["pHash", <span key="h" className="font-mono text-xs">{data.phash}</span>],
                ["Provenance", data.provenance ? Object.entries(data.provenance).map(([k, v]) => `${k}: ${v}`).join("; ") : <span key="n" className="text-subtle">not recorded</span>],
              ]}
            />
            {data.prediction && (
              <div>
                <div className="mb-1 flex items-center gap-2 font-medium">
                  Baseline model <KindTag kind="model_prediction" />
                </div>
                <ul className="space-y-0.5 text-xs">
                  {data.prediction.top_probs.map(([c, p]: [string, number]) => (
                    <li key={c} className="flex justify-between">
                      <span className={c === data.label ? "font-medium" : undefined}>{c}</span>
                      <span className="tabular">{pct(p, 1)}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {data.label_finding && (
              <div>
                <div className="mb-1 flex items-center gap-2 font-medium">
                  Label evidence <KindTag kind="heuristic" />
                </div>
                <p className="text-xs text-muted">
                  Suspicion {fixed(data.label_finding.suspicion, 2)} · {humanize(data.label_finding.action)} · neighbours agree {pct(data.label_finding.evidence.neighbor_same_label_share)}
                </p>
              </div>
            )}
            {data.quality_findings?.length > 0 && (
              <div>
                <div className="mb-1 flex items-center gap-2 font-medium">
                  Quality findings <KindTag kind="measured" />
                </div>
                <ul className="space-y-1 text-xs text-muted">
                  {data.quality_findings.map((f: any, i: number) => (
                    <li key={i}>
                      <Badge tone={f.severity === "high" ? "bad" : f.severity === "medium" ? "warn" : "neutral"}>{f.severity}</Badge> {f.description}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {data.quality_metrics && (
              <details>
                <summary className="cursor-pointer text-xs font-medium text-muted">All measured attributes</summary>
                <pre className="mt-2 max-h-48 overflow-auto rounded bg-surface-2 p-2 font-mono text-[11px]">{JSON.stringify(data.quality_metrics, null, 1)}</pre>
              </details>
            )}
          </div>
        </div>
      )}
    </Modal>
  );
}
