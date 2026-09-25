"""Evidence views for a dataset version (latest completed audit unless ?audit_id=)."""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import UTC, datetime

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from datacourt import jobs, ledger
from datacourt.api.deps import current_principal, not_demo
from datacourt.api.views import iso, sample_view
from datacourt.db import models as m
from datacourt.db.session import get_db
from datacourt.enums import CollectionStatus, JobType, Role, RunStatus
from datacourt.ml import dna as dna_mod
from datacourt.ml import governance as gov
from datacourt.pipeline.context import AuditContext, load_samples
from datacourt.pipeline.facts import collect_facts, enrich_with_governance
from datacourt.services.blame import blame_map
from datacourt.tenancy import Principal, get_owned, get_version, not_found, resolve_audit

router = APIRouter(tags=["findings"])


def _ctx(s: Session, p: Principal, version_id: uuid.UUID, audit_id: uuid.UUID | None):
    v = get_version(s, p, version_id)
    a = resolve_audit(s, v, audit_id)
    return v, a


def _samples_by_id(s: Session, ids) -> dict[uuid.UUID, m.Sample]:
    ids = list(ids)
    if not ids:
        return {}
    return {x.id: x for x in s.scalars(select(m.Sample).where(m.Sample.id.in_(ids)))}


def _step_output(s: Session, audit_id: uuid.UUID, stage: str) -> dict:
    st = s.scalar(
        select(m.AuditPipelineStep).where(
            m.AuditPipelineStep.audit_run_id == audit_id, m.AuditPipelineStep.stage == stage
        )
    )
    return (st.output or {}) if st else {}


def _artifact_ctx(s: Session, a: m.AuditRun) -> AuditContext:
    v = s.get(m.DatasetVersion, a.dataset_version_id)
    assert v is not None
    return AuditContext(a.id, a.org_id, v.id, v.dataset_id, str(a.profile), a.config, load_samples(v.id))


# ---- overview ---------------------------------------------------------------------


@router.get("/versions/{version_id}/overview")
def overview(
    version_id: uuid.UUID,
    audit_id: uuid.UUID | None = None,
    p: Principal = Depends(current_principal),
    s: Session = Depends(get_db),
) -> dict:
    v = get_version(s, p, version_id)
    try:
        a = resolve_audit(s, v, audit_id)
    except HTTPException:
        return {"version_id": str(v.id), "stats": v.stats, "audit": None}
    facts = collect_facts(s, a)
    debt = gov.compute_debt(facts)
    pf = s.scalar(
        select(m.PreflightResult)
        .where(m.PreflightResult.audit_run_id == a.id)
        .order_by(m.PreflightResult.created_at.desc())
        .limit(1)
    )
    dna = s.scalar(select(m.DatasetDnaProfile).where(m.DatasetDnaProfile.audit_run_id == a.id))
    metrics = s.scalars(
        select(m.SampleQualityMetrics.metrics).where(m.SampleQualityMetrics.audit_run_id == a.id)
    ).all()

    def hist(key: str, bins: int = 20) -> dict:
        vals = np.array([float(x[key]) for x in metrics if key in x])
        if len(vals) == 0:
            return {"counts": [], "edges": []}
        if key in ("blur_laplacian_var",):
            vals = np.log10(vals + 1)
        h, e = np.histogram(vals, bins=bins)
        return {"counts": h.tolist(), "edges": [round(float(x), 3) for x in e]}

    return {
        "version_id": str(v.id),
        "audit": {
            "id": str(a.id),
            "profile": str(a.profile),
            "finished_at": iso(a.finished_at),
            "summary": a.summary,
            "warnings": a.warnings,
            "embedding_model": a.embedding_model,
        },
        "stats": v.stats,
        "facts": facts,
        "debt": {
            "overall": debt["overall"],
            "levels": {k: d["level"] for k, d in debt["dimensions"].items()},
        },
        "preflight": {"status": str(pf.status), "override": pf.override_status} if pf else None,
        "dna": {"fingerprint": dna.fingerprint, "strip": dna.profile.get("strip")} if dna else None,
        "distributions": {
            "brightness": hist("brightness"),
            "log10_sharpness": hist("blur_laplacian_var"),
            "min_side": hist("min_side"),
            "saturation": hist("saturation"),
        },
    }


# ---- quality ------------------------------------------------------------------------


