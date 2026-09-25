import type { ReactNode } from "react";
import { DocsNav } from "@/components/docs-nav";
import { SiteFooter, SiteHeader } from "@/components/site";
import { DOCS } from "@/lib/docs";

export default function DocsLayout({ children }: { children: ReactNode }) {
  const items = DOCS.map(({ slug, title, group }) => ({ slug, title, group }));
  return (
    <>
      <SiteHeader />
      <div className="mx-auto grid max-w-6xl gap-10 px-4 py-10 md:grid-cols-[200px_1fr]">
        <DocsNav items={items} />
        <main id="main" className="min-w-0">
          {children}
        </main>
      </div>
      <SiteFooter />
    </>
  );
}
