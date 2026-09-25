"use client";

import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { ChevronLeft, ChevronRight, Gavel, Keyboard, Sparkles, Undo2 } from "lucide-react";
import { Suspense, useCallback, useEffect, useState } from "react";
import { RequireAudit } from "@/components/audit-progress";
import {
  Badge,
  Button,
  Card,
  cx,
  EmptyState,
  InfoTip,
  inputCls,
  KindTag,
  Loading,
  Notice,
  SampleThumb,
  Select,
  SplitTag,
  StatusBadge,
  VerdictBadge,
} from "@/components/ui";
import { api, useApi } from "@/lib/api";
import { ago, fixed, humanize, pct, signed } from "@/lib/format";
import { useSession } from "@/lib/session";

const WITNESS_LABEL: Record<string, string> = {
  quality: "Quality witness",
  embedding: "Embedding witness",
  neighbor: "Neighbour witness",
  model: "Model witness",
  split: "Split witness",
  duplicate: "Duplicate witness",
  training_dynamics: "Training-dynamics witness",
  influence: "Influence witness",
  shortcut: "Shortcut witness",
  privacy: "Privacy witness",
};

const ACTIONS = [
  { key: "k", action: "keep", label: "Keep", hint: "K" },
  { key: "r", action: "relabel", label: "Relabel", hint: "R" },
  { key: "x", action: "remove", label: "Remove", hint: "X" },
  { key: "u", action: "unsure", label: "Unsure", hint: "U" },
  { key: "e", action: "escalate", label: "Escalate", hint: "E" },
] as const;