@router.get("/versions/{version_id}/quality")
def quality(
    version_id: uuid.UUID,
    audit_id: uuid.UUID | None = None,
    finding_type: str | None = None,
    severity: str | None = None,
    offset: int = 0,
    limit: int = Query(60, le=200),
    p: Principal = Depends(current_principal),
    s: Session = Depends(get_db),
) -> dict:
    v, a = _ctx(s, p, version_id, audit_id)
    base = select(m.QualityFinding).where(m.QualityFinding.audit_run_id == a.id)
    counts = s.execute(
        select(m.QualityFinding.finding_type, m.QualityFinding.severity, func.count())
        .where(m.QualityFinding.audit_run_id == a.id)
        .group_by(m.QualityFinding.finding_type, m.QualityFinding.severity)
    ).all()
    if finding_type:
        base = base.where(m.QualityFinding.finding_type == finding_type)
    if severity:
        base = base.where(m.QualityFinding.severity == severity)
    total = s.scalar(select(func.count()).select_from(base.subquery()))
    rows = s.scalars(
        base.order_by(m.QualityFinding.severity.desc(), m.QualityFinding.finding_type)
        .offset(offset)
        .limit(limit)
    ).all()
    samples = _samples_by_id(s, {r.sample_id for r in rows})
    privacy = s.execute(
        select(m.PrivacyFinding.kind, func.count())
        .where(m.PrivacyFinding.audit_run_id == a.id)
        .group_by(m.PrivacyFinding.kind)
    ).all()
    return {
        "audit_id": str(a.id),
        "rule_version": a.algorithm_versions.get("quality"),
        "thresholds": a.config["quality"],
        "counts": [{"type": t, "severity": str(sv), "count": c} for t, sv, c in counts],
        "privacy_counts": dict(privacy),
        "privacy_scan": a.config["privacy"]["enabled"],
        "rejected_files": (v.stats or {}).get("rejected_reasons", {}),
        "total": total,
        "items": [
            {
                "type": r.finding_type,
                "severity": str(r.severity),
                "value": r.measured_value,
                "threshold": r.threshold,
                "comparator": r.comparator,
                "deterministic": r.deterministic,
                "description": r.description,
                "sample": sample_view(samples[r.sample_id]),
            }
            for r in rows
        ],
    }


@router.get("/versions/{version_id}/privacy")
def privacy(
    version_id: uuid.UUID,
    audit_id: uuid.UUID | None = None,
    p: Principal = Depends(current_principal),
    s: Session = Depends(get_db),
) -> dict:
    v, a = _ctx(s, p, version_id, audit_id)
    rows = s.scalars(select(m.PrivacyFinding).where(m.PrivacyFinding.audit_run_id == a.id).limit(500)).all()
    samples = _samples_by_id(s, {r.sample_id for r in rows})
    return {
        "enabled": a.config["privacy"]["enabled"],
        "algorithm": a.algorithm_versions.get("privacy"),
        "note": "Detector-based flags only. No identity recognition is performed.",
        "items": [
            {
                "kind": r.kind,
                "count": r.count,
                "score": r.score,
                "boxes": r.boxes,
                "sample": sample_view(samples[r.sample_id]),
            }
            for r in rows
        ],
    }


# ---- duplicates & lineage --------------------------------------------------------------


@router.get("/versions/{version_id}/duplicates")
def duplicates(
    version_id: uuid.UUID,
    audit_id: uuid.UUID | None = None,
    kind: str | None = None,
    cross_split: bool | None = None,
    label_conflict: bool | None = None,
    offset: int = 0,
    limit: int = Query(30, le=100),
    p: Principal = Depends(current_principal),
    s: Session = Depends(get_db),
) -> dict:
    v, a = _ctx(s, p, version_id, audit_id)
    q = select(m.DuplicateFamily).where(m.DuplicateFamily.audit_run_id == a.id)
    if kind:
        q = q.where(m.DuplicateFamily.kind == kind)
    if cross_split is not None:
        q = q.where(m.DuplicateFamily.crosses_splits.is_(cross_split))
    if label_conflict is not None:
        q = q.where(m.DuplicateFamily.label_conflict.is_(label_conflict))
    total = s.scalar(select(func.count()).select_from(q.subquery()))
    fams = s.scalars(
        q.order_by(
            m.DuplicateFamily.crosses_splits.desc(),
            m.DuplicateFamily.size.desc(),
            m.DuplicateFamily.family_number,
        )
        .offset(offset)
        .limit(limit)
    ).all()
    members = (
        s.scalars(
            select(m.DuplicateFamilyMember).where(m.DuplicateFamilyMember.family_id.in_([f.id for f in fams]))
        ).all()
        if fams
        else []
    )
    samples = _samples_by_id(s, {x.sample_id for x in members})
    by_fam: dict = {}
    for x in members:
        by_fam.setdefault(x.family_id, []).append(x)
    out = _step_output(s, a.id, "DUPLICATE_ANALYSIS")
    return {
        "audit_id": str(a.id),
        "summary": {
            k: out.get(k)
            for k in (
                "families",
                "edges",
                "redundant_samples",
                "relations",
                "exact_families",
                "label_conflict_families",
                "thresholds",
            )
        },
        "total": total,
        "families": [
            {
                "id": str(f.id),
                "number": f.family_number,
                "size": f.size,
                "kind": f.kind,
                "splits": f.splits,
                "labels": f.labels,
                "crosses_splits": f.crosses_splits,
                "label_conflict": f.label_conflict,
                "max_similarity": f.max_similarity,
                "members": [
                    {
                        "sample": sample_view(samples[x.sample_id]),
                        "relation": x.relation,
                        "similarity": x.similarity,
                        "is_root": x.parent_sample_id is None,
                        "depth": x.depth,
                    }
                    for x in sorted(by_fam.get(f.id, []), key=lambda y: (y.depth, str(y.sample_id)))
                ],
            }
            for f in fams
        ],
    }


