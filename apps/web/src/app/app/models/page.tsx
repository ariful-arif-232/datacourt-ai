"use client";

import Link from "next/link";
import { Badge, Card, KindTag, Loading, Notice, PageHeader } from "@/components/ui";
import { useApi } from "@/lib/api";

type Registry = {
  algorithms: { area: string; id: string; kind: string; summary: string }[];
  embedding_backends: { id: string; default: boolean; dim: number; source: string; summary: string }[];
  configured_embedding_backend: string;
  llm: { provider: string | null; model: string | null; role: string };
};

export default function ModelsPage() {
  const { data, error } = useApi<Registry>("/algorithms");
  return (
    <>
      <PageHeader
        title="Models & algorithms"
        description="Every score in DataCourt is produced by a named, versioned algorithm. Audits store the exact versions and configuration they ran with, so results stay reproducible after upgrades."
      />
      {error ? (
        <Notice tone="bad">{error.message}</Notice>
      ) : !data ? (
        <Loading />
      ) : (
        <div className="space-y-5">
          <Card title="Embedding backends" subtitle="Frozen image representations used for similarity, kNN witnesses, the baseline classifier and coverage maps.">
            <ul className="grid gap-3 md:grid-cols-2">
              {data.embedding_backends.map((b) => (
                <li key={b.id} className="rounded-lg border hairline p-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-sm font-medium">{b.id}</span>
                    {b.id === data.configured_embedding_backend && <Badge tone="accent">active</Badge>}
                    <span className="text-xs text-muted">{b.dim}-d</span>
                  </div>
                  <p className="mt-1 text-xs text-muted">{b.summary}</p>
                  <p className="mt-1 text-xs text-subtle">{b.source}</p>
                </li>
              ))}
            </ul>
          </Card>
          <Card title="Language model">
            <p className="text-sm">
              {data.llm.provider ? (
                <>
                  <span className="font-mono">{data.llm.model}</span> ({data.llm.provider}) is configured.
                </>
              ) : (
                <>No language model is configured; explanations use deterministic templates.</>
              )}{" "}
              <span className="text-muted">{data.llm.role}</span>
            </p>
          </Card>
          <Card title="Versioned algorithms">
            <table className="w-full text-sm">
              <thead className="text-left text-xs text-subtle">
                <tr>
                  <th className="py-1 pr-3 font-medium">Area</th>
                  <th className="py-1 pr-3 font-medium">Version</th>
                  <th className="py-1 pr-3 font-medium">Output type</th>
                  <th className="py-1 font-medium">What it does</th>
                </tr>
              </thead>
              <tbody>
                {data.algorithms.map((a) => (
                  <tr key={a.id} className="border-t hairline align-top">
                    <td className="py-2 pr-3 text-muted">{a.area}</td>
                    <td className="py-2 pr-3 font-mono text-xs">{a.id}</td>
                    <td className="py-2 pr-3">
                      <KindTag kind={a.kind} />
                    </td>
                    <td className="py-2 text-xs text-muted">{a.summary}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="mt-3 text-xs text-subtle">
              Formulas, thresholds and known limitations are documented in the{" "}
              <Link href="/docs/methodology" className="underline">
                methodology
              </Link>
              .
            </p>
          </Card>
        </div>
      )}
    </>
  );
}