function CaseInner({ versionId, caseId }: { versionId: string; caseId: string }) {
  const params = useSearchParams();
  const sessionId = params.get("session");
  const router = useRouter();
  const { canWrite, isAdmin } = useSession();
  const { data: c, mutate, error } = useApi<any>(`/cases/${caseId}`);
  const { data: session } = useApi<any>(sessionId ? `/review-sessions/${sessionId}` : null);
  const [expl, setExpl] = useState<any>(null);
  const [explBusy, setExplBusy] = useState(false);
  const [targetChoice, setTarget] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [adjudicate, setAdjudicate] = useState(false);
  const [msg, setMsg] = useState<{ tone: "ok" | "bad"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [showCf, setShowCf] = useState(false);
  const { data: cf } = useApi<any>(showCf ? `/cases/${caseId}/counterfactuals` : null);

  // Default relabel target: the jury's suggested class, else the first other class. Per-case state
  // resets because the component is keyed by caseId.
  const suggested = c?.verdict_record?.scores?.target_label;
  const defaultTarget = c ? ((c.classes.find((x: any) => x.name === suggested) ?? c.classes.find((x: any) => x.name !== c.sample.label))?.id ?? "") : "";
  const target = targetChoice ?? defaultTarget;

  const nav = (() => {
    if (session) {
      const ids: string[] = session.items.map((i: any) => i.id);
      const pos = ids.indexOf(caseId);
      return { prev: pos > 0 ? ids[pos - 1] : null, next: pos >= 0 && pos + 1 < ids.length ? ids[pos + 1] : null, position: pos + 1, total: ids.length };
    }
    return c?.navigation;
  })();
  const go = useCallback((id: string | null) => id && router.push(`/app/datasets/${versionId}/court/${id}${sessionId ? `?session=${sessionId}` : ""}`), [router, versionId, sessionId]);

  const decide = useCallback(
    async (action: string) => {
      if (!canWrite || busy) return;
      setBusy(true);
      setMsg(null);
      try {
        await api(`/cases/${caseId}/decision`, { json: { action, target_class_id: action === "relabel" ? target : null, note, adjudicate } });
        setMsg({ tone: "ok", text: `Recorded: ${humanize(action)}${adjudicate ? " (adjudication)" : ""}.` });
        await mutate();
        if (sessionId && nav?.next) setTimeout(() => go(nav.next), 350);
      } catch (e: any) {
        setMsg({ tone: "bad", text: e.message });
      } finally {
        setBusy(false);
      }
    },
    [canWrite, busy, caseId, target, note, adjudicate, mutate, sessionId, nav, go],
  );

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable)) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "j" || e.key === "ArrowRight") go(nav?.next ?? null);
      else if (e.key === "k" && e.shiftKey) go(nav?.previous ?? nav?.prev ?? null);
      else if (e.key === "ArrowLeft") go(nav?.previous ?? nav?.prev ?? null);
      else {
        const a = ACTIONS.find((x) => x.key === e.key.toLowerCase());
        if (a && !e.shiftKey) decide(a.action);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [nav, go, decide]);

  if (error) return <EmptyState title="Case not found">{error.message}</EmptyState>;
  if (!c) return <Loading />;

  const pro = c.evidence.filter((e: any) => e.stance === "prosecution");
  const def = c.evidence.filter((e: any) => e.stance === "defense");
  const neutral = c.evidence.filter((e: any) => e.stance === "neutral");
  const explanation = expl ?? c.explanation;
  const matched = c.verdict_record?.rule_trace?.find((r: any) => r.matched);
  const active = c.decisions.filter((d: any) => !d.undone_at);

  const runExplain = async () => {
    setExplBusy(true);
    try {
      setExpl(await api(`/cases/${caseId}/explain`, { method: "POST" }));
    } catch (e: any) {
      setMsg({ tone: "bad", text: e.message });
    } finally {
      setExplBusy(false);
    }
  };
  const undo = async (id: string) => {
    try {
      await api(`/decisions/${id}/undo`, { method: "POST" });
      mutate();
    } catch (e: any) {
      setMsg({ tone: "bad", text: e.message });
    }
  };
  const launchWhatIf = async (option: any) => {
    const actions =
      option.action === "remove"
        ? [{ type: "remove_samples", sample_ids: [c.sample.id] }]
        : [{ type: "relabel_samples", items: [{ sample_id: c.sample.id, target_class: option.target }] }];
    const r = await api("/what-if", { json: { dataset_version_id: versionId, name: `Case #${c.case_number}: ${option.action}${option.target ? ` → ${option.target}` : ""}`, actions, seeds: 3, source: "case" } });
    router.push(`/app/datasets/${versionId}/what-if?run=${r.id}`);
  };

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="text-xs font-medium uppercase tracking-wider text-subtle">
            <Link href={`/app/datasets/${versionId}/${session ? "review" : "court"}`} className="hover:text-ink">
              {session ? session.name : "Court"}
            </Link>
          </div>
          <h1 className="flex flex-wrap items-center gap-2 font-display text-[28px]">
            Case #{c.case_number}
            <VerdictBadge verdict={c.verdict} />
            <StatusBadge status={c.status} />
          </h1>
          <p className="text-sm text-muted">
            {humanize(c.primary_concern)} · priority {fixed(c.priority, 2)} · {humanize(c.uncertainty)} uncertainty <InfoTip term="uncertainty" /> · ~{fixed(c.est_review_minutes, 1)} min
          </p>
        </div>
        <div className="flex items-center gap-2">
          <span className="hidden items-center gap-1 text-xs text-subtle md:flex">
            <Keyboard className="size-3.5" aria-hidden /> K keep · R relabel · X remove · U unsure · E escalate · J / ← →
          </span>
          <Button size="sm" onClick={() => go(nav?.previous ?? nav?.prev ?? null)} disabled={!(nav?.previous ?? nav?.prev)} aria-label="Previous case">
            <ChevronLeft className="size-4" aria-hidden />
          </Button>
          <span className="text-xs tabular text-muted">
            {nav?.position} / {nav?.total}
          </span>
          <Button size="sm" onClick={() => go(nav?.next ?? null)} disabled={!nav?.next} aria-label="Next case">
            <ChevronRight className="size-4" aria-hidden />
          </Button>
        </div>
      </div>

      <div className="grid gap-5 xl:grid-cols-[340px_1fr]">
        {/* ---------------- exhibit */}
        <div className="space-y-4">
          <Card>
            <div className="thumb-checker overflow-hidden rounded-lg border hairline">
              <img src={c.sample.image_url} alt={`Exhibit: ${c.sample.label} — ${c.sample.name}`} className="max-h-[360px] w-full object-contain" />
            </div>
            <div className="mt-3 space-y-1.5 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-muted">Current label</span>
                <Badge tone="accent">{c.sample.label}</Badge>
                <SplitTag split={c.sample.split} />
              </div>
              <div className="truncate font-mono text-[11px] text-subtle" title={c.sample.path}>
                {c.sample.path}
              </div>
              <div className="text-xs text-muted">
                {c.sample.width}×{c.sample.height} · {c.sample.format} · sha {c.sample.sha256.slice(0, 12)}…
              </div>
              {c.prediction && (
                <div className="pt-1">
                  <div className="mb-0.5 flex items-center gap-1.5 text-xs text-muted">
                    Baseline model <KindTag kind="model_prediction" />
                  </div>
                  {c.prediction.top_probs.map(([k, p]: [string, number]) => (
                    <div key={k} className="grid grid-cols-[1fr_auto] gap-2 text-xs">
                      <span className={k === c.sample.label ? "font-medium" : undefined}>{k}</span>
                      <span className="tabular">{pct(p, 1)}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </Card>
          {c.neighbors.length > 0 && (
            <Card title="Nearest neighbours" info="neighbor_agreement">
              <ul className="grid grid-cols-4 gap-2">
                {c.neighbors.map((n: any) => (
                  <li key={n.sample.id}>
                    <SampleThumb s={n.sample} size={68} caption={false} />
                    <div className={cx("mt-0.5 truncate text-[10.5px]", n.sample.label === c.sample.label ? "text-muted" : "font-medium text-bad")} title={n.sample.label}>
                      {n.sample.label}
                    </div>
                    <div className="text-[10px] tabular text-subtle">cos {fixed(n.similarity, 3)}</div>
                  </li>
                ))}
              </ul>
            </Card>
          )}
          {c.family_members.length > 0 && (
            <Card title="Duplicate family members">
              <ul className="grid grid-cols-3 gap-2">
                {c.family_members.map((m: any) => (
                  <li key={m.sample.id}>
                    <SampleThumb s={m.sample} size={88} />
                    <div className="text-[10.5px] text-muted">{m.is_root ? "source-like" : humanize(m.relation)}</div>
                  </li>
                ))}
              </ul>
            </Card>
          )}
        </div>

        {/* ---------------- trial */}
        <div className="min-w-0 space-y-4">
          <div className="grid gap-4 lg:grid-cols-2">
            <Card title={<span className="text-bad">Prosecution</span>} subtitle="Evidence against the current label, split or presence in training">
              <EvidenceList items={pro} empty="No witness argues against the current data state." />
            </Card>
            <Card title={<span className="text-ok">Defense</span>} subtitle="Evidence for keeping the sample as it is">
              <EvidenceList items={def} empty="No witness supports the current data state." />
            </Card>
          </div>
          {neutral.length > 0 && (
            <Card title="Other witnesses (neutral or discounted)">
              <EvidenceList items={neutral} empty="" />
            </Card>
          )}

          <Card
            title={
              <span className="flex items-center gap-2">
                <Gavel className="size-4" aria-hidden /> Jury verdict <InfoTip term="verdict" />
              </span>
            }
            subtitle={`Deterministic rules · ${c.verdict_record?.jury_version} · config ${c.verdict_record?.config_hash?.slice(0, 12)}… · no LLM involved`}
          >
            <div className="flex flex-wrap items-center gap-2">
              <VerdictBadge verdict={c.verdict} />
              {c.verdict_record?.scores?.target_label && <span className="text-sm">suggested label: <b>{c.verdict_record.scores.target_label}</b></span>}
              {matched && <span className="text-xs text-muted">matched rule {matched.rule}</span>}
            </div>
            <div className="mt-3 flex flex-wrap gap-1.5" aria-label="Reason codes">
              {c.reason_codes.map((r: string) => (
                <code key={r} className="rounded bg-surface-2 px-1.5 py-0.5 font-mono text-[10.5px]">
                  {r}
                </code>
              ))}
            </div>
            <details className="mt-3">
              <summary className="cursor-pointer text-xs font-medium text-muted">Rule trace & scores</summary>
              <ol className="mt-2 space-y-0.5 font-mono text-[11px]">
                {c.verdict_record?.rule_trace?.map((r: any) => (
                  <li key={r.rule} className={r.matched ? "font-semibold text-ink" : r.evaluated ? "text-muted" : "text-subtle line-through"}>
                    {r.matched ? "▶ " : "  "}
                    {r.rule} {r.evaluated ? (r.matched ? "— matched" : "— no") : "— not evaluated"}
                  </li>
                ))}
              </ol>
              <pre className="mt-2 rounded bg-surface-2 p-2 font-mono text-[11px]">{JSON.stringify(c.verdict_record?.scores, null, 1)}</pre>
            </details>
          </Card>

          <Card
            title={
              <span className="flex items-center gap-2">
                <Sparkles className="size-4" aria-hidden /> Narrative
              </span>
            }
            subtitle="Optional plain-language explanation generated from the structured evidence above. It never changes the verdict."
            actions={
              <Button size="sm" onClick={runExplain} loading={explBusy}>
                {explanation ? "Regenerate" : "Explain this case"}
              </Button>
            }
          >
            {explanation ? (
              <div className="space-y-3 text-sm">
                <KindTag kind={explanation.source === "gemini" ? "llm" : "heuristic"} />
                {explanation.source !== "gemini" && <span className="ml-2 text-xs text-muted">{explanation.content.note ?? "deterministic template"}</span>}
                {explanation.source === "gemini" && <span className="ml-2 text-xs text-muted">{explanation.model}</span>}
                <p>
                  <b className="text-bad">Prosecutor.</b> {explanation.content.prosecutor}
                </p>
                <p>
                  <b className="text-ok">Defense.</b> {explanation.content.defense}
                </p>
                <p className="text-muted">{explanation.content.plain_summary}</p>
                <Notice tone="info" title="Question for the reviewer">
                  {explanation.content.reviewer_question}
                </Notice>
                {explanation.content.unverified_numbers && Object.keys(explanation.content.unverified_numbers).length > 0 && (
                  <Notice tone="warn" title="Unverified numbers">
                    The model mentioned numbers not found in the evidence: {JSON.stringify(explanation.content.unverified_numbers)}. Treat them as unreliable.
                  </Notice>
                )}
              </div>
            ) : (
              <p className="text-sm text-muted">Only structured evidence is sent (no images, file paths or notes).</p>
            )}
          </Card>

          {c.rare_or_wrong && (
            <Card title={`Rare or wrong? Leading hypothesis: ${humanize(c.rare_or_wrong.hypothesis)}`}>
              <div className="grid gap-4 md:grid-cols-2 text-xs">
                <ul className="space-y-1">
                  {Object.entries(c.rare_or_wrong.scores as Record<string, number>)
                    .sort((a, b) => b[1] - a[1])
                    .map(([k, v]) => (
                      <li key={k} className="grid grid-cols-[110px_1fr_34px] items-center gap-2">
                        <span>{humanize(k)}</span>
                        <span className="h-1.5 rounded-full bg-surface-3">
                          <span className="block h-full rounded-full bg-accent" style={{ width: `${v * 100}%` }} />
                        </span>
                        <span className="tabular">{fixed(v, 2)}</span>
                      </li>
                    ))}
                </ul>
                <div className="space-y-2">
                  {c.rare_or_wrong.reasons.map((r: string) => (
                    <p key={r} className="text-muted">
                      • {r}
                    </p>
                  ))}
                  {c.rare_or_wrong.valuable_reasons.length > 0 && (
                    <div className="rounded-md bg-accent-soft p-2 text-accent">
                      {c.rare_or_wrong.valuable_reasons.map((r: string) => (
                        <p key={r}>• {r}</p>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            </Card>
          )}

          <Card
            title="What should I change? (counterfactual estimates)"
            actions={
              !showCf && (
                <Button size="sm" onClick={() => setShowCf(true)}>
                  Estimate options
                </Button>
              )
            }
          >
            {!showCf ? (
              <p className="text-sm text-muted">Fast first-order estimates of how keep / remove / relabel would move evaluation loss, without retraining.</p>
            ) : !cf ? (
              <Loading rows={1} />
            ) : (
              <div className="space-y-3 text-sm">
                {cf.available ? (
                  <>
                    <table className="w-full text-sm">
                      <tbody>
                        {cf.options.map((o: any, i: number) => (
                          <tr key={i} className={cx("border-t hairline", o === cf.best_estimated && "bg-accent-soft")}>
                            <td className="py-1.5 pr-2">
                              {humanize(o.action)}
                              {o.target ? ` → ${o.target}` : ""}
                            </td>
                            <td className={cx("py-1.5 pr-2 tabular", o.delta_eval_loss < 0 ? "text-ok" : o.delta_eval_loss > 0 ? "text-bad" : "")}>{signed(o.delta_eval_loss, 5)} Δ loss</td>
                            <td className="py-1.5 text-right">
                              {o.action !== "keep" && canWrite && (
                                <Button size="sm" variant="ghost" onClick={() => launchWhatIf(o)}>
                                  Verify in What-if →
                                </Button>
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    <p className="text-xs text-subtle">
                      <KindTag kind="model_estimate" /> {cf.caveat} Negative Δ = evaluation loss expected to fall.
                    </p>
                  </>
                ) : (
                  <Notice>{cf.reason}</Notice>
                )}
                {cf.qualitative?.map((q: any) => (
                  <p key={q.action} className="text-xs text-muted">
                    <b>{humanize(q.action)}:</b> {q.note}
                  </p>
                ))}
              </div>
            )}
          </Card>

          <Card title="Human decision" subtitle="The human decision is final. Decisions are recorded with the evidence snapshot in the ledger and can be undone until exported.">
            {canWrite ? (
              <div className="space-y-3">
                <div className="flex flex-wrap items-center gap-2">
                  {ACTIONS.map((a) => (
                    <Button key={a.action} variant={a.action === "remove" ? "danger" : a.action === "keep" ? "primary" : "secondary"} size="sm" onClick={() => decide(a.action)} disabled={busy || (a.action === "relabel" && !target)}>
                      {a.label} <kbd className="ml-1 rounded border border-current/30 px-1 font-mono text-[10px] opacity-70">{a.hint}</kbd>
                    </Button>
                  ))}
                  <Select label="Relabel target" value={target} onChange={setTarget} options={c.classes.filter((x: any) => x.name !== c.sample.label).map((x: any) => ({ value: x.id, label: `→ ${x.name}` }))} />
                </div>
                <textarea value={note} onChange={(e) => setNote(e.target.value)} rows={2} placeholder="Reason / note (recommended)" aria-label="Decision note" className={inputCls} />
                {isAdmin && (c.status === "disputed" || active.length > 0) && (
                  <label className="flex items-center gap-2 text-sm">
                    <input type="checkbox" checked={adjudicate} onChange={(e) => setAdjudicate(e.target.checked)} /> Record as expert adjudication (resolves disputes)
                  </label>
                )}
                {c.status === "disputed" && <Notice tone="warn">Reviewers disagree on this case. An owner or admin can adjudicate.</Notice>}
              </div>
            ) : (
              <Notice>Read-only access: decisions require the reviewer role.</Notice>
            )}
            {msg && <div className="mt-3"><Notice tone={msg.tone}>{msg.text}</Notice></div>}
            {c.decisions.length > 0 && (
              <ul className="mt-4 space-y-1.5 border-t hairline pt-3 text-sm">
                {c.decisions.map((d: any) => (
                  <li key={d.id} className={cx("flex flex-wrap items-center gap-2", d.undone_at && "opacity-50 line-through")}>
                    <KindTag kind="human" />
                    <b>{humanize(d.action)}</b>
                    {d.target_class && <span>→ {d.target_class}</span>}
                    {d.adjudication && <Badge tone="accent">adjudication</Badge>}
                    <span className="text-xs text-muted">
                      {d.reviewer} · {ago(d.created_at)}
                    </span>
                    {d.note && <span className="text-xs text-muted">“{d.note}”</span>}
                    {!d.undone_at && canWrite && (
                      <button onClick={() => undo(d.id)} className="ml-auto inline-flex items-center gap-1 text-xs text-muted hover:text-ink">
                        <Undo2 className="size-3" aria-hidden /> undo
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>
      </div>
    </div>
  );
}

function EvidenceList({ items, empty }: { items: any[]; empty: string }) {
  if (!items.length) return <p className="text-sm text-muted">{empty}</p>;
  return (
    <ul className="space-y-3">
      {items.map((e) => (
        <li key={e.id} className="text-sm">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-xs font-medium text-subtle">{WITNESS_LABEL[e.witness] ?? e.witness}</span>
            <KindTag kind={e.evidence_kind} />
          </div>
          <div className="mt-0.5 font-medium">{e.title}</div>
          <p className="text-xs leading-relaxed text-muted">{e.detail}</p>
        </li>
      ))}
    </ul>
  );
}

export default function CasePage() {
  const { versionId, caseId } = useParams<{ versionId: string; caseId: string }>();
  return (
    <RequireAudit>
      <Suspense>
        <CaseInner key={caseId} versionId={versionId} caseId={caseId} />
      </Suspense>
    </RequireAudit>
  );
}