@router.get("/families/{family_id}/lineage")
def lineage(
    family_id: uuid.UUID, p: Principal = Depends(current_principal), s: Session = Depends(get_db)
) -> dict:
    fam = s.get(m.DuplicateFamily, family_id)
    if fam is None:
        raise not_found()
    audit = s.get(m.AuditRun, fam.audit_run_id)
    assert audit is not None
    from datacourt.tenancy import require_org

    require_org(s, p, audit.org_id)
    members = s.scalars(
        select(m.DuplicateFamilyMember).where(m.DuplicateFamilyMember.family_id == family_id)
    ).all()
    samples = _samples_by_id(s, {x.sample_id for x in members})
    edges = s.scalars(select(m.SimilarityEdge).where(m.SimilarityEdge.family_id == family_id)).all()
    leaks = s.scalars(select(m.LeakageFinding).where(m.LeakageFinding.family_id == family_id)).all()
    return {
        "family": {
            "id": str(fam.id),
            "number": fam.family_number,
            "size": fam.size,
            "kind": fam.kind,
            "splits": fam.splits,
            "labels": fam.labels,
            "crosses_splits": fam.crosses_splits,
            "label_conflict": fam.label_conflict,
        },
        "leakage": [
            {
                "risk": str(lk.risk),
                "kind": str(lk.kind),
                "splits": lk.splits_crossed,
                "explanation": lk.explanation,
            }
            for lk in leaks
        ],
        "nodes": [
            {
                "id": str(x.sample_id),
                "parent": str(x.parent_sample_id) if x.parent_sample_id else None,
                "relation": x.relation,
                "similarity": x.similarity,
                "phash_distance": x.phash_distance,
                "depth": x.depth,
                "evidence": x.evidence,
                "sample": sample_view(samples[x.sample_id]),
            }
            for x in members
        ],
        "edges": [
            {
                "a": str(e.sample_a_id),
                "b": str(e.sample_b_id),
                "relation": str(e.relation),
                "cosine": e.cosine,
                "phash_distance": e.phash_distance,
            }
            for e in edges
        ],
        "legend": {
            "exact": "byte-identical (measured)",
            "resized": "same content, different size (heuristic)",
            "flipped": "mirror image (heuristic)",
            "possible_crop": "cropped / reframed (heuristic, keypoint-verified)",
            "photometric": "brightness/colour change (heuristic)",
            "near_duplicate": "same fine detail (heuristic)",
        },
    }


# ---- leakage ------------------------------------------------------------------------------


@router.get("/versions/{version_id}/leakage")
def leakage(
    version_id: uuid.UUID,
    audit_id: uuid.UUID | None = None,
    risk: str | None = None,
    p: Principal = Depends(current_principal),
    s: Session = Depends(get_db),
) -> dict:
    v, a = _ctx(s, p, version_id, audit_id)
    q = select(m.LeakageFinding).where(m.LeakageFinding.audit_run_id == a.id)
    if risk:
        q = q.where(m.LeakageFinding.risk == risk)
    rows = s.scalars(q).all()
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    rows = sorted(rows, key=lambda r: (order[str(r.risk)], -r.max_similarity))[:300]
    samples = _samples_by_id(s, {uuid.UUID(x) for r in rows for x in r.sample_ids[:12]})
    integ = s.scalar(select(m.SplitIntegrityMetrics).where(m.SplitIntegrityMetrics.audit_run_id == a.id))
    return {
        "audit_id": str(a.id),
        "integrity": integ.metrics if integ else {},
        "summary": {k: v2 for k, v2 in _step_output(s, a.id, "LEAKAGE_ANALYSIS").items() if k != "integrity"},
        "findings": [
            {
                "id": str(r.id),
                "kind": str(r.kind),
                "risk": str(r.risk),
                "splits": r.splits_crossed,
                "family_id": str(r.family_id) if r.family_id else None,
                "max_similarity": r.max_similarity,
                "explanation": r.explanation,
                "evidence": r.evidence,
                "size": len(r.sample_ids),
                "samples": [
                    sample_view(samples[uuid.UUID(x)]) for x in r.sample_ids[:12] if uuid.UUID(x) in samples
                ],
            }
            for r in rows
        ],
    }


# ---- labels / rare-or-wrong -------------------------------------------------------------------


@router.get("/versions/{version_id}/labels")
def labels(
    version_id: uuid.UUID,
    audit_id: uuid.UUID | None = None,
    action: str | None = None,
    class_name: str | None = None,
    min_suspicion: float = 0.0,
    offset: int = 0,
    limit: int = Query(60, le=200),
    p: Principal = Depends(current_principal),
    s: Session = Depends(get_db),
) -> dict:
    v, a = _ctx(s, p, version_id, audit_id)
    q = (
        select(m.LabelFinding)
        .join(m.Sample, m.Sample.id == m.LabelFinding.sample_id)
        .where(m.LabelFinding.audit_run_id == a.id, m.LabelFinding.suspicion_score >= min_suspicion)
    )
    if action:
        q = q.where(m.LabelFinding.recommended_action == action)
    if class_name:
        q = q.where(m.Sample.label == class_name)
    total = s.scalar(select(func.count()).select_from(q.subquery()))
    rows = s.scalars(q.order_by(m.LabelFinding.suspicion_score.desc()).offset(offset).limit(limit)).all()
    samples = _samples_by_id(s, {r.sample_id for r in rows})
    cases = (
        {
            c.sample_id: c
            for c in s.scalars(
                select(m.CourtCase).where(
                    m.CourtCase.audit_run_id == a.id, m.CourtCase.sample_id.in_([r.sample_id for r in rows])
                )
            )
        }
        if rows
        else {}
    )
    out = _step_output(s, a.id, "LABEL_FORENSICS")
    return {
        "audit_id": str(a.id),
        "summary": out,
        "weights_config": a.config["labels"]["weights"],
        "total": total,
        "items": [
            {
                "suspicion": r.suspicion_score,
                "action": str(r.recommended_action),
                "model_disagreement": r.model_disagreement,
                "neighbor_agreement": r.neighbor_agreement,
                "centroid_ratio": r.centroid_ratio,
                "evidence": r.evidence,
                "sample": sample_view(samples[r.sample_id]),
                "case": {
                    "id": str(cases[r.sample_id].id),
                    "number": cases[r.sample_id].case_number,
                    "verdict": str(cases[r.sample_id].verdict),
                }
                if r.sample_id in cases
                else None,
            }
            for r in rows
        ],
    }


