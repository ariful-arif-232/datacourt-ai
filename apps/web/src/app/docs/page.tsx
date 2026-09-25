import type { Metadata } from "next";
import Link from "next/link";
import { DOCS } from "@/lib/docs";

export const metadata: Metadata = { title: "Documentation", description: "Methodology, evaluation, security, privacy, dataset formats, CI and architecture." };

export default function DocsIndex() {
  const groups = [...new Set(DOCS.map((d) => d.group))];
  return (
    <>
      <h1 className="font-display text-4xl">Documentation</h1>
      <p className="mt-3 max-w-2xl text-muted">
        How DataCourt turns data into evidence, how it was evaluated, how your data is protected, and how to run it. These pages are rendered from the <code className="font-mono text-sm">docs/</code> folder of the
        repository.
      </p>
      {groups.map((g) => (
        <section key={g} className="mt-10">
          <h2 className="text-xs font-medium uppercase tracking-wide text-subtle">{g}</h2>
          <ul className="mt-3 grid gap-3 sm:grid-cols-2">
            {DOCS.filter((d) => d.group === g).map((d) => (
              <li key={d.slug}>
                <Link href={`/docs/${d.slug}`} className="card block h-full p-4 transition-colors hover:bg-surface-2">
                  <div className="font-medium">{d.title}</div>
                  <p className="mt-1 text-sm text-muted">{d.summary}</p>
                </Link>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </>
  );
}
