"use client";

import { useState } from "react";
import { ShieldCheck, ShieldX } from "lucide-react";
import { Button, Card, EmptyState, InfoTip, Loading, Notice, PageHeader } from "@/components/ui";
import { api, qs, useApi } from "@/lib/api";
import { ago } from "@/lib/format";
import { useSession } from "@/lib/session";

type Entry = {
  seq: number;
  id: string;
  event_type: string;
  entity_type: string;
  entity_id: string;
  actor: string | null;
  payload: Record<string, unknown>;
  payload_sha256: string;
  prev_hash: string;
  entry_hash: string;
  dataset_version_id: string | null;
  created_at: string;
};

export default function LedgerPage() {
  const { org } = useSession();
  const [before, setBefore] = useState<number | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const { data, error } = useApi<{ entries: Entry[] }>(org ? `/orgs/${org.id}/ledger${qs({ limit: 50, before_seq: before ?? undefined })}` : null);
  const [verify, setVerify] = useState<any>(null);
  const [checking, setChecking] = useState(false);
  const runVerify = async () => {
    if (!org) return;
    setChecking(true);
    try {
      setVerify(await api(`/orgs/${org.id}/ledger/verify`));
    } finally {
      setChecking(false);
    }
  };
  const entries = data?.entries ?? [];
  return (
    <>
      <PageHeader
        title={
          <span className="flex items-center gap-2">
            Evidence ledger <InfoTip term="ledger" />
          </span>
        }
        description="An append-only, hash-chained record of audits, verdicts, human decisions, overrides, experiments and exports. Each entry commits to the previous one, so any later edit breaks the chain."
        actions={
          <Button onClick={runVerify} loading={checking}>
            <ShieldCheck className="size-4" aria-hidden /> Verify chain
          </Button>
        }
      />
      {verify && (
        <div className="mb-4">
          {verify.valid ? (
            <Notice tone="ok" title="Chain intact">
              {verify.checked} entries verified. Head hash <span className="font-mono">{String(verify.head ?? "").slice(0, 20)}…</span>
            </Notice>
          ) : (
            <Notice tone="bad" title="Chain broken">
              <span className="inline-flex items-center gap-1">
                <ShieldX className="size-4" aria-hidden /> Verification failed at entry #{verify.broken_at} after checking {verify.checked} entries.
              </span>
            </Notice>
          )}
        </div>
      )}
      {error ? (
        <Notice tone="bad">{error.message}</Notice>
      ) : !data ? (
        <Loading />
      ) : entries.length === 0 ? (
        <EmptyState title="No ledger entries yet">Entries appear when you upload data, run audits and record decisions.</EmptyState>
      ) : (
        <Card>
          <ol className="divide-y divide-border">
            {entries.map((e) => (
              <li key={e.id} className="py-2.5 text-sm">
                <button type="button" className="flex w-full flex-wrap items-center gap-2 text-left" onClick={() => setOpen(open === e.seq ? null : e.seq)} aria-expanded={open === e.seq}>
                  <span className="w-12 shrink-0 font-mono text-xs text-subtle">#{e.seq}</span>
                  <span className="font-mono text-xs">{e.event_type}</span>
                  <span className="text-xs text-muted">
                    {e.entity_type} {e.entity_id.slice(0, 8)}
                  </span>
                  <div className="flex-1" />
                  <span className="text-xs text-muted">{e.actor ?? "system"}</span>
                  <span className="w-24 text-right text-xs text-subtle">{ago(e.created_at)}</span>
                </button>
                {open === e.seq && (
                  <div className="mt-2 space-y-2 rounded-lg bg-surface-2 p-3 text-xs">
                    <div className="grid gap-1 font-mono">
                      <div>
                        <span className="text-subtle">prev_hash </span>
                        {e.prev_hash}
                      </div>
                      <div>
                        <span className="text-subtle">payload_sha256 </span>
                        {e.payload_sha256}
                      </div>
                      <div>
                        <span className="text-subtle">entry_hash </span>
                        {e.entry_hash}
                      </div>
                    </div>
                    <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all font-mono">{JSON.stringify(e.payload, null, 2)}</pre>
                  </div>
                )}
              </li>
            ))}
          </ol>
          <div className="mt-3 flex justify-between">
            <Button size="sm" variant="ghost" disabled={before === null} onClick={() => setBefore(null)}>
              Newest
            </Button>
            <Button size="sm" disabled={entries.length < 50} onClick={() => setBefore(entries[entries.length - 1].seq)}>
              Older entries
            </Button>
          </div>
        </Card>
      )}
    </>
  );
}