@router.get("/versions/{version_id}/rare-or-wrong")
def rare_or_wrong(
    version_id: uuid.UUID,
    audit_id: uuid.UUID | None = None,
    hypothesis: str | None = None,
    offset: int = 0,
    limit: int = Query(60, le=200),
    p: Principal = Depends(current_principal),
    s: Session = Depends(get_db),
) -> dict:
    v, a = _ctx(s, p, version_id, audit_id)
    q = select(m.RareWrongFinding).where(m.RareWrongFinding.audit_run_id == a.id)
    counts = dict(
        s.execute(
            select(m.RareWrongFinding.hypothesis, func.count())
            .where(m.RareWrongFinding.audit_run_id == a.id)
            .group_by(m.RareWrongFinding.hypothesis)
        ).all()
    )
    if hypothesis:
        q = q.where(m.RareWrongFinding.hypothesis == hypothesis)
    total = s.scalar(select(func.count()).select_from(q.subquery()))
    rows = s.scalars(q.offset(offset).limit(limit)).all()
    samples = _samples_by_id(s, {r.sample_id for r in rows})
    return {
        "audit_id": str(a.id),
        "counts": {str(k): c for k, c in counts.items()},
        "total": total,
        "note": "Scores are evidence scores for competing hypotheses, not calibrated probabilities.",
        "items": [
            {
                "hypothesis": str(r.hypothesis),
                "scores": r.scores,
                "reasons": r.reasons,
                "valuable_reasons": r.valuable_reasons,
                "sample": sample_view(samples[r.sample_id]),
            }
            for r in rows
        ],
    }


# ---- shortcuts ------------------------------------------------------------------------------------


@router.get("/versions/{version_id}/shortcuts")
def shortcuts(
    version_id: uuid.UUID,
    audit_id: uuid.UUID | None = None,
    p: Principal = Depends(current_principal),
    s: Session = Depends(get_db),
) -> dict:
    v, a = _ctx(s, p, version_id, audit_id)
    rows = s.scalars(
        select(m.ShortcutFinding)
        .where(m.ShortcutFinding.audit_run_id == a.id)
        .order_by(m.ShortcutFinding.strength.desc())
    ).all()
    classes = {
        c.id: c.name
        for c in s.scalars(select(m.DatasetClass).where(m.DatasetClass.dataset_version_id == v.id))
    }
    samples = _samples_by_id(s, {uuid.UUID(x) for r in rows for x in r.affected_sample_ids[:8]})
    return {
        "audit_id": str(a.id),
        "summary": _step_output(s, a.id, "SHORTCUT_ANALYSIS"),
        "thresholds": a.config["shortcuts"],
        "findings": [
            {
                "id": str(r.id),
                "cue": r.cue,
                "cue_value": r.cue_value,
                "class": classes.get(r.class_id),
                "strength": r.strength,
                "strength_label": r.strength_label,
                "association": r.association,
                "affected_count": len(r.affected_sample_ids),
                "consequence": r.consequence,
                "recommendation": r.recommendation,
                "evidence": r.evidence,
                "examples": [
                    sample_view(samples[uuid.UUID(x)])
                    for x in r.affected_sample_ids[:8]
                    if uuid.UUID(x) in samples
                ],
            }
            for r in rows
        ],
    }


# ---- coverage & collection ------------------------------------------------------------------------


