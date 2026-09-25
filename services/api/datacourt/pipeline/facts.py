"""Audit facts: the aggregate numbers that governance rules, contracts and reports read.

Computed from the database (not cached) so they reflect the latest human decisions.
"""

from __future__ import annotations

import uuid
from collections import Counter
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from datacourt.db import models as m
from datacourt.enums import CaseStatus, FileStatus, Risk, Severity, Verdict

HIGH_PRIORITY_VERDICTS = {
    Verdict.STRONG_REVIEW,
    Verdict.POSSIBLE_RELABEL,
    Verdict.POSSIBLE_REMOVE,
    Verdict.LEAKAGE_ACTION_NEEDED,
}
UNRESOLVED = {CaseStatus.OPEN, CaseStatus.DISPUTED}


def collect_facts(s: Session, audit: m.AuditRun) -> dict[str, Any]:
    version = s.get(m.DatasetVersion, audit.dataset_version_id)
    assert version is not None
    dataset = s.get(m.Dataset, version.dataset_id)
    vid, aid = version.id, audit.id
    stats = version.stats or {}
    n = int(stats.get("sample_count", 0))
    class_counts: dict[str, int] = stats.get("class_counts", {})
    split_counts: dict[str, int] = stats.get("splits", {})
    files = stats.get("files", {})
    accepted = int(files.get(str(FileStatus.ACCEPTED), 0))
    rejected = int(files.get(str(FileStatus.REJECTED), 0))
    eval_count = int(split_counts.get("val", 0)) + int(split_counts.get("test", 0))

    csc: dict[str, dict] = stats.get("class_split_counts", {})
    has_eval = eval_count > 0
    train_like = {"train", "unsplit"}
    missing_train = sorted(
        c for c, sp in csc.items() if not any(sp.get(t, 0) for t in train_like) and has_eval
    )
    missing_eval = sorted(
        c for c, sp in csc.items() if has_eval and not (sp.get("val", 0) or sp.get("test", 0))
    )

    # Cases
    cases = s.execute(
        select(
            m.CourtCase.id,
            m.CourtCase.verdict,
            m.CourtCase.status,
            m.CourtCase.categories,
            m.CourtCase.sample_id,
            m.CourtCase.priority_score,
            m.CourtCase.case_number,
        ).where(m.CourtCase.audit_run_id == aid)
    ).all()
    resolved_samples = {c.sample_id for c in cases if c.status not in UNRESOLVED}
    unresolved = [c for c in cases if c.status in UNRESOLVED]
    label_strong = sum(
        1
        for c in unresolved
        if "label" in (c.categories or []) and c.verdict in (Verdict.STRONG_REVIEW, Verdict.POSSIBLE_RELABEL)
    )
    label_review = sum(
        1 for c in unresolved if "label" in (c.categories or []) and c.verdict == Verdict.REVIEW
    )
    high_pri = sum(
        1 for c in unresolved if c.verdict in HIGH_PRIORITY_VERDICTS or c.status == CaseStatus.DISPUTED
    )
    top_label = sorted(
        (c for c in unresolved if "label" in (c.categories or [])), key=lambda c: -c.priority_score
    )[:5]

    # Quality
    q_rows = s.execute(
        select(m.QualityFinding.sample_id, m.QualityFinding.finding_type, m.QualityFinding.severity).where(
            m.QualityFinding.audit_run_id == aid
        )
    ).all()
    q_samples = {r.sample_id for r in q_rows if r.severity in (Severity.MEDIUM, Severity.HIGH)}
    q_by_type = Counter(r.finding_type for r in q_rows)

    # Duplicates
    fams = s.execute(
        select(m.DuplicateFamily.size, m.DuplicateFamily.kind).where(m.DuplicateFamily.audit_run_id == aid)
    ).all()
    redundant = sum(f.size - 1 for f in fams)

    # Leakage (resolved cases no longer count)
    leaks = s.scalars(select(m.LeakageFinding).where(m.LeakageFinding.audit_run_id == aid)).all()
    sample_split = dict(
        s.execute(select(m.Sample.id, m.Sample.split).where(m.Sample.dataset_version_id == vid)).all()
    )
    eval_leaking: set = set()
    exact_eval: set = set()
    by_risk = Counter(str(lk.risk) for lk in leaks)
    for lk in leaks:
        if lk.risk == Risk.LOW:
            continue
        for sid in lk.sample_ids:
            u = uuid.UUID(sid)
            if str(sample_split.get(u)) in ("val", "test") and u not in resolved_samples:
                eval_leaking.add(u)
                if str(lk.kind) == "exact_cross_split":
                    exact_eval.add(u)
    top_leak = [
        {"risk": str(lk.risk), "kind": str(lk.kind), "splits": lk.splits_crossed, "size": len(lk.sample_ids)}
        for lk in sorted(
            leaks, key=lambda x: ["low", "medium", "high", "critical"].index(str(x.risk)), reverse=True
        )[:5]
    ]

    # Shortcuts
    sc = s.execute(
        select(
            m.ShortcutFinding.strength, m.ShortcutFinding.strength_label, m.ShortcutFinding.evidence
        ).where(m.ShortcutFinding.audit_run_id == aid)
    ).all()
    shortcut_max = max((r.strength for r in sc), default=0.0)
    shortcut_strong = sum(1 for r in sc if r.strength_label == "strong")
    step = s.scalar(
        select(m.AuditPipelineStep).where(
            m.AuditPipelineStep.audit_run_id == aid, m.AuditPipelineStep.stage == "SHORTCUT_ANALYSIS"
        )
    )
    sc_summary = (step.output or {}).get("summary", {}) if step else {}

    gaps = Counter(s.scalars(select(m.CoverageGap.priority).where(m.CoverageGap.audit_run_id == aid)).all())

    counts = [v for v in class_counts.values() if v > 0]
    imbalance = (max(counts) / min(counts)) if counts else None
    decided = sum(1 for c in cases if c.status in (CaseStatus.DECIDED, CaseStatus.RESOLVED))
    disputed = sum(1 for c in cases if c.status == CaseStatus.DISPUTED)
    label_actions = Counter(
        s.scalars(select(m.LabelFinding.recommended_action).where(m.LabelFinding.audit_run_id == aid)).all()
    )

    splits_present = sorted(k for k, v in split_counts.items() if v)
    return {
        "sample_count": n,
        "class_count": len(counts),
        "class_counts": class_counts,
        "min_class_count": min(counts) if counts else 0,
        "imbalance_ratio": round(imbalance, 3) if imbalance else None,
        "split_counts": split_counts,
        "splits_present": splits_present,
        "eval_count": eval_count,
        "unreadable_fraction": rejected / max(1, accepted + rejected),
        "rejected_files": rejected,
        "classes_missing_from_train": missing_train,
        "classes_missing_from_eval": missing_eval,
        "case_count": len(cases),
        "cases_by_verdict": dict(Counter(str(c.verdict) for c in cases)),
        "unresolved_cases": len(unresolved),
        "unresolved_label_strong": label_strong,
        "unresolved_label_review": label_review,
        "unresolved_high_priority": high_pri,
        "decided_cases": decided,
        "disputed_cases": disputed,
        "top_label_cases": [{"case_number": c.case_number, "priority": c.priority_score} for c in top_label],
        "label_actions": {str(k): v for k, v in label_actions.items()},
        "quality_samples_medium_plus": len(q_samples),
        "quality_rate": len(q_samples) / max(1, n),
        "quality_by_type": dict(q_by_type),
        "duplicate_families": len(fams),
        "exact_families": sum(1 for f in fams if f.kind == "exact"),
        "redundant_samples": redundant,
        "duplicate_rate": redundant / max(1, n),
        "leakage_by_risk": dict(by_risk),
        "leakage_high_count": by_risk.get("high", 0) + by_risk.get("critical", 0),
        "eval_samples_leaking": len(eval_leaking),
        "exact_eval_leaks": len(exact_eval),
        "top_leakage": top_leak,
        "shortcut_max_strength": round(float(shortcut_max), 4),
        "shortcut_strong": shortcut_strong,
        "cue_only_accuracy": sc_summary.get("all_cues_balanced_accuracy"),
        "cue_chance": sc_summary.get("chance_balanced_accuracy"),
        "gaps_high": gaps.get("high", 0),
        "gaps_medium": gaps.get("medium", 0),
        "gaps_low": gaps.get("low", 0),
        "provenance_coverage": _provenance_coverage(stats, dataset),
        "dataset_provenance": (dataset.provenance if dataset else {}) or {},
    }


def _provenance_coverage(stats: dict, dataset: m.Dataset | None) -> float:
    """Per-sample coverage, or 1.0 when dataset-level source and license are recorded."""
    per_sample = float(stats.get("provenance_coverage", 0.0))
    prov = (dataset.provenance if dataset else None) or {}
    return 1.0 if prov.get("source") and prov.get("license") else per_sample


def enrich_with_governance(
    facts: dict[str, Any], debt_overall: str | None, preflight_status: str | None
) -> dict[str, Any]:
    from datacourt.ml.governance import LEVEL_RANK, PREFLIGHT_RANK

    out = dict(facts)
    out["debt_level_rank"] = LEVEL_RANK.get(debt_overall or "", None)
    out["preflight_rank"] = PREFLIGHT_RANK.get(preflight_status or "", None)
    return out


def count_rows(s: Session, model: Any, audit_id: uuid.UUID) -> int:
    return int(s.scalar(select(func.count()).select_from(model).where(model.audit_run_id == audit_id)) or 0)
