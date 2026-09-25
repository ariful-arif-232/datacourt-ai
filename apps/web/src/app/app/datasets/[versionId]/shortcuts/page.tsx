"use client";

import { RequireAudit } from "@/components/audit-progress";
import { BarList } from "@/components/charts";
import { Badge, Card, EmptyState, InfoTip, KindTag, Loading, Notice, PageHeader, SampleThumb } from "@/components/ui";
import { fixed, humanize, pct } from "@/lib/format";
import { useVersionApi } from "@/lib/hooks";

export default function ShortcutsPage() {
  const { data } = useVersionApi<any>("/shortcuts");
  const sm = data?.summary?.summary;
  const tests = sm?.perturbation_tests;
  return (
    <RequireAudit>
      <PageHeader
        title="Shortcut / spurious-correlation detective"
        description="Can the label be predicted from things that are not the object — background colour, frame, file format, resolution, camera? Associations are measured, and (in DEEP audits) tested by masking the object or the background. Association is not causation."
      />
      {!data ? (
        <Loading />
      ) : (
        <div className="space-y-5">
          <div className="grid gap-5 lg:grid-cols-2">
            <Card title="Cue ↔ label association" info="cramers_v" subtitle="Bias-corrected Cramér's V on the training split; ≥ 0.3 moderate, ≥ 0.5 strong.">
              <BarList
                max={1}
                format={(v) => v.toFixed(2)}
                data={Object.entries(sm?.per_cue ?? {})
                  .map(([k, v]: [string, any]) => ({
                    label: humanize(k),
                    value: v.cramers_v,
                    color: v.cramers_v >= 0.5 ? "var(--bad)" : v.cramers_v >= 0.3 ? "var(--warn)" : "var(--series-1)",
                    hint: `train V=${fixed(v.cramers_v, 3)}${v.cramers_v_eval !== null ? `, eval V=${fixed(v.cramers_v_eval, 3)}` : ""}`,
                  }))
                  .sort((a, b) => b.value - a.value)}
              />
            </Card>
            <Card title="Predictability from cues alone" info="cue_only_accuracy">
              <div className="flex items-end gap-6">
                <div>
                  <div className="text-3xl font-semibold tabular">{pct(sm?.all_cues_balanced_accuracy)}</div>
                  <div className="text-xs text-muted">balanced accuracy using only non-content cues</div>
                </div>
                <div>
                  <div className="text-3xl font-semibold tabular text-subtle">{pct(sm?.chance_balanced_accuracy)}</div>
                  <div className="text-xs text-muted">chance level</div>
                </div>
              </div>
              {tests ? (
                <div className="mt-5 border-t hairline pt-4">
                  <div className="mb-2 flex items-center gap-2 text-sm font-medium">
                    Perturbation test <KindTag kind="model_estimate" />
                  </div>
                  <div className="grid grid-cols-3 gap-2 text-center text-xs">
                    <div className="rounded-lg bg-surface-2 p-2">
                      <div className="text-lg font-semibold tabular">{pct(tests.original_accuracy)}</div>original images
                    </div>
                    <div className="rounded-lg bg-surface-2 p-2">
                      <div className={`text-lg font-semibold tabular ${tests.border_only_accuracy > tests.chance * 1.5 ? "text-bad" : ""}`}>{pct(tests.border_only_accuracy)}</div>
                      background only
                    </div>
                    <div className="rounded-lg bg-surface-2 p-2">
                      <div className="text-lg font-semibold tabular">{pct(tests.content_only_accuracy)}</div>object only
                    </div>
                  </div>
                  <p className="mt-2 text-xs text-muted">{tests.interpretation} n = {tests.n} evaluation images.</p>
                </div>
              ) : (
                <p className="mt-4 text-xs text-subtle">Perturbation tests run in DEEP audits with an evaluation split.</p>
              )}
            </Card>
          </div>
          {data.findings.length === 0 ? (
            <EmptyState title="No moderate or strong cue–label associations">No non-content cue is strongly tied to any class on this dataset.</EmptyState>
          ) : (
            data.findings.map((f: any) => (
              <Card
                key={f.id}
                title={
                  <span className="flex flex-wrap items-center gap-2">
                    {humanize(f.cue)} = “{f.cue_value}” → {f.class}
                    <Badge tone={f.strength_label === "strong" ? "bad" : "warn"}>{f.strength_label}</Badge>
                    <KindTag kind="heuristic" />
                  </span>
                }
              >
                <div className="grid gap-4 lg:grid-cols-[1fr_1fr]">
                  <div className="space-y-2 text-sm">
                    <p>{f.consequence}</p>
                    <Notice tone="info" title="Recommended test">
                      {f.recommendation}
                    </Notice>
                    <p className="flex flex-wrap gap-x-3 text-xs text-muted tabular">
                      <span>V {fixed(f.association.cramers_v, 3)}</span>
                      {f.association.cramers_v_eval !== null && <span>eval V {fixed(f.association.cramers_v_eval, 3)}</span>}
                      <span>lift {fixed(f.association.lift, 2)}×</span>
                      <span>P(class | cue) {pct(f.association.p_class_given_cue)}</span>
                      <span>{f.affected_count} samples</span>
                      {f.association.cue_only_balanced_accuracy !== null && (
                        <span className="inline-flex items-center gap-1">
                          single-cue accuracy {pct(f.association.cue_only_balanced_accuracy)} <InfoTip term="cue_only_accuracy" />
                        </span>
                      )}
                    </p>
                  </div>
                  <ul className="flex flex-wrap gap-2">
                    {f.examples.map((s: any) => (
                      <li key={s.id}>
                        <SampleThumb s={s} size={84} caption={false} />
                      </li>
                    ))}
                  </ul>
                </div>
              </Card>
            ))
          )}
        </div>
      )}
    </RequireAudit>
  );
}