@router.get("/versions/{version_id}/coverage")
def coverage(
    version_id: uuid.UUID,
    audit_id: uuid.UUID | None = None,
    max_points: int = Query(4000, le=20000),
    p: Principal = Depends(current_principal),
    s: Session = Depends(get_db),
) -> dict:
    v, a = _ctx(s, p, version_id, audit_id)
    ctx = _artifact_ctx(s, a)
    cov = ctx.load_npz("coverage")
    S = ctx.samples
    n = S.n
    step = max(1, int(np.ceil(n / max_points)))
    idx = np.arange(0, n, step)
    dens = cov["density"]
    dens_pct = (dens.argsort().argsort() / max(1, n - 1)) if n else dens
    points = [
        {
            "id": str(S.ids[i]),
            "x": round(float(cov["coords"][i, 0]), 4),
            "y": round(float(cov["coords"][i, 1]), 4),
            "label": S.class_names[S.labels[i]],
            "split": S.split[i],
            "cluster": int(cov["assign"][i]),
            "density_pct": round(float(dens_pct[i]), 3),
        }
        for i in idx
    ]
    clusters = s.scalars(
        select(m.CoverageCluster)
        .where(m.CoverageCluster.audit_run_id == a.id)
        .order_by(m.CoverageCluster.cluster_index)
    ).all()
    gaps = s.scalars(
        select(m.CoverageGap)
        .where(m.CoverageGap.audit_run_id == a.id)
        .order_by(m.CoverageGap.priority_score.desc())
    ).all()
    classes = {
        c.id: c.name
        for c in s.scalars(select(m.DatasetClass).where(m.DatasetClass.dataset_version_id == v.id))
    }
    anchor_ids = {uuid.UUID(x) for g in gaps for x in g.anchors[:6]}
    anchors = _samples_by_id(s, anchor_ids)
    tasks = s.scalars(
        select(m.CollectionTask)
        .where(m.CollectionTask.dataset_id == v.dataset_id)
        .order_by(m.CollectionTask.created_at.desc())
    ).all()
    return {
        "audit_id": str(a.id),
        "projection": _step_output(s, a.id, "COVERAGE_ANALYSIS").get("projection"),
        "points": points,
        "sampled_every": step,
        "classes": S.class_names,
        "clusters": [
            {
                "index": c.cluster_index,
                "size": c.size,
                "centroid": c.centroid_2d,
                "composition": c.class_composition,
                "splits": c.split_composition,
                "purity": c.purity,
                "density": c.density,
                "is_sparse": c.is_sparse,
                "dominant_class": classes.get(c.dominant_class_id),
                "attributes": c.attributes,
            }
            for c in clusters
        ],
        "gaps": [
            {
                "id": str(g.id),
                "kind": g.kind,
                "class": classes.get(g.class_id),
                "cluster": g.cluster_index,
                "condition": g.condition,
                "title": g.title,
                "description": g.description,
                "priority": g.priority,
                "priority_score": g.priority_score,
                "suggested_quantity": g.suggested_quantity,
                "evidence": g.evidence,
                "anchors": [
                    sample_view(anchors[uuid.UUID(x)]) for x in g.anchors[:6] if uuid.UUID(x) in anchors
                ],
            }
            for g in gaps
        ],
        "collection_tasks": [_task_view(t) for t in tasks],
        "disclaimer": "Recommendations describe what to collect next. DataCourt never generates synthetic data by default; "
        "quantities are heuristic ranges derived from the gap size.",
    }


def _task_view(t: m.CollectionTask) -> dict:
    return {
        "id": str(t.id),
        "gap_id": str(t.gap_id) if t.gap_id else None,
        "target_class": t.target_class,
        "target_condition": t.target_condition,
        "quantity": t.quantity,
        "priority": t.priority,
        "reason": t.reason,
        "status": str(t.status),
        "anchors": t.anchors,
        "created_at": iso(t.created_at),
        "updated_at": iso(t.updated_at),
    }


class TaskFromGapIn(BaseModel):
    gap_id: uuid.UUID


@router.post("/versions/{version_id}/collection-tasks")
def create_collection_task(
    version_id: uuid.UUID, body: TaskFromGapIn, p: Principal = Depends(not_demo), s: Session = Depends(get_db)
) -> dict:
    v = get_version(s, p, version_id, Role.REVIEWER)
    gap = s.get(m.CoverageGap, body.gap_id)
    if gap is None:
        raise not_found()
    a = s.get(m.AuditRun, gap.audit_run_id)
    if a is None or a.dataset_version_id != v.id:
        raise not_found()
    cls = s.get(m.DatasetClass, gap.class_id) if gap.class_id else None
    cond = (gap.condition or {}).get("description") or gap.title
    t = m.CollectionTask(
        org_id=v.org_id,
        dataset_id=v.dataset_id,
        gap_id=gap.id,
        target_class=cls.name if cls else "(any)",
        target_condition=cond,
        quantity=gap.suggested_quantity,
        priority=gap.priority,
        reason=gap.description,
        anchors=gap.anchors[:6],
        status=CollectionStatus.OPEN,
        created_by=p.user_id,
    )
    s.add(t)
    s.commit()
    return _task_view(t)


class TaskPatch(BaseModel):
    status: CollectionStatus


@router.patch("/collection-tasks/{task_id}")
def update_collection_task(
    task_id: uuid.UUID, body: TaskPatch, p: Principal = Depends(not_demo), s: Session = Depends(get_db)
) -> dict:
    t = get_owned(s, p, m.CollectionTask, task_id, Role.REVIEWER)
    t.status = body.status
    t.updated_at = datetime.now(UTC)
    s.commit()
    return _task_view(t)


# ---- cartography & influence -------------------------------------------------------------------------


