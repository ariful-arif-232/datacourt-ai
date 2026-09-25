"use client";

import { Badge, InfoTip, KindTag, Notice } from "@/components/ui";
import { fixed, humanize, pct, signed } from "@/lib/format";

export function WhatIfSummary({ run }: { run: any }) {
  const sm = run.summary || {};
  if (run.status !== "completed") {
    return (
      <Notice tone={run.status === "failed" ? "bad" : "info"}>
        {run.status === "failed" ? run.error : `Experiment ${humanize(run.status).toLowerCase()}${run.progress ? ` · ${pct(run.progress)}` : ""}…`}
      </Notice>
    );
  }
  const sets = Object.entries(sm.deltas ?? {}) as [string, any][];
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
        <KindTag kind="model_estimate" />
        <span>
          {sm.model} · {sm.training_bootstrap_replicates} paired training-bootstrap replicates · training {sm.training_samples?.before} → {sm.training_samples?.after} samples
        </span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-left text-xs text-subtle">
            <tr>
              <th className="pb-1.5 font-medium">Evaluation set</th>
              <th className="pb-1.5 font-medium">
                <span className="inline-flex items-center gap-1">Macro F1 before → after <InfoTip term="macro_f1" /></span>
              </th>
              <th className="pb-1.5 font-medium">Δ macro F1</th>
              <th className="pb-1.5 font-medium">Δ accuracy</th>
              <th className="pb-1.5 font-medium">Δ balanced acc.</th>
            </tr>
          </thead>
          <tbody>
            {sets.map(([name, d]) => (
              <tr key={name} className="border-t hairline">
                <td className="py-1.5 pr-3">
                  <span className="inline-flex items-center gap-1">
                    {humanize(name)} {name === "preserved_holdout" && <InfoTip term="preserved_holdout" />}
                  </span>
                  <div className="text-[11px] text-subtle">n = {sm.eval_sets?.[name]}</div>
                </td>
                <td className="py-1.5 pr-3 tabular">
                  {fixed(d.macro_f1.before, 3)} → {fixed(d.macro_f1.after, 3)}
                </td>
                <td className="py-1.5 pr-3">
                  <Delta d={d.macro_f1} />
                </td>
                <td className="py-1.5 pr-3">
                  <Delta d={d.accuracy} />
                </td>
                <td className="py-1.5">
                  <Delta d={d.balanced_accuracy} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {sm.preserved_holdout_ci && (
        <p className="text-xs text-muted">
          Preserved-holdout macro-F1 Δ, paired evaluation bootstrap 95% CI [{signed(sm.preserved_holdout_ci.ci95[0])}, {signed(sm.preserved_holdout_ci.ci95[1])}]{" "}
          {sm.preserved_holdout_ci.excludes_zero ? <Badge tone="info">excludes 0</Badge> : <Badge>includes 0</Badge>}
        </p>
      )}
      {sm.failure && (
        <Notice tone={sm.failure.fixed ? "ok" : "warn"} title={sm.failure.fixed ? "The replayed failure is fixed in this experiment" : "The replayed failure is not fixed"}>
          True label {sm.failure.true_label}: before predicted {sm.failure.before.predicted} (p(true) {pct(sm.failure.before.p_true)}), after predicted {sm.failure.after.predicted} (p(true) {pct(sm.failure.after.p_true)}).
        </Notice>
      )}
      <p className="text-xs text-subtle">
        {sm.label} {sm.noise_note}
      </p>
    </div>
  );
}

function Delta({ d }: { d: any }) {
  const good = d.delta > 0;
  return (
    <span className="inline-flex items-center gap-1.5 tabular">
      <span className={d.within_noise ? "text-muted" : good ? "text-ok" : d.delta < 0 ? "text-bad" : ""}>{signed(d.delta, 3)}</span>
      <span className="text-[11px] text-subtle">±{fixed(d.bootstrap_std, 3)}</span>
      {d.within_noise && (
        <span className="rounded bg-surface-2 px-1 text-[10px] text-muted" title="|Δ| ≤ 2 × std across training-bootstrap replicates">
          within noise
        </span>
      )}
    </span>
  );
}
