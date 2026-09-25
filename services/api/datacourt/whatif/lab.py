"""What-if Lab (`what-if-v1`): isolated before/after experiments on proposed data changes.

Nothing here mutates the dataset. A change set is resolved to (removed training samples,
relabelled samples, evaluation samples dropped). Both variants are trained with the same
baseline trainer, config and seeds on the audit's frozen embeddings, then evaluated on
the *same* evaluation sets:

  original_eval      evaluation split exactly as uploaded (labels as given)
  preserved_holdout  evaluation samples untouched by any action and not involved in
                     leakage — the fairest before/after comparison
  cleaned_eval       evaluation split with leaked copies removed and proposed relabels
                     applied — depends on the proposed labels being right

Point estimates come from converged (L-BFGS) fits of both variants. Training-data
uncertainty comes from a paired Poisson bootstrap (each original sample gets the same
weight in both variants); a difference is flagged `within_noise` when |Δ| ≤ 2 × the std
of Δ across replicates. A paired evaluation bootstrap gives a 95% CI for the macro-F1
difference on the preserved holdout. These are experimental results on this evaluation
setup, not guarantees about production performance.
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import UTC, datetime
from typing import Any

import numpy as np
from sklearn.metrics import f1_score
from sqlalchemy import select

from datacourt import algorithms, jobs, ledger
from datacourt.db import models as m
from datacourt.db.session import new_session
from datacourt.enums import Risk, RunStatus, Severity
from datacourt.ml import baseline as bl
from datacourt.pipeline.context import AuditContext, load_samples
from datacourt.whatif.actions import (  # noqa: F401 - re-exported for callers
    ACTION_TYPES,
    RISK_RANK,
    SEV_RANK,
    InvalidChangeSet,
    validate_actions,
)

VERSION = algorithms.WHAT_IF


def resolve(s, audit_id: uuid.UUID, S, actions: list[dict], seed: int) -> dict[str, Any]:
    pos = {sid: i for i, sid in enumerate(S.ids)}
    cls_idx = {n: i for i, n in enumerate(S.class_names)}
    remove: set[int] = set()
    relabel: dict[int, int] = {}
    eval_drop: set[int] = set()
    protect: set[int] = set()
    class_balanced: bool | None = None
    max_per_class: int | None = None
    affected: list[dict] = []
    is_eval = {i for i, sp in enumerate(S.split) if sp in ("val", "test")}

    def add_remove(idx: set[int]) -> int:
        n0 = len(remove) + len(eval_drop)
        for i in idx:
            (eval_drop if i in is_eval else remove).add(i)
        return len(remove) + len(eval_drop) - n0

    cases = s.scalars(select(m.CourtCase).where(m.CourtCase.audit_run_id == audit_id)).all()
    for a in actions:
        t = a["type"]
        n = 0
        if t == "remove_samples":
            n = add_remove({pos[uuid.UUID(x)] for x in a["sample_ids"] if uuid.UUID(x) in pos})
        elif t == "relabel_samples":
            for it in a["items"]:
                sid = uuid.UUID(it["sample_id"])
                if (
                    sid in pos
                    and it.get("target_class") in cls_idx
                    and cls_idx[it["target_class"]] != S.labels[pos[sid]]
                ):
                    relabel[pos[sid]] = cls_idx[it["target_class"]]
                    n += 1
        elif t == "remove_duplicate_copies":
            fams = s.scalars(
                select(m.DuplicateFamily).where(m.DuplicateFamily.audit_run_id == audit_id)
            ).all()
            wanted = set(a.get("family_ids") or []) or None
            idx = set()
            for f in fams:
                if wanted and str(f.id) not in wanted:
                    continue
                mem = s.scalars(
                    select(m.DuplicateFamilyMember.sample_id).where(m.DuplicateFamilyMember.family_id == f.id)
                ).all()
                idx |= {pos[x] for x in mem if x != f.root_sample_id and pos[x] not in is_eval}
            n = add_remove(idx)
        elif t == "exclude_quality":
            thr = SEV_RANK.get(a.get("min_severity", "high"), 3)
            rows = s.execute(
                select(m.QualityFinding.sample_id, m.QualityFinding.severity).where(
                    m.QualityFinding.audit_run_id == audit_id
                )
            ).all()
            n = add_remove(
                {pos[sid] for sid, sev in rows if SEV_RANK[str(sev)] >= thr and pos[sid] not in is_eval}
            )
        elif t == "rebalance":
            if isinstance(a.get("max_per_class"), int):
                max_per_class = max(1, int(a["max_per_class"]))
            if isinstance(a.get("class_balanced"), bool):
                class_balanced = a["class_balanced"]
            n = -1
        elif t == "preserve_rare":
            protect |= {pos[c.sample_id] for c in cases if str(c.verdict) == "LIKELY_RARE"}
            protect |= {
                pos[sid]
                for sid, h in s.execute(
                    select(m.RareWrongFinding.sample_id, m.RareWrongFinding.hypothesis).where(
                        m.RareWrongFinding.audit_run_id == audit_id
                    )
                ).all()
                if str(h) == "rare_valid"
            }
            n = len(protect)
        elif t == "move_leakage_out_of_eval":
            thr = RISK_RANK.get(a.get("min_risk", "medium"), 1)
            idx = set()
            for lk in s.scalars(select(m.LeakageFinding).where(m.LeakageFinding.audit_run_id == audit_id)):
                if RISK_RANK[str(lk.risk)] >= thr:
                    idx |= {pos[uuid.UUID(x)] for x in lk.sample_ids if pos.get(uuid.UUID(x)) in is_eval}
            eval_drop |= idx
            n = len(idx)
        elif t == "apply_review_decisions":
            from datacourt.services.review import final_decision

            cid = {
                c.id: c
                for c in s.scalars(
                    select(m.DatasetClass).where(m.DatasetClass.dataset_version_id == S_version(s, audit_id))
                )
            }
            for c in cases:
                d = final_decision(s, c.id)
                if d is None:
                    continue
                i = pos[c.sample_id]
                if str(d.action) == "remove":
                    n += add_remove({i})
                elif str(d.action) == "relabel" and d.target_class_id in cid:
                    relabel[i] = cls_idx[cid[d.target_class_id].name]
                    n += 1
        elif t == "apply_jury_suggestions":
            verdicts = set(a.get("verdicts") or ["POSSIBLE_RELABEL", "POSSIBLE_REMOVE"])
            vrows = {
                v.case_id: v
                for v in s.scalars(
                    select(m.CaseVerdict).where(m.CaseVerdict.case_id.in_([c.id for c in cases]))
                )
            }
            for c in cases:
                i = pos[c.sample_id]
                if str(c.verdict) not in verdicts:
                    continue
                if str(c.verdict) == "POSSIBLE_RELABEL":
                    tgt = (vrows.get(c.id).scores or {}).get("target_label") if c.id in vrows else None
                    if tgt in cls_idx:
                        relabel[i] = cls_idx[tgt]
                        n += 1
                elif str(c.verdict) == "POSSIBLE_REMOVE":
                    n += add_remove({i})
        affected.append({"type": t, "affected": n})
    removed_protected = sorted((remove | eval_drop) & protect)
    remove -= protect
    eval_drop -= protect
    for i in protect:
        relabel.pop(i, None)
    return {
        "remove": remove,
        "relabel": relabel,
        "eval_drop": eval_drop,
        "class_balanced": class_balanced,
        "max_per_class": max_per_class,
        "affected": affected,
        "protected_from_removal": len(removed_protected),
    }


def S_version(s, audit_id: uuid.UUID) -> uuid.UUID:
    a = s.get(m.AuditRun, audit_id)
    assert a is not None
    return a.dataset_version_id


def _fit_probs(
    emb: np.ndarray,
    idx: np.ndarray,
    y: np.ndarray,
    n_classes: int,
    C: float,
    balanced: bool,
    weights: np.ndarray | None = None,
) -> np.ndarray:
    mdl = bl.fit_logreg(emb[idx], y[idx], n_classes, C=C, class_balanced=balanced, sample_weight=weights)
    return bl.softmax(mdl.logits(emb))


KEY_METRICS = ("accuracy", "macro_f1", "balanced_accuracy")


def _bootstrap_delta(
    y: np.ndarray, p_before: np.ndarray, p_after: np.ndarray, seed: int, n_boot: int = 1000
) -> dict | None:
    if len(y) < 20:
        return None
    rng = np.random.default_rng(seed)
    a, b = p_before.argmax(1), p_after.argmax(1)
    labels = sorted(set(y.tolist()))
    deltas = []
    for _ in range(n_boot):
        idx = rng.integers(0, len(y), len(y))
        fa = f1_score(y[idx], a[idx], labels=labels, average="macro", zero_division=0)
        fb = f1_score(y[idx], b[idx], labels=labels, average="macro", zero_division=0)
        deltas.append(fb - fa)
    lo, hi = np.percentile(deltas, [2.5, 97.5])
    return {
        "metric": "macro_f1",
        "ci95": [round(float(lo), 5), round(float(hi), 5)],
        "resamples": n_boot,
        "excludes_zero": bool(lo > 0 or hi < 0),
    }


def run_experiment(
    audit_id: uuid.UUID,
    actions: list[dict],
    seeds: int = 3,
    failure_sample_id: uuid.UUID | None = None,
    report=None,
) -> dict[str, Any]:
    with new_session() as s:
        audit = s.get(m.AuditRun, audit_id)
        assert audit is not None
        version = s.get(m.DatasetVersion, audit.dataset_version_id)
        assert version is not None
        S = load_samples(version.id)
        ctx = AuditContext(
            audit.id, audit.org_id, version.id, version.dataset_id, str(audit.profile), audit.config, S
        )
        if not ctx.has_artifact("embeddings"):
            raise jobs.PermanentJobError("audit has no embeddings; run an audit first")
        res = resolve(s, audit_id, S, actions, audit.config["seed"])
        leak_eval: set[int] = set()
        pos = {sid: i for i, sid in enumerate(S.ids)}
        for lk in s.scalars(select(m.LeakageFinding).where(m.LeakageFinding.audit_run_id == audit_id)):
            if lk.risk != Risk.LOW:
                leak_eval |= {pos[uuid.UUID(x)] for x in lk.sample_ids if uuid.UUID(x) in pos}
    emb = ctx.load_npz("embeddings")["emb"].astype(np.float64)
    split = np.array(S.split)
    fit_mask = np.isin(split, ["train", "unsplit"])
    eval_mask = np.isin(split, ["val", "test"])
    seed0 = int(audit.config["seed"])
    if not eval_mask.any():
        # No evaluation split: hold out a deterministic stratified 20% of the fit set, never modified.
        rng = np.random.default_rng(seed0 + 99)
        hold = np.zeros(S.n, bool)
        for c in range(S.n_classes):
            idx = np.nonzero(fit_mask & (S.labels == c))[0]
            if len(idx) >= 5:
                hold[rng.choice(idx, size=max(1, len(idx) // 5), replace=False)] = True
        eval_mask, fit_mask = hold, fit_mask & ~hold
        res["remove"] -= set(np.nonzero(hold)[0].tolist())
    fit_idx = np.nonzero(fit_mask)[0]
    ev_idx = np.nonzero(eval_mask)[0]
    y_given = S.labels.copy()
    y_after = S.labels.copy()
    for i, c in res["relabel"].items():
        y_after[i] = c
    after_idx = np.array([i for i in fit_idx if i not in res["remove"]], dtype=int)
    if res["max_per_class"]:
        rng = np.random.default_rng(seed0 + 7)
        keep = []
        for c in range(S.n_classes):
            idx = after_idx[y_after[after_idx] == c]
            if len(idx) > res["max_per_class"]:
                idx = np.sort(rng.choice(idx, size=res["max_per_class"], replace=False))
            keep.extend(idx.tolist())
        after_idx = np.array(sorted(keep), dtype=int)
    if len(set(y_after[after_idx].tolist())) < 2:
        raise jobs.PermanentJobError("change set leaves fewer than two classes in training")
    touched = set(res["remove"]) | set(res["relabel"]) | set(res["eval_drop"])
    sets = {
        "original_eval": (ev_idx, y_given),
        "preserved_holdout": (
            np.array([i for i in ev_idx if i not in touched and i not in leak_eval], dtype=int),
            y_given,
        ),
        "cleaned_eval": (np.array([i for i in ev_idx if i not in res["eval_drop"]], dtype=int), y_after),
    }
    bc = audit.config["baseline"]
    C = float(bc.get("C", 0.001))
    bal_before = bool(bc["class_balanced"])
    bal_after = bal_before if res["class_balanced"] is None else bool(res["class_balanced"])
    replicates = max(0, int(seeds))
    # Point estimate: converged fits on the full before/after training sets (deterministic).
    point = {
        "before": _fit_probs(emb, fit_idx, y_given, S.n_classes, C, bal_before),
        "after": _fit_probs(emb, after_idx, y_after, S.n_classes, C, bal_after),
    }
    if report:
        report(0.2)
    # Uncertainty: paired Poisson bootstrap of the training data (same weight per original sample in both variants).
    rng = np.random.default_rng(seed0 + 1000)
    reps: list[dict[str, np.ndarray]] = []
    for r in range(replicates):
        w = rng.poisson(1.0, S.n).astype(np.float64)
        reps.append(
            {
                "before": _fit_probs(emb, fit_idx, y_given, S.n_classes, C, bal_before, w[fit_idx]),
                "after": _fit_probs(emb, after_idx, y_after, S.n_classes, C, bal_after, w[after_idx]),
            }
        )
        if report:
            report(0.2 + 0.7 * (r + 1) / max(1, replicates))
    results: dict[str, dict[str, Any]] = {"before": {}, "after": {}}
    deltas: dict[str, dict] = {}
    for name, (idx, y) in sets.items():
        if len(idx) == 0:
            continue
        pm = {v: bl.classification_metrics(y[idx], point[v][idx], S.class_names) for v in ("before", "after")}
        rep_m = [
            {v: bl.classification_metrics(y[idx], rp[v][idx], S.class_names) for v in ("before", "after")}
            for rp in reps
        ]
        for v in ("before", "after"):
            results[v][name] = {
                "n": int(len(idx)),
                "metrics": pm[v],
                "bootstrap": {
                    k: {
                        "mean": round(float(np.mean([x[v][k] for x in rep_m])), 5) if rep_m else None,
                        "std": round(float(np.std([x[v][k] for x in rep_m])), 5) if rep_m else None,
                    }
                    for k in KEY_METRICS
                },
            }
        deltas[name] = {}
        for k in KEY_METRICS:
            d_point = pm["after"][k] - pm["before"][k]
            d_reps = [x["after"][k] - x["before"][k] for x in rep_m]
            std = float(np.std(d_reps)) if d_reps else 0.0
            deltas[name][k] = {
                "before": pm["before"][k],
                "after": pm["after"][k],
                "delta": round(d_point, 5),
                "bootstrap_std": round(std, 5),
                "bootstrap_range": [round(float(min(d_reps)), 5), round(float(max(d_reps)), 5)]
                if d_reps
                else None,
                "within_noise": bool(d_reps) and abs(d_point) <= 2 * std,
            }
    summary: dict[str, Any] = {
        "algorithm": VERSION,
        "model": f"{bl.VERSION} (L-BFGS, C={C})",
        "training_bootstrap_replicates": replicates,
        "training_samples": {"before": int(len(fit_idx)), "after": int(len(after_idx))},
        "changes": {
            "removed_from_training": len(res["remove"]),
            "relabelled": len(res["relabel"]),
            "dropped_from_evaluation": len(res["eval_drop"]),
            "protected_rare": res["protected_from_removal"],
            "per_action": res["affected"],
            "class_counts_after": {
                S.class_names[c]: int(v) for c, v in Counter(y_after[after_idx].tolist()).items()
            },
        },
        "eval_sets": {k: int(len(v[0])) for k, v in sets.items()},
        "deltas": deltas,
        "noise_note": "within_noise = |Δ| ≤ 2 × std of Δ across paired training-data bootstrap replicates.",
        "label": "Experimental result on this evaluation setup — not a guaranteed production improvement.",
    }
    ph_idx, _ = sets["preserved_holdout"]
    if len(ph_idx):
        summary["preserved_holdout_ci"] = _bootstrap_delta(
            y_given[ph_idx], point["before"][ph_idx], point["after"][ph_idx], seed0
        )
    probs = {v: [point[v]] for v in ("before", "after")}
    if failure_sample_id is not None and failure_sample_id in pos:
        i = pos[failure_sample_id]
        pb = np.mean([p[i] for p in probs["before"]], 0)
        pa = np.mean([p[i] for p in probs["after"]], 0)
        summary["failure"] = {
            "sample_id": str(failure_sample_id),
            "true_label": S.class_names[y_given[i]],
            "before": {
                "predicted": S.class_names[int(pb.argmax())],
                "p_true": round(float(pb[y_given[i]]), 4),
            },
            "after": {
                "predicted": S.class_names[int(pa.argmax())],
                "p_true": round(float(pa[y_given[i]]), 4),
            },
            "fixed": bool(pa.argmax() == y_given[i] and pb.argmax() != y_given[i]),
        }
    return {"results": results, "summary": summary, "resolved": res}


def run_what_if_job(ctx) -> dict:
    run_id = uuid.UUID(ctx.payload["what_if_run_id"])
    with new_session() as s:
        run = s.get(m.WhatIfRun, run_id)
        if run is None:
            raise jobs.PermanentJobError("what-if run not found")
        run.status = RunStatus.RUNNING
        actions = [
            {"type": a.action_type, **a.params}
            for a in s.scalars(select(m.WhatIfAction).where(m.WhatIfAction.run_id == run_id))
        ]
        audit_id, seeds = run.audit_run_id, int(run.config.get("seeds", 3))
        failure_sample = None
        if run.failure_event_id:
            fe = s.get(m.FailureEvent, run.failure_event_id)
            failure_sample = fe.sample_id if fe else None
        s.commit()
    out = run_experiment(
        audit_id,
        actions,
        seeds=seeds,
        failure_sample_id=failure_sample,
        report=lambda f: ctx.report(f, "WHAT_IF"),
    )
    with new_session() as s:
        run = s.get(m.WhatIfRun, run_id)
        assert run is not None
        s.query(m.WhatIfResult).filter(m.WhatIfResult.run_id == run_id).delete()
        for variant, sets in out["results"].items():
            for name, metrics in sets.items():
                s.add(m.WhatIfResult(run_id=run_id, variant=variant, eval_set=name, metrics=metrics))
        for a, info in zip(
            s.scalars(select(m.WhatIfAction).where(m.WhatIfAction.run_id == run_id)).all(),
            out["summary"]["changes"]["per_action"],
            strict=False,
        ):
            a.affected_count = max(0, int(info["affected"]))
        run.summary = out["summary"]
        run.status = RunStatus.COMPLETED
        run.finished_at = datetime.now(UTC)
        ledger.record(
            s,
            org_id=run.org_id,
            event_type="what_if.completed",
            entity_type="what_if_run",
            entity_id=run.id,
            actor_id=run.created_by,
            dataset_version_id=run.dataset_version_id,
            payload={
                "actions": actions,
                "seeds": seeds,
                "deltas": out["summary"]["deltas"],
                "ci": out["summary"].get("preserved_holdout_ci"),
                "algorithm": VERSION,
            },
        )
        s.commit()
    return {"what_if_run_id": str(run_id)}


__all__ = [
    "ACTION_TYPES",
    "InvalidChangeSet",
    "Severity",
    "run_experiment",
    "run_what_if_job",
    "validate_actions",
]