@router.get("/versions/{version_id}/cartography")
def cartography(
    version_id: uuid.UUID,
    audit_id: uuid.UUID | None = None,
    category: str | None = None,
    p: Principal = Depends(current_principal),
    s: Session = Depends(get_db),
) -> dict:
    v, a = _ctx(s, p, version_id, audit_id)
    rows = s.execute(
        select(
            m.TrainingDynamics.sample_id,
            m.TrainingDynamics.confidence,
            m.TrainingDynamics.variability,
            m.TrainingDynamics.correctness,
            m.TrainingDynamics.forgetting_events,
            m.TrainingDynamics.category,
        ).where(m.TrainingDynamics.audit_run_id == a.id)
    ).all()
    if not rows:
        return {
            "audit_id": str(a.id),
            "available": False,
            "reason": _step_output(s, a.id, "TRAINING_DYNAMICS").get(
                "reason", "training dynamics not computed"
            ),
        }
    label = dict(
        s.execute(select(m.Sample.id, m.Sample.label).where(m.Sample.dataset_version_id == v.id)).all()
    )
    pick = [r for r in rows if r.category == category] if category else rows
    ex_rows = sorted(pick, key=lambda r: r.confidence)[:24]
    samples = _samples_by_id(s, {r.sample_id for r in ex_rows})
    traj = (
        {
            t.sample_id: t.trajectory
            for t in s.scalars(
                select(m.TrainingDynamics).where(
                    m.TrainingDynamics.audit_run_id == a.id,
                    m.TrainingDynamics.sample_id.in_([r.sample_id for r in ex_rows]),
                )
            )
        }
        if ex_rows
        else {}
    )
    return {
        "audit_id": str(a.id),
        "available": True,
        "summary": _step_output(s, a.id, "TRAINING_DYNAMICS"),
        "thresholds": a.config["dynamics"],
        "points": [
            {
                "id": str(r.sample_id),
                "confidence": round(r.confidence, 4),
                "variability": round(r.variability, 4),
                "correctness": round(r.correctness, 3),
                "forgetting": r.forgetting_events,
                "category": str(r.category),
                "label": label.get(r.sample_id),
            }
            for r in rows[:8000]
        ],
        "examples": [
            {
                "sample": sample_view(samples[r.sample_id]),
                "confidence": r.confidence,
                "variability": r.variability,
                "category": str(r.category),
                "forgetting": r.forgetting_events,
                "trajectory": traj.get(r.sample_id),
            }
            for r in ex_rows
        ],
    }


@router.get("/versions/{version_id}/influence")
def influence(
    version_id: uuid.UUID,
    audit_id: uuid.UUID | None = None,
    sort: str = Query("harmful", pattern="^(harmful|self)$"),
    limit: int = Query(60, le=200),
    p: Principal = Depends(current_principal),
    s: Session = Depends(get_db),
) -> dict:
    v, a = _ctx(s, p, version_id, audit_id)
    order = (
        m.InfluenceFinding.harmful_influence.desc()
        if sort == "harmful"
        else m.InfluenceFinding.self_influence.desc()
    )
    rows = s.scalars(
        select(m.InfluenceFinding).where(m.InfluenceFinding.audit_run_id == a.id).order_by(order).limit(limit)
    ).all()
    if not rows:
        return {
            "audit_id": str(a.id),
            "available": False,
            "reason": _step_output(s, a.id, "INFLUENCE_ANALYSIS").get("reason", "influence not computed"),
        }
    samples = _samples_by_id(s, {r.sample_id for r in rows})
    cases = {
        c.sample_id: c
        for c in s.scalars(
            select(m.CourtCase).where(
                m.CourtCase.audit_run_id == a.id, m.CourtCase.sample_id.in_([r.sample_id for r in rows])
            )
        )
    }
    return {
        "audit_id": str(a.id),
        "available": True,
        "summary": _step_output(s, a.id, "INFLUENCE_ANALYSIS"),
        "items": [
            {
                "self_influence": r.self_influence,
                "self_influence_pct": r.self_influence_pct,
                "harmful_influence": r.harmful_influence,
                "helpful_influence": r.helpful_influence,
                "failures_harmed": r.failures_harmed,
                "affected_classes": r.affected_classes,
                "sample": sample_view(samples[r.sample_id]),
                "case": {
                    "id": str(cases[r.sample_id].id),
                    "number": cases[r.sample_id].case_number,
                    "verdict": str(cases[r.sample_id].verdict),
                }
                if r.sample_id in cases
                else None,
            }
            for r in rows
        ],
    }


@router.get("/versions/{version_id}/failures")
def failures(
    version_id: uuid.UUID,
    audit_id: uuid.UUID | None = None,
    p: Principal = Depends(current_principal),
    s: Session = Depends(get_db),
) -> dict:
    v, a = _ctx(s, p, version_id, audit_id)
    rows = s.scalars(
        select(m.FailureEvent)
        .where(m.FailureEvent.audit_run_id == a.id)
        .order_by(m.FailureEvent.confidence.desc())
        .limit(300)
    ).all()
    samples = _samples_by_id(s, {r.sample_id for r in rows})
    classes = {
        c.id: c.name
        for c in s.scalars(select(m.DatasetClass).where(m.DatasetClass.dataset_version_id == v.id))
    }
    base = s.scalar(
        select(m.BaselineModelRun).where(
            m.BaselineModelRun.audit_run_id == a.id, m.BaselineModelRun.kind == "full"
        )
    )
    return {
        "audit_id": str(a.id),
        "eval_mode": (base.metrics or {}).get("eval_mode") if base else None,
        "metrics": (base.metrics or {}).get("eval") if base else None,
        "items": [
            {
                "id": str(r.id),
                "split": r.split,
                "actual": classes.get(r.actual_class_id),
                "predicted": classes.get(r.predicted_class_id),
                "confidence": r.confidence,
                "sample": sample_view(samples[r.sample_id]),
            }
            for r in rows
        ],
    }


@router.get("/failures/{failure_id}/blame-map")
def get_blame_map(
    failure_id: uuid.UUID, p: Principal = Depends(current_principal), s: Session = Depends(get_db)
) -> dict:
    fe = s.get(m.FailureEvent, failure_id)
    if fe is None:
        raise not_found()
    a = s.get(m.AuditRun, fe.audit_run_id)
    assert a is not None
    from datacourt.tenancy import require_org

    require_org(s, p, a.org_id)
    return blame_map(s, fe, sample_view)


