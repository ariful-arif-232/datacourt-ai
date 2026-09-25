import Link from "next/link";
import type { ReactNode } from "react";
import { ArrowRight, BookOpen, GitBranch } from "lucide-react";
import { DemoButton, GITHUB_URL, SiteFooter, SiteHeader } from "@/components/site";
import { Button, KindTag } from "@/components/ui";

const WORKFLOW = [
  ["Find", "Quality defects, duplicate families, split leakage, suspicious labels, shortcuts, coverage gaps and privacy flags."],
  ["Explain", "Every finding carries its evidence: which witness said what, how reliable that witness is, and the thresholds used."],
  ["Prioritise", "A deterministic jury turns evidence into cases, and the Review Budget orders them by expected value per minute."],
  ["Review", "Humans decide with keyboard-first review, multi-reviewer consensus and expert adjudication."],
  ["Test", "The What-if Lab retrains the baseline on the changed dataset and measures a preserved holdout set."],
  ["Prove", "Hash-chained evidence ledger, versioned algorithms, Dataset Debt, preflight gate and an audit report."],
  ["Export", "A new dataset version built from final human decisions, with manifest, action log and checksums."],
] as const;

function Section({ id, eyebrow, title, children, aside }: { id?: string; eyebrow: string; title: ReactNode; children: ReactNode; aside?: ReactNode }) {
  return (
    <section id={id} className="border-t hairline">
      <div className="mx-auto grid max-w-6xl gap-10 px-4 py-16 md:grid-cols-[1fr_1.1fr] md:py-20">
        <div>
          <div className="text-xs font-medium uppercase tracking-[0.14em] text-accent">{eyebrow}</div>
          <h2 className="mt-2 font-display text-3xl leading-tight md:text-[34px]">{title}</h2>
          <div className="prose-dc mt-4 text-[15px] text-muted">{children}</div>
        </div>
        {aside && <div className="self-center">{aside}</div>}
      </div>
    </section>
  );
}

function Panel({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`card p-5 text-sm ${className}`}>{children}</div>;
}

function Row({ k, v, mono }: { k: ReactNode; v: ReactNode; mono?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-t hairline py-2 first:border-t-0">
      <span className="text-muted">{k}</span>
      <span className={mono ? "font-mono text-xs" : "tabular font-medium"}>{v}</span>
    </div>
  );
}

function Tile({ value, label, note }: { value: string; label: string; note?: string }) {
  return (
    <div className="card p-4">
      <div className="font-display text-3xl tabular">{value}</div>
      <div className="mt-1 text-sm">{label}</div>
      {note && <div className="mt-1 text-xs text-subtle">{note}</div>}
    </div>
  );
}

