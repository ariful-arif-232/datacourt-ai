import { readFileSync } from "node:fs";
import { join } from "node:path";
import { Marked, type Tokens } from "marked";

export type DocMeta = { slug: string; file: string; title: string; summary: string; group: "Product" | "Trust" | "Build" };

export const DOCS: DocMeta[] = [
  { slug: "methodology", file: "METHODOLOGY.md", title: "Methodology", summary: "Every stage, formula, threshold and limitation.", group: "Product" },
  { slug: "evaluation", file: "EVALUATION.md", title: "Evaluation", summary: "Benchmark with known answers: precision, recall, review effort, what-if.", group: "Product" },
  { slug: "novelty", file: "NOVELTY.md", title: "Novelty & positioning", summary: "What is foundational, what is different, and what we do not claim.", group: "Product" },
  { slug: "algorithms", file: "ALGORITHM_VERSIONS.md", title: "Algorithm versions", summary: "Versioned algorithms and the versioning policy.", group: "Product" },
  { slug: "security", file: "SECURITY.md", title: "Security", summary: "Threat model, tenancy, authentication, upload safety, secrets.", group: "Trust" },
  { slug: "privacy", file: "PRIVACY.md", title: "Privacy", summary: "What is stored, third parties, privacy scan, retention and deletion.", group: "Trust" },
  { slug: "dataset-formats", file: "DATASET_FORMATS.md", title: "Dataset formats", summary: "Supported ZIP layouts, metadata files, limits and the export format.", group: "Build" },
  { slug: "api", file: "API.md", title: "CI & API", summary: "Data-contract gate for CI, tokens and the REST API.", group: "Build" },
  { slug: "architecture", file: "ARCHITECTURE.md", title: "Architecture", summary: "Services, job queue, pipeline, data model and scaling.", group: "Build" },
  { slug: "deployment", file: "DEPLOYMENT.md", title: "Deployment", summary: "Vercel, GitHub Actions worker, Neon and Backblaze B2 setup; local development.", group: "Build" },
];

const REPO = process.env.NEXT_PUBLIC_GITHUB_URL || "https://github.com/ariful-arif-232/datacourt-ai";
const BY_FILE = new Map(DOCS.map((d) => [d.file, d.slug]));

export function slugify(text: string): string {
  return text
    .toLowerCase()
    .replace(/<[^>]+>/g, "")
    .replace(/&[a-z]+;/g, "")
    .replace(/[^a-z0-9\s-]/g, "")
    .trim()
    .replace(/\s+/g, "-");
}

function rewriteHref(href: string): string {
  if (/^(https?:|mailto:|#)/.test(href)) return href;
  const [path, hash] = href.split("#");
  const slug = BY_FILE.get(path.replace(/^\.\//, ""));
  if (slug) return `/docs/${slug}${hash ? `#${hash}` : ""}`;
  // Repository files (e.g. ../vercel.json): link to GitHub on the default branch (HEAD).
  const url = new URL(path, `${REPO}/blob/HEAD/docs/`);
  return `${url.toString()}${hash ? `#${hash}` : ""}`;
}

export type Heading = { id: string; text: string };

export function loadDoc(slug: string): { meta: DocMeta; html: string; headings: Heading[] } | null {
  const meta = DOCS.find((d) => d.slug === slug);
  if (!meta) return null;
  const md = readFileSync(join(process.cwd(), "src", "content", "docs", meta.file), "utf8");
  const headings: Heading[] = [];
  const marked = new Marked({ gfm: true });
  marked.use({
    renderer: {
      heading(this: any, token: Tokens.Heading) {
        const text = this.parser.parseInline(token.tokens);
        if (token.depth === 1) return ""; // the page header renders the title
        const id = slugify(token.text);
        if (token.depth === 2) headings.push({ id, text: token.text.replace(/`/g, "") });
        return `<h${token.depth} id="${id}"><a href="#${id}" class="anchor">${text}</a></h${token.depth}>\n`;
      },
      link(this: any, token: Tokens.Link) {
        const href = rewriteHref(token.href);
        const external = /^https?:/.test(href) && !href.startsWith(REPO);
        const text = this.parser.parseInline(token.tokens);
        return `<a href="${href}"${external ? ' target="_blank" rel="noopener noreferrer"' : ""}>${text}</a>`;
      },
      table(this: any, token: Tokens.Table) {
        const head = token.header.map((c) => `<th>${this.parser.parseInline(c.tokens)}</th>`).join("");
        const rows = token.rows.map((r) => `<tr>${r.map((c) => `<td>${this.parser.parseInline(c.tokens)}</td>`).join("")}</tr>`).join("");
        return `<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table></div>\n`;
      },
    },
  });
  const html = marked.parse(md, { async: false }) as string;
  return { meta, html, headings };
}