# ---- debt, preflight, dna ------------------------------------------------------------------------


@router.get("/versions/{version_id}/debt")
def debt(
    version_id: uuid.UUID,
    audit_id: uuid.UUID | None = None,
    p: Principal = Depends(current_principal),
    s: Session = Depends(get_db),
) -> dict:
    v, a = _ctx(s, p, version_id, audit_id)
    facts = collect_facts(s, a)
    live = gov.compute_debt(facts)
    at_audit = s.scalar(
        select(m.DatasetDebtSnapshot).where(
            m.DatasetDebtSnapshot.audit_run_id == a.id, m.DatasetDebtSnapshot.trigger == "audit"
        )
    )
    trend = []
    for ver in s.scalars(
        select(m.DatasetVersion)
        .where(m.DatasetVersion.dataset_id == v.dataset_id)
        .order_by(m.DatasetVersion.version_number)
    ):
        snap = s.scalar(
            select(m.DatasetDebtSnapshot)
            .where(m.DatasetDebtSnapshot.dataset_version_id == ver.id)
            .order_by(m.DatasetDebtSnapshot.created_at.desc())
            .limit(1)
        )
        if snap:
            trend.append(
                {
                    "version": ver.version_number,
                    "version_id": str(ver.id),
                    "overall": str(snap.overall),
                    "levels": {k: d["level"] for k, d in snap.dimensions["dimensions"].items()},
                    "values": {k: d["value"] for k, d in snap.dimensions["dimensions"].items()},
                    "at": iso(snap.created_at),
                }
            )
    return {
        "audit_id": str(a.id),
        "live": live,
        "at_audit": at_audit.dimensions if at_audit else None,
        "trend": trend,
        "note": "Live values include human review progress; 'at audit' is the snapshot taken when the audit finished.",
    }


@router.post("/versions/{version_id}/debt/snapshot")
def snapshot_debt(
    version_id: uuid.UUID, p: Principal = Depends(not_demo), s: Session = Depends(get_db)
) -> dict:
    v, a = _ctx(s, p, version_id, None)
    live = gov.compute_debt(collect_facts(s, a))
    s.add(
        m.DatasetDebtSnapshot(
            dataset_version_id=v.id,
            audit_run_id=a.id,
            formula_version=gov.DEBT_VERSION,
            trigger="review",
            overall=live["overall"],
            dimensions=live,
        )
    )
    s.commit()
    return {"overall": live["overall"]}


@router.get("/versions/{version_id}/preflight")
def preflight(
    version_id: uuid.UUID,
    audit_id: uuid.UUID | None = None,
    p: Principal = Depends(current_principal),
    s: Session = Depends(get_db),
) -> dict:
    v, a = _ctx(s, p, version_id, audit_id)
    facts = collect_facts(s, a)
    live = gov.preflight(facts, a.config["preflight"])
    stored = s.scalar(
        select(m.PreflightResult)
        .where(m.PreflightResult.audit_run_id == a.id)
        .order_by(m.PreflightResult.created_at.desc())
        .limit(1)
    )
    debt_live = gov.compute_debt(facts)
    contract = None
    ds = s.get(m.Dataset, v.dataset_id)
    assert ds is not None
    c = s.scalar(
        select(m.DataContract)
        .where(m.DataContract.project_id == ds.project_id, m.DataContract.is_active.is_(True))
        .order_by(m.DataContract.version.desc())
        .limit(1)
    )
    if c is not None:
        contract = {
            "id": str(c.id),
            "version": c.version,
            "name": c.name,
            **gov.evaluate_contract(
                c.rules, enrich_with_governance(facts, debt_live["overall"], live["status"])
            ),
        }
    return {
        "audit_id": str(a.id),
        "live": live,
        "config": a.config["preflight"],
        "stored": {
            "id": str(stored.id),
            "status": str(stored.status),
            "override_status": stored.override_status,
            "override_reason": stored.override_reason,
            "override_at": iso(stored.override_at),
        }
        if stored
        else None,
        "effective_status": stored.override_status if stored and stored.override_status else live["status"],
        "contract": contract,
    }


class OverrideIn(BaseModel):
    status: str = Field(pattern="^(READY|READY_WITH_WARNINGS)$")
    reason: str = Field(min_length=10, max_length=2000)


@router.post("/versions/{version_id}/preflight/override")
def override_preflight(
    version_id: uuid.UUID, body: OverrideIn, p: Principal = Depends(not_demo), s: Session = Depends(get_db)
) -> dict:
    v = get_version(s, p, version_id, Role.ADMIN)
    a = resolve_audit(s, v, None)
    pf = s.scalar(
        select(m.PreflightResult)
        .where(m.PreflightResult.audit_run_id == a.id)
        .order_by(m.PreflightResult.created_at.desc())
        .limit(1)
    )
    if pf is None:
        raise not_found()
    pf.override_status, pf.override_reason, pf.override_by, pf.override_at = (
        body.status,
        body.reason,
        p.user_id,
        datetime.now(UTC),
    )
    ledger.record(
        s,
        org_id=v.org_id,
        event_type="preflight.overridden",
        entity_type="preflight_result",
        entity_id=pf.id,
        actor_id=p.user_id,
        dataset_version_id=v.id,
        payload={"from": str(pf.status), "to": body.status, "reason": body.reason},
    )
    ledger.security_event(
        s, "preflight.override", org_id=v.org_id, actor_id=p.user_id, target_type="preflight", target_id=pf.id
    )
    s.commit()
    return {"ok": True, "effective_status": body.status}


