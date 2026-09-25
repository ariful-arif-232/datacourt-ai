import { existsSync } from "node:fs";
import { join } from "node:path";
import { DOCS, loadDoc, slugify } from "@/lib/docs";

describe("docs", () => {
  it("has a synced markdown file for every registered page", () => {
    for (const d of DOCS) expect(existsSync(join(process.cwd(), "src", "content", "docs", d.file))).toBe(true);
  });

  it("renders anchors used by in-app links", () => {
    const doc = loadDoc("methodology")!;
    expect(doc.html).toContain('id="duplicates"');
    expect(doc.headings.map((h) => h.id)).toContain("court-cases-and-the-jury");
  });

  it("rewrites relative links to /docs routes and the repository", () => {
    expect(loadDoc("security")!.html).toContain('href="/docs/methodology#evidence-ledger"');
    const evaluation = loadDoc("evaluation")!;
    expect(evaluation.html).toMatch(/href="https:\/\/github\.com\/[^"]+\/blob\/HEAD\/docs\/benchmark\/results\.json"/);
    // Files outside docs/ resolve against the repository root.
    const deployment = loadDoc("deployment")!;
    expect(deployment.html).toMatch(/href="https:\/\/github\.com\/[^"]+\/blob\/HEAD\/vercel\.json"/);
  });

  it("returns null for unknown pages", () => {
    expect(loadDoc("nope")).toBeNull();
  });

  it("slugifies headings like GitHub", () => {
    expect(slugify("Court cases and the jury")).toBe("court-cases-and-the-jury");
    expect(slugify("What-if Lab")).toBe("what-if-lab");
  });
});