export default function Landing() {
  return (
    <>
      <SiteHeader />
      <main id="main">
        <section className="mx-auto max-w-6xl px-4 pb-16 pt-16 md:pt-24">
          <div className="grid items-center gap-12 md:grid-cols-[1.15fr_1fr]">
            <div>
              <p className="text-sm font-medium text-accent">DataCourt AI</p>
              <h1 className="mt-3 font-display text-[40px] leading-[1.08] tracking-tight md:text-[56px]">Put your dataset on trial before your model pays the price.</h1>
              <p className="mt-5 max-w-xl text-lg text-muted">Find the data hurting your model, understand why, review what matters first, and prove whether the fix actually helps.</p>
              <div className="mt-8 flex flex-wrap items-center gap-3">
                <Button href="/register" variant="primary">
                  Audit a dataset <ArrowRight className="size-4" aria-hidden />
                </Button>
                <Button href="/docs/methodology">
                  <BookOpen className="size-4" aria-hidden /> View methodology
                </Button>
              </div>
              <DemoButton className="mt-3 block" />
              <p className="mt-6 text-xs text-subtle">Image classification datasets as ZIP folders. Private by default. No model training on your data outside your workspace.</p>
            </div>
            <Panel className="shadow-sm">
              <div className="flex items-center justify-between">
                <span className="font-mono text-xs text-subtle">Case #0014 · illustrative</span>
                <span className="rounded-full bg-warn-soft px-2 py-0.5 text-xs font-medium text-warn">Possible relabel</span>
              </div>
              <p className="mt-3 font-medium">train/circle/img_0412.png is labelled “circle”.</p>
              <div className="mt-4 space-y-2">
                <div className="rounded-lg border hairline p-3">
                  <div className="text-xs font-medium uppercase tracking-wide text-bad">Prosecution</div>
                  <p className="mt-1 text-muted">Out-of-fold baseline predicts “triangle” (p = 0.91); 9 of 10 nearest neighbours are triangles.</p>
                </div>
                <div className="rounded-lg border hairline p-3">
                  <div className="text-xs font-medium uppercase tracking-wide text-ok">Defence</div>
                  <p className="mt-1 text-muted">Image quality is normal; the sample is not isolated, so a rare-but-valid explanation is weak.</p>
                </div>
              </div>
              <div className="mt-4">
                <Row k="Jury rule" v="R5 · consistent alternative label" />
                <Row k="Witness reliability (kNN / baseline)" v="0.71 / 0.64" />
                <Row k="Suggested action" v="Relabel → triangle (needs human)" />
              </div>
              <div className="mt-3 flex flex-wrap gap-1.5">
                <KindTag kind="measured" />
                <KindTag kind="model_prediction" />
                <KindTag kind="heuristic" />
                <KindTag kind="human" />
              </div>
            </Panel>
          </div>
        </section>

        <Section
          eyebrow="The problem"
          title="Models inherit every flaw in their data, and most tools stop at a list of suspicious images."
          aside={
            <div className="grid gap-3 sm:grid-cols-2">
              <Panel>
                <div className="font-medium">Leakage inflates metrics</div>
                <p className="mt-1 text-muted">A resized copy of a training image in the test split makes the model look better than it is.</p>
              </Panel>
              <Panel>
                <div className="font-medium">Mislabels teach the wrong thing</div>
                <p className="mt-1 text-muted">A few percent of wrong labels move decision boundaries and hide real errors.</p>
              </Panel>
              <Panel>
                <div className="font-medium">Shortcuts pass validation</div>
                <p className="mt-1 text-muted">If every “star” has a blue border, the model can learn the border and still score well.</p>
              </Panel>
              <Panel>
                <div className="font-medium">Rare is not wrong</div>
                <p className="mt-1 text-muted">Unusual but valid examples look like outliers. Deleting them makes the model more brittle.</p>
              </Panel>
            </div>
          }
        >
          <p>
            Flagging is the easy part. What teams actually need is to know which findings are real, which matter for the model, which to look at first with limited review time, and whether fixing
            them improves anything measurable.
          </p>
        </Section>

        <Section
          eyebrow="Evidence, not guesses"
          title="Every number says where it came from."
          aside={
            <Panel>
              <Row k={<KindTag kind="measured" />} v="hashes, image statistics, split overlap" />
              <Row k={<KindTag kind="heuristic" />} v="versioned rules and thresholds" />
              <Row k={<KindTag kind="model_prediction" />} v="out-of-fold baseline predictions" />
              <Row k={<KindTag kind="model_estimate" />} v="influence, what-if effects" />
              <Row k={<KindTag kind="llm" />} v="plain-language summaries only" />
              <Row k={<KindTag kind="human" />} v="the only thing that changes data" />
            </Panel>
          }
        >
          <p>
            DataCourt labels each output with its type. Scores and verdicts come from deterministic, versioned algorithms. An optional language model can phrase an explanation, but it never produces a
            score or a verdict, and numbers it adds that are not in the evidence get flagged.
          </p>
          <p>Audits record the exact algorithm versions and configuration they ran with, so results can be reproduced and compared across releases.</p>
        </Section>

        <section id="workflow" className="border-t hairline">
          <div className="mx-auto max-w-6xl px-4 py-16 md:py-20">
            <div className="text-xs font-medium uppercase tracking-[0.14em] text-accent">The DataCourt workflow</div>
            <h2 className="mt-2 max-w-2xl font-display text-3xl md:text-[34px]">From suspicion to a proven, versioned fix.</h2>
            <ol className="mt-10 grid gap-px overflow-hidden rounded-xl border hairline bg-border sm:grid-cols-2 lg:grid-cols-7">
              {WORKFLOW.map(([name, text], i) => (
                <li key={name} className="bg-surface p-4">
                  <div className="font-mono text-xs text-subtle">0{i + 1}</div>
                  <div className="mt-1 font-medium">{name}</div>
                  <p className="mt-1 text-xs leading-relaxed text-muted">{text}</p>
                </li>
              ))}
            </ol>
          </div>
        </section>

        <Section
          eyebrow="Case Court"
          title="Findings become cases with a prosecution, a defence and a rule-based jury."
          aside={
            <Panel>
              <Row k="Witnesses" v="baseline, kNN, centroid, dynamics, influence" />
              <Row k="Witness weight" v="chance-corrected reliability on this dataset" />
              <Row k="Jury" v="ordered rules R1–R10 with a full trace" />
              <Row k="Verdicts" v="KEEP · REVIEW · STRONG REVIEW · POSSIBLE RELABEL · LIKELY RARE · …" />
              <Row k="Final say" v="human reviewers, with consensus and adjudication" />
            </Panel>
          }
        >
          <p>
            A case shows the evidence for the problem and the evidence against it, including the rare-but-valid hypothesis. A deterministic jury weighs witnesses by how reliable they proved to be on this
            dataset and records which rule fired. Nothing is deleted or relabelled automatically.
          </p>
        </Section>

        <Section
          eyebrow="Review Budget"
          title="Spend review time where it changes the most."
          aside={
            <div className="grid grid-cols-2 gap-3">
              <Tile value="55" label="reviews to find 80% of injected label errors" note="vs. ~895 in random order (synthetic benchmark)" />
              <Tile value="26" label="reviews to find 50%" note="vs. ~560 in random order" />
            </div>
          }
        >
          <p>
            Given a budget in minutes or items, the optimiser picks cases by expected impact per minute, with diminishing returns so one duplicate family or one class cannot absorb the whole budget. You
            can optimise for label errors, leakage or a balance.
          </p>
          <p className="text-xs">
            Measured on DataCourt’s synthetic benchmark with injected, known errors. Real-world gains depend on the dataset. See{" "}
            <Link href="/docs/evaluation" className="underline">
              evaluation
            </Link>
            .
          </p>
        </Section>

        <Section
          eyebrow="Model-to-Data Blame Map"
          title="Trace model failures back to the training data that caused them."
          aside={
            <Panel>
              <Row k="Evaluation failure" v="a star predicted as “circle”" />
              <Row k="Harmful training samples" v="ranked by TracIn on the baseline head" />
              <Row k="Linked evidence" v="open label cases and duplicate families" />
              <Row k="Failure Replay" v="retrain after fixes and see whether it flips" />
            </Panel>
          }
        >
          <p>
            For each evaluation error, DataCourt estimates which training samples pushed the model toward the wrong answer, and connects them to open cases. Influence is an approximation on a linear
            head, labelled as a model estimate. It is not a causal proof.
          </p>
        </Section>

        <Section
          eyebrow="What-if Lab"
          title="Test a cleanup before you commit to it."
          aside={
            <Panel>
              <Row k="Actions" v="relabel · remove copies · move leakage · protect rare" />
              <Row k="Evaluation sets" v="original · preserved holdout · cleaned" />
              <Row k="Uncertainty" v="paired training bootstrap + evaluation CI" />
              <Row k="Controls" v="random-removal baseline for comparison" />
            </Panel>
          }
        >
          <p>
            Apply proposed changes to a copy, retrain the baseline and compare metrics on a holdout set that no action touched. DataCourt says plainly when an improvement is within training noise instead
            of claiming a win.
          </p>
        </Section>

        <Section
          eyebrow="Dataset Debt"
          title="A running balance of what is wrong with your data."
          aside={
            <Panel>
              <Row k="Leakage debt" v="evaluation samples with training copies" />
              <Row k="Label debt" v="unresolved strong label cases" />
              <Row k="Redundancy debt" v="excess copies within families" />
              <Row k="Shortcut debt" v="strength of class–cue associations" />
              <Row k="Coverage & quality debt" v="gaps and defective images" />
            </Panel>
          }
        >
          <p>
            Debt has published formulas and thresholds. It goes down as reviewers resolve cases, is snapshotted per version, and feeds a preflight gate (ready, ready with warnings, or blocked) and data
            contracts that CI can query.
          </p>
        </Section>

        <Section
          eyebrow="Active Collection Planner"
          title="Know what to collect next, not just what to delete."
          aside={
            <Panel>
              <Row k="Gap type" v="class × condition, sparse region, rare class" />
              <Row k="Example" v="triangles are rarely photographed in low light" />
              <Row k="Recommendation" v="how many more to collect, and of what" />
              <Row k="Evidence" v="coverage map regions and condition tables" />
            </Panel>
          }
        >
          <p>
            A coverage map groups the embedding space into regions and compares classes and conditions such as lighting, resolution and background. Under-covered combinations become concrete collection
            requests.
          </p>
        </Section>

        <Section
          eyebrow="Version Intelligence"
          title="Every export is a new version, compared against the last one."
          aside={
            <Panel>
              <Row k="Diff" v="files · labels · splits · classes" />
              <Row k="Issues" v="leakage fixed / new · label cases resolved / new" />
              <Row k="Dataset DNA" v="versioned fingerprint and drift signals" />
              <Row k="Contamination" v="overlap with your benchmark versions" />
            </Panel>
          }
        >
          <p>
            Clean exports are registered and audited automatically, so you can see what a cleanup actually changed, whether debt fell, and whether the data drifted in ways nobody intended.
          </p>
        </Section>

        <Section
          eyebrow="Security & privacy"
          title="Your data stays in your workspace."
          aside={
            <Panel>
              <Row k="Tenancy" v="organisation-scoped, role-based access" />
              <Row k="Uploads" v="ZIP bomb, path traversal and type checks" />
              <Row k="Storage" v="private bucket, short-lived signed URLs" />
              <Row k="Sessions" v="HttpOnly cookies, CSRF header, rate limits" />
              <Row k="Deletion" v="workspace and account deletion, retention policy" />
              <Row k="Privacy scan" v="face / text detectors, no identity recognition" />
            </Panel>
          }
        >
          <p>
            Originals are immutable and every change is traceable in a hash-chained ledger. Reports are technical audit aids, not legal or compliance certifications.{" "}
            <Link href="/docs/security" className="underline">
              Read the security model
            </Link>
            .
          </p>
        </Section>

        <Section
          eyebrow="Methodology"
          title="Measured on a benchmark with known answers."
          aside={
            <div className="grid grid-cols-2 gap-3">
              <Tile value="1.00 / 0.98" label="duplicate pair precision / recall" note="exact, resized, photometric, flipped, cropped" />
              <Tile value="1.00 / 1.00" label="leakage pair precision / recall" />
              <Tile value="0.74" label="label-error average precision" note="random ranking ≈ 0.04" />
              <Tile value="0%" label="rare-but-valid samples recommended for removal" />
            </div>
          }
        >
          <p>
            A seeded generator plants duplicates, cross-split leakage, label errors, rare valid examples, a colour shortcut, quality defects and coverage gaps, then scores DataCourt against the ground
            truth. The numbers come from the built-in CPU descriptor backend. They show the method works on controlled data, not how it will perform on any given real dataset.
          </p>
          <p>
            <Link href="/docs/methodology" className="underline">
              Methodology
            </Link>{" "}
            ·{" "}
            <Link href="/docs/evaluation" className="underline">
              Full evaluation and limitations
            </Link>
          </p>
        </Section>

        <section className="border-t hairline">
          <div className="mx-auto flex max-w-6xl flex-col items-start gap-6 px-4 py-16 md:flex-row md:items-center md:justify-between">
            <div>
              <h2 className="font-display text-3xl">Open, inspectable, reproducible.</h2>
              <p className="mt-2 max-w-xl text-muted">The source, algorithms, benchmark generator and evaluation scripts are on GitHub. Run the benchmark yourself and check every number on this page.</p>
            </div>
            <div className="flex flex-wrap gap-3">
              <Button href={GITHUB_URL} target="_blank" rel="noopener noreferrer">
                <GitBranch className="size-4" aria-hidden /> View on GitHub
              </Button>
              <Button href="/register" variant="primary">
                Audit a dataset
              </Button>
            </div>
          </div>
        </section>
      </main>
      <SiteFooter />
    </>
  );
}
