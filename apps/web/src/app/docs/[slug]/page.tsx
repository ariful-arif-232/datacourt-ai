import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { DOCS, loadDoc } from "@/lib/docs";

export const dynamicParams = false;

export function generateStaticParams() {
  return DOCS.map((d) => ({ slug: d.slug }));
}

export async function generateMetadata({ params }: { params: Promise<{ slug: string }> }): Promise<Metadata> {
  const { slug } = await params;
  const meta = DOCS.find((d) => d.slug === slug);
  return meta ? { title: meta.title, description: meta.summary } : {};
}

export default async function DocPage({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  const doc = loadDoc(slug);
  if (!doc) notFound();
  return (
    <div className="grid gap-10 xl:grid-cols-[1fr_200px]">
      <article className="min-w-0">
        <h1 className="font-display text-4xl">{doc.meta.title}</h1>
        <p className="mt-2 text-muted">{doc.meta.summary}</p>
        {/* Content is our own repository markdown, rendered at build time. */}
        <div className="prose-dc mt-8 max-w-3xl" dangerouslySetInnerHTML={{ __html: doc.html }} />
      </article>
      {doc.headings.length > 2 && (
        <aside className="hidden xl:block">
          <nav aria-label="On this page" className="sticky top-20 text-sm">
            <div className="mb-2 text-xs font-medium uppercase tracking-wide text-subtle">On this page</div>
            <ul className="space-y-1.5">
              {doc.headings.map((h) => (
                <li key={h.id}>
                  <a href={`#${h.id}`} className="text-muted hover:text-ink">
                    {h.text}
                  </a>
                </li>
              ))}
            </ul>
          </nav>
        </aside>
      )}
    </div>
  );
}
