"""Benchmark evaluation: compares an audit's outputs with the generator's ground truth.

All metrics are computed from what the pipeline actually stored — no cherry-picking.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from itertools import combinations

import numpy as np
from sklearn.metrics import average_precision_score
from sqlalchemy import select
from sqlalchemy.orm import Session

from datacourt.db import models as m
from datacourt.enums import Risk, Severity


def _groups(pairs: list[tuple[str, str]]) -> list[set[str]]:
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        parent[find(a)] = find(b)
    out: dict[str, set[str]] = defaultdict(set)
    for x in list(parent):
        out[find(x)].add(x)
    return [g for g in out.values() if len(g) > 1]


def _pairs_of(groups: list[set[str]]) -> set[frozenset]:
    return {frozenset(p) for g in groups for p in combinations(sorted(g), 2)}


def _pr(pred: set, truth: set) -> dict:
    tp = len(pred & truth)
    return {
        "precision": round(tp / len(pred), 4) if pred else None,
        "recall": round(tp / len(truth), 4) if truth else None,
        "tp": tp,
        "predicted": len(pred),
        "truth": len(truth),
    }


def evaluate(s: Session, audit_id: uuid.UUID, gt: dict) -> dict:
    audit = s.get(m.AuditRun, audit_id)
    assert audit is not None
    samples = s.scalars(select(m.Sample).where(m.Sample.dataset_version_id == audit.dataset_version_id)).all()
    path_of = {x.id: x.relative_path for x in samples}
    split_of = {x.relative_path: str(x.split) for x in samples}
    id_of = {x.relative_path: x.id for x in samples}
    classes = {
        c.id: c.name
        for c in s.scalars(
            select(m.DatasetClass).where(m.DatasetClass.dataset_version_id == audit.dataset_version_id)
        )
    }
    present = set(id_of)
    res: dict = {
        "audit_run_id": str(audit_id),
        "profile": str(audit.profile),
        "embedding_model": audit.embedding_model,
        "samples": len(samples),
    }

    # ---- duplicates -------------------------------------------------------------
    gt_pairs = [tuple(p) for p in gt["exact_duplicates"] + gt["cross_split_exact"]]
    gt_pairs += [(d["original"], d["copy"]) for d in gt["near_duplicates"] + gt["cross_split_variant"]]
    gt_pairs = [p for p in gt_pairs if p[0] in present and p[1] in present]
    gt_groups = _groups(gt_pairs)
    fam_members: dict[uuid.UUID, set[str]] = defaultdict(set)
    for fid, sid in s.execute(
        select(m.DuplicateFamilyMember.family_id, m.DuplicateFamilyMember.sample_id)
        .join(m.DuplicateFamily, m.DuplicateFamily.id == m.DuplicateFamilyMember.family_id)
        .where(m.DuplicateFamily.audit_run_id == audit_id)
    ):
        fam_members[fid].add(path_of[sid])
    pred_groups = list(fam_members.values())
    res["duplicates"] = {"pairs": _pr(_pairs_of(pred_groups), _pairs_of(gt_groups))}
    group_of = {p: i for i, g in enumerate(gt_groups) for p in g}
    edges = s.execute(
        select(m.SimilarityEdge.sample_a_id, m.SimilarityEdge.sample_b_id, m.SimilarityEdge.relation).where(
            m.SimilarityEdge.audit_run_id == audit_id
        )
    ).all()
    by_rel: dict[str, list[bool]] = defaultdict(list)
    for a, b, rel in edges:
        pa, pb = path_of[a], path_of[b]
        by_rel[str(rel)].append(pa in group_of and group_of.get(pa) == group_of.get(pb))
    res["duplicates"]["edge_precision_by_relation"] = {
        k: {"edges": len(v), "precision": round(float(np.mean(v)), 4)} for k, v in by_rel.items()
    }
    rel_truth = {
        frozenset((d["original"], d["copy"])): d["relation"]
        for d in gt["near_duplicates"] + gt["cross_split_variant"]
    }
    pred_pairs = _pairs_of(pred_groups)
    per_kind: dict[str, list[bool]] = defaultdict(list)
    for pair, kind in rel_truth.items():
        per_kind[kind].append(pair in pred_pairs)
    for a, b in gt["exact_duplicates"] + gt["cross_split_exact"]:
        per_kind["exact"].append(frozenset((a, b)) in pred_pairs)
    res["duplicates"]["recall_by_injected_relation"] = {
        k: round(float(np.mean(v)), 4) for k, v in per_kind.items()
    }

    # ---- leakage ----------------------------------------------------------------
    gt_leak_pairs = {frozenset(p) for p in _pairs_of(gt_groups) if len({split_of[x] for x in p}) > 1}
    pred_leak_pairs: set[frozenset] = set()
    pred_leak_eval: set[str] = set()
    for lk in s.scalars(select(m.LeakageFinding).where(m.LeakageFinding.audit_run_id == audit_id)):
        if lk.risk == Risk.LOW:
            continue
        paths = [path_of[uuid.UUID(x)] for x in lk.sample_ids]
        for a, b in combinations(paths, 2):
            if split_of[a] != split_of[b]:
                pred_leak_pairs.add(frozenset((a, b)))
        pred_leak_eval |= {p for p in paths if split_of[p] in ("val", "test")}
    gt_leak_eval = {x for p in gt_leak_pairs for x in p if split_of[x] in ("val", "test")}
    res["leakage"] = {
        "pairs": _pr(pred_leak_pairs, gt_leak_pairs),
        "eval_samples": _pr(pred_leak_eval, gt_leak_eval),
    }

    # ---- label triage -----------------------------------------------------------
    errors = {p for p in gt["label_errors"] if p in present}
    susp = dict(
        s.execute(
            select(m.LabelFinding.sample_id, m.LabelFinding.suspicion_score).where(
                m.LabelFinding.audit_run_id == audit_id
            )
        ).all()
    )
    paths = sorted(present)
    y = np.array([p in errors for p in paths])
    score = np.array([susp.get(id_of[p], 0.0) for p in paths])
    order = np.argsort(-score, kind="stable")
    K = int(y.sum())
    lab = {
        "label_errors": K,
        "average_precision": round(float(average_precision_score(y, score)), 4) if K else None,
        "random_baseline_precision": round(K / len(paths), 4),
    }
    for mult in (1, 2):
        k = K * mult
        lab[f"precision_at_{k}"] = round(float(y[order[:k]].mean()), 4)
        lab[f"recall_at_{k}"] = round(float(y[order[:k]].sum() / max(1, K)), 4)
    cases = {
        c.sample_id: c for c in s.scalars(select(m.CourtCase).where(m.CourtCase.audit_run_id == audit_id))
    }
    verdict_of_errors = defaultdict(int)
    for p in errors:
        c = cases.get(id_of[p])
        verdict_of_errors[str(c.verdict) if c else "NO_CASE"] += 1
    lab["verdicts_for_label_errors"] = dict(verdict_of_errors)
    relabels = [c for c in cases.values() if str(c.verdict) == "POSSIBLE_RELABEL"]
    tgt = (
        {
            v.case_id: v.scores.get("target_label")
            for v in s.scalars(
                select(m.CaseVerdict).where(m.CaseVerdict.case_id.in_([c.id for c in relabels]))
            )
        }
        if relabels
        else {}
    )
    correct_target = sum(
        1
        for c in relabels
        if path_of[c.sample_id] in errors
        and tgt.get(c.id) == gt["label_errors"][path_of[c.sample_id]]["true"]
    )
    lab["possible_relabel"] = {
        "count": len(relabels),
        "precision": round(sum(1 for c in relabels if path_of[c.sample_id] in errors) / len(relabels), 4)
        if relabels
        else None,
        "target_label_correct": correct_target,
    }
    res["label_triage"] = lab

    # ---- rare vs wrong ----------------------------------------------------------
    rare = [p for p in gt["rare_valid"] if p in present]
    rv = defaultdict(int)
    destructive = 0
    for p in rare:
        c = cases.get(id_of[p])
        v = str(c.verdict) if c else "NO_CASE"
        rv[v] += 1
        destructive += v in ("POSSIBLE_RELABEL", "POSSIBLE_REMOVE")
    hyp = dict(
        s.execute(
            select(m.RareWrongFinding.sample_id, m.RareWrongFinding.hypothesis).where(
                m.RareWrongFinding.audit_run_id == audit_id
            )
        ).all()
    )
    res["rare_vs_wrong"] = {
        "rare_valid_samples": len(rare),
        "verdicts": dict(rv),
        "false_removal_recommendation_rate": round(destructive / len(rare), 4) if rare else None,
        "hypotheses_for_rare": dict(_count(str(hyp.get(id_of[p], "none")) for p in rare)),
        "hypotheses_for_label_errors": dict(_count(str(hyp.get(id_of[p], "none")) for p in errors)),
        "label_errors_marked_likely_rare": sum(
            1 for p in errors if cases.get(id_of[p]) and str(cases[id_of[p]].verdict) == "LIKELY_RARE"
        ),
    }

    # ---- quality ----------------------------------------------------------------
    qf = s.execute(
        select(m.QualityFinding.sample_id, m.QualityFinding.finding_type, m.QualityFinding.severity).where(
            m.QualityFinding.audit_run_id == audit_id
        )
    ).all()
    flagged = {path_of[r.sample_id] for r in qf if r.severity in (Severity.MEDIUM, Severity.HIGH)}
    gtq = {p for p in gt["quality"] if p in present}
    kind_map = {
        "blur": "potential_blur",
        "low_resolution": "low_resolution",
        "underexposed": "potential_underexposure",
    }
    types = defaultdict(set)
    for r in qf:
        types[path_of[r.sample_id]].add(r.finding_type)
    res["quality"] = {
        "injected": _pr(flagged, gtq),
        "recall_by_kind": {
            k: round(
                float(
                    np.mean(
                        [
                            kind_map[k] in types.get(p, set())
                            for p, kk in gt["quality"].items()
                            if kk == k and p in present
                        ]
                    )
                ),
                4,
            )
            for k in kind_map
        },
        "false_positive_types": dict(_count(t for p in flagged - gtq for t in types.get(p, set()))),
        "note": "Precision counts every medium/high finding outside the injected set as a false positive, including "
        "genuinely dark 'low-light' renders the generator creates on purpose.",
    }

    # ---- shortcut / coverage ----------------------------------------------------
    sc_rows = s.scalars(select(m.ShortcutFinding).where(m.ShortcutFinding.audit_run_id == audit_id)).all()
    hit = [
        r
        for r in sc_rows
        if r.cue == gt["shortcut"]["cue"]
        and r.cue_value == gt["shortcut"]["value"]
        and classes.get(r.class_id) == gt["shortcut"]["class"]
    ]
    res["shortcut"] = {
        "injected": gt["shortcut"],
        "detected": bool(hit),
        "strength": hit[0].strength if hit else None,
        "strength_label": hit[0].strength_label if hit else None,
        "total_findings": len(sc_rows),
        "other_findings": [
            f"{r.cue}={r.cue_value} → {classes.get(r.class_id)} ({r.strength_label})"
            for r in sc_rows
            if r not in hit
        ],
    }
    gaps = s.scalars(select(m.CoverageGap).where(m.CoverageGap.audit_run_id == audit_id)).all()
    cond_hit = [
        g
        for g in gaps
        if g.kind == "condition_gap"
        and classes.get(g.class_id) == gt["condition_gap"]["class"]
        and (g.condition or {}).get("condition") == gt["condition_gap"]["condition"]
    ]
    imb_hit = [
        g
        for g in gaps
        if g.kind == "underrepresented_class" and classes.get(g.class_id) == gt["imbalance"]["class"]
    ]
    res["coverage"] = {
        "condition_gap_detected": bool(cond_hit),
        "condition_gap_priority": cond_hit[0].priority if cond_hit else None,
        "imbalance_detected": bool(imb_hit),
        "total_gaps": len(gaps),
    }
    steps = s.scalars(select(m.AuditPipelineStep).where(m.AuditPipelineStep.audit_run_id == audit_id)).all()
    res["runtime_seconds"] = round(sum((x.duration_ms or 0) for x in steps) / 1000, 2)
    res["stage_seconds"] = {
        x.stage: round((x.duration_ms or 0) / 1000, 2) for x in sorted(steps, key=lambda x: x.order)
    }
    return res


def _count(items) -> dict:
    out: dict = defaultdict(int)
    for i in items:
        out[i] += 1
    return out
