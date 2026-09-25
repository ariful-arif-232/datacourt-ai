// Copies the canonical docs (repo-root docs/*.md) into src/content/docs so the /docs pages
// build even when only apps/web is available (e.g. Vercel with a root directory).
// `node scripts/sync-docs.mjs --check` exits 1 if the copies are stale (used in CI).
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const src = join(here, "..", "..", "..", "docs");
const dest = join(here, "..", "src", "content", "docs");
const FILES = [
  "METHODOLOGY.md",
  "EVALUATION.md",
  "ARCHITECTURE.md",
  "SECURITY.md",
  "PRIVACY.md",
  "DATASET_FORMATS.md",
  "API.md",
  "ALGORITHM_VERSIONS.md",
  "NOVELTY.md",
  "DEPLOYMENT.md",
];

const check = process.argv.includes("--check");
if (!existsSync(src)) {
  console.log(`sync-docs: ${src} not found; using committed copies.`);
  process.exit(0);
}
mkdirSync(dest, { recursive: true });
let stale = 0;
for (const f of FILES) {
  const body = readFileSync(join(src, f), "utf8");
  const target = join(dest, f);
  const current = existsSync(target) ? readFileSync(target, "utf8") : null;
  if (current === body) continue;
  stale++;
  if (check) console.error(`sync-docs: ${f} is out of date`);
  else writeFileSync(target, body);
}
if (check && stale) {
  console.error("Run `npm run sync-docs` and commit the result.");
  process.exit(1);
}
console.log(check ? "sync-docs: up to date" : `sync-docs: ${stale} file(s) updated`);