@router.get("/versions/{version_id}/dna")
def dna(
    version_id: uuid.UUID,
    compare_to: uuid.UUID | None = None,
    p: Principal = Depends(current_principal),
    s: Session = Depends(get_db),
) -> dict:
    v, a = _ctx(s, p, version_id, None)
    prof = s.scalar(select(m.DatasetDnaProfile).where(m.DatasetDnaProfile.audit_run_id == a.id))
    if prof is None:
        raise not_found()
    profile = {k: val for k, val in prof.profile.items() if k != "per_class"}
    profile["embedding"] = {"dispersion": prof.profile["embedding"]["dispersion"]}
    profile["per_class"] = {
        c: {k: x for k, x in d.items() if k != "centroid"} for c, d in prof.profile["per_class"].items()
    }
    other = None
    if compare_to is None:
        prev = s.scalar(
            select(m.DatasetVersion)
            .where(
                m.DatasetVersion.dataset_id == v.dataset_id,
                m.DatasetVersion.version_number < v.version_number,
            )
            .order_by(m.DatasetVersion.version_number.desc())
            .limit(1)
        )
        other = prev
    else:
        other = get_version(s, p, compare_to)
        if other.dataset_id != v.dataset_id:
            raise HTTPException(status_code=422, detail="compare_to must be a version of the same dataset")
    drift = None
    if other is not None:
        try:
            oa = resolve_audit(s, other, None)
            op = s.scalar(select(m.DatasetDnaProfile).where(m.DatasetDnaProfile.audit_run_id == oa.id))
            if op:
                drift = dna_mod.compare(
                    op.profile,
                    prof.profile,
                    label_a=f"v{other.version_number}",
                    label_b=f"v{v.version_number}",
                )
                drift["compared_to"] = {
                    "version_id": str(other.id),
                    "version": other.version_number,
                    "fingerprint": op.fingerprint,
                }
        except HTTPException:
            drift = None
    return {
        "audit_id": str(a.id),
        "fingerprint": prof.fingerprint,
        "dna_version": prof.dna_version,
        "profile": profile,
        "drift": drift,
        "note": "Dataset DNA is a descriptive profile for comparing versions — not a cryptographic identity. "
        "Use the manifest SHA-256 for exact identity.",
    }


# ---- contamination -----------------------------------------------------------------------------


class ContaminationIn(BaseModel):
    reference_version_id: uuid.UUID


@router.post("/versions/{version_id}/contamination-checks")
def start_contamination(
    version_id: uuid.UUID,
    body: ContaminationIn,
    p: Principal = Depends(not_demo),
    s: Session = Depends(get_db),
) -> dict:
    v = get_version(s, p, version_id, Role.REVIEWER)
    ref = get_version(s, p, body.reference_version_id)
    if ref.org_id != v.org_id:
        raise not_found()
    chk = m.ContaminationCheck(
        org_id=v.org_id,
        source_version_id=v.id,
        reference_version_id=ref.id,
        status=RunStatus.QUEUED,
        created_by=p.user_id,
    )
    s.add(chk)
    s.flush()
    jobs.enqueue(s, JobType.CONTAMINATION, {"check_id": str(chk.id)}, org_id=v.org_id, priority=110)
    s.commit()
    return {"id": str(chk.id), "status": str(chk.status)}


@router.get("/versions/{version_id}/contamination-checks")
def list_contamination(
    version_id: uuid.UUID, p: Principal = Depends(current_principal), s: Session = Depends(get_db)
) -> list[dict]:
    v = get_version(s, p, version_id)
    rows = s.scalars(
        select(m.ContaminationCheck)
        .where(m.ContaminationCheck.source_version_id == v.id)
        .order_by(m.ContaminationCheck.created_at.desc())
    ).all()
    names = {}
    for r in rows:
        rv = s.get(m.DatasetVersion, r.reference_version_id)
        d = s.get(m.Dataset, rv.dataset_id) if rv else None
        names[r.id] = f"{d.name} v{rv.version_number}" if d and rv else "?"
    return [
        {
            "id": str(r.id),
            "status": str(r.status),
            "reference": names[r.id],
            "reference_version_id": str(r.reference_version_id),
            "result": r.result,
            "created_at": iso(r.created_at),
        }
        for r in rows
    ]


@router.get("/orgs/{org_id}/versions")
def org_versions(
    org_id: uuid.UUID, p: Principal = Depends(current_principal), s: Session = Depends(get_db)
) -> list[dict]:
    """All ready dataset versions in a workspace (for choosing contamination references)."""
    from datacourt.tenancy import require_org

    require_org(s, p, org_id)
    rows = s.execute(
        select(m.DatasetVersion, m.Dataset.name)
        .join(m.Dataset, m.Dataset.id == m.DatasetVersion.dataset_id)
        .where(m.DatasetVersion.org_id == org_id, m.DatasetVersion.status == "ready")
        .order_by(m.Dataset.name, m.DatasetVersion.version_number)
    ).all()
    return [
        {
            "id": str(v.id),
            "dataset": name,
            "version_number": v.version_number,
            "samples": (v.stats or {}).get("sample_count"),
        }
        for v, name in rows
    ]


__all__ = ["Counter", "router"]
