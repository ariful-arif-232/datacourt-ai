"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { cx } from "@/components/ui";

type Item = { slug: string; title: string; group: string };

export function DocsNav({ items }: { items: Item[] }) {
  const path = usePathname();
  const groups = [...new Set(items.map((i) => i.group))];
  return (
    <nav aria-label="Documentation" className="text-sm md:sticky md:top-20 md:self-start">
      <Link href="/docs" className={cx("mb-4 block font-medium", path === "/docs" ? "text-ink" : "text-muted hover:text-ink")}>
        Documentation
      </Link>
      <div className="flex gap-6 overflow-x-auto md:block md:space-y-5">
        {groups.map((g) => (
          <div key={g} className="shrink-0">
            <div className="mb-1.5 text-xs font-medium uppercase tracking-wide text-subtle">{g}</div>
            <ul className="space-y-1">
              {items
                .filter((i) => i.group === g)
                .map((i) => {
                  const active = path === `/docs/${i.slug}`;
                  return (
                    <li key={i.slug}>
                      <Link
                        href={`/docs/${i.slug}`}
                        aria-current={active ? "page" : undefined}
                        className={cx("block rounded px-2 py-1 -mx-2", active ? "bg-surface-2 font-medium text-ink" : "text-muted hover:text-ink")}
                      >
                        {i.title}
                      </Link>
                    </li>
                  );
                })}
            </ul>
          </div>
        ))}
      </div>
    </nav>
  );
}
