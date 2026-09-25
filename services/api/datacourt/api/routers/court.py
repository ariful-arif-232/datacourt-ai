from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from datacourt.api.deps import current_principal, not_demo, rate_limit
from datacourt.api.views import case_summary, iso, sample_view
from datacourt.db import models as m
from datacourt.db.session import get_db
from datacourt.enums import DecisionAction, Role
from datacourt.pipeline.context import AuditContext, load_samples
from datacourt.services import counterfactual, explain, review
from datacourt.tenancy import Principal, get_case, get_version, not_found, require_org, resolve_audit

router = APIRouter(tags=["court"])

SORTS = {
    "priority": (m.CourtCase.priority_score.desc(),),
    "impact": (m.CourtCase.impact_score.desc(), m.CourtCase.priority_score.desc()),
    "newest": (m.CourtCase.updated_at.desc(),),
    "uncertainty": (m.CourtCase.uncertainty.desc(), m.CourtCase.priority_score.desc()),
    "case": (m.CourtCase.case_number,),
    "class": (m.Sample.label, m.CourtCase.priority_score.desc()),
    "name": (m.Sample.relative_path,),
}


@router.get("/versions/{version_id}/cases")
def list_cases(
    version_id: uuid.UUID,
    audit_id: uuid.UUID | None = None,
    verdict: list[str] | None = Query(None),
    status: str | None = None,
    category: str | None = None,
    class_name: str | None = None,
    split: str | None = None,
    uncertainty: str | None = None,
    family_id: uuid.UUID | None = None,
    reason_code: str | None = None,
    q: str | None = Query(None, max_length=200),
    sort: str = Query("priority"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    p: Principal = Depends(current_principal),
    s: Session = Depends(get_db),
) -> dict:
    v = get_version(s, p, version_id)
    a = resolve_audit(s, v, audit_id)
    stmt = (
        select(m.CourtCase, m.Sample)
        .join(m.Sample, m.Sample.id == m.CourtCase.sample_id)
        .where(m.CourtCase.audit_run_id == a.id)
    )
    if verdict:
        stmt = stmt.where(m.CourtCase.verdict.in_(verdict))
    if status:
        stmt = stmt.where(m.CourtCase.status == status)
    if category:
        stmt = stmt.where(m.CourtCase.categories.contains([category]))
    if reason_code:
        stmt = stmt.where(m.CourtCase.reason_codes.contains([reason_code]))
    if class_name:
        stmt = stmt.where(m.Sample.label == class_name)
    if split:
        stmt = stmt.where(m.Sample.split == split)
    if uncertainty:
        stmt = stmt.where(m.CourtCase.uncertainty == uncertainty)
    if family_id:
        stmt = stmt.where(m.CourtCase.family_id == family_id)
    if q:
        if q.isdigit():
            stmt = stmt.where(m.CourtCase.case_number == int(q))
        else:
            stmt = stmt.where(or_(m.Sample.relative_path.ilike(f"%{q}%"), m.Sample.sha256 == q.lower()))
    total = s.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = s.execute(stmt.order_by(*SORTS.get(sort, SORTS["priority"])).offset(offset).limit(limit)).all()
    counts = s.execute(
        select(m.CourtCase.verdict, m.CourtCase.status, func.count())
        .where(m.CourtCase.audit_run_id == a.id)
        .group_by(m.CourtCase.verdict, m.CourtCase.status)
    ).all()
    return {
        "audit_id": str(a.id),
        "total": total,
        "offset": offset,
        "limit": limit,
        "counts": [{"verdict": str(vv), "status": str(st), "count": c} for vv, st, c in counts],
        "items": [case_summary(c, x) for c, x in rows],
    }


def _decisions_view(s: Session, case_id: uuid.UUID) -> list[dict]:
    rows = s.scalars(
        select(m.HumanDecision).where(m.HumanDecision.case_id == case_id).order_by(m.HumanDecision.created_at)
    ).all()
    users = (
        {u.id: u.name for u in s.scalars(select(m.User).where(m.User.id.in_({r.reviewer_id for r in rows})))}
        if rows
        else {}
    )
    classes = {
        c.id: c.name
        for c in s.scalars(
            select(m.DatasetClass).where(
                m.DatasetClass.id.in_({r.target_class_id for r in rows if r.target_class_id})
            )
        )
    }
    return [
        {
            "id": str(r.id),
            "reviewer_id": str(r.reviewer_id),
            "reviewer": users.get(r.reviewer_id),
            "action": str(r.action),
            "target_class": classes.get(r.target_class_id),
            "note": r.note,
            "adjudication": r.is_adjudication,
            "created_at": iso(r.created_at),
            "undone_at": iso(r.undone_at),
            "evidence_snapshot": r.evidence_snapshot,
        }
        for r in rows
    ]


@router.get("/cases/{case_id}")
def case_detail(
    case_id: uuid.UUID, p: Principal = Depends(current_principal), s: Session = Depends(get_db)
) -> dict:
    c = get_case(s, p, case_id)
    sample = s.get(m.Sample, c.sample_id)
    assert sample is not None
    evidence = s.scalars(select(m.CaseEvidence).where(m.CaseEvidence.case_id == c.id)).all()
    verdict = s.scalar(
        select(m.CaseVerdict)
        .where(m.CaseVerdict.case_id == c.id)
        .order_by(m.CaseVerdict.created_at.desc())
        .limit(1)
    )
    expl = s.scalar(
        select(m.CaseExplanation)
        .where(m.CaseExplanation.case_id == c.id)
        .order_by(m.CaseExplanation.created_at.desc())
        .limit(1)
    )
    notes = s.execute(
        select(m.ReviewerNote, m.User.name)
        .join(m.User, m.User.id == m.ReviewerNote.user_id)
        .where(m.ReviewerNote.case_id == c.id)
        .order_by(m.ReviewerNote.created_at)
    ).all()
    classes = s.scalars(
        select(m.DatasetClass)
        .where(m.DatasetClass.dataset_version_id == c.dataset_version_id)
        .order_by(m.DatasetClass.index)
    ).all()
    pred = s.scalar(
        select(m.ModelPrediction).where(
            m.ModelPrediction.audit_run_id == c.audit_run_id, m.ModelPrediction.sample_id == c.sample_id
        )
    )
    rw = s.scalar(
        select(m.RareWrongFinding).where(
            m.RareWrongFinding.audit_run_id == c.audit_run_id, m.RareWrongFinding.sample_id == c.sample_id
        )
    )
    # Visual neighbours for side-by-side comparison.
    neighbors = []
    lf = s.scalar(
        select(m.LabelFinding).where(
            m.LabelFinding.audit_run_id == c.audit_run_id, m.LabelFinding.sample_id == c.sample_id
        )
    )
    fam_members = []
    if c.family_id:
        mem = s.scalars(
            select(m.DuplicateFamilyMember).where(m.DuplicateFamilyMember.family_id == c.family_id)
        ).all()
        sm = {x.id: x for x in s.scalars(select(m.Sample).where(m.Sample.id.in_([y.sample_id for y in mem])))}
        fam_members = [
            {
                "sample": sample_view(sm[y.sample_id]),
                "relation": y.relation,
                "is_root": y.parent_sample_id is None,
            }
            for y in mem
            if y.sample_id != c.sample_id
        ][:8]
    try:
        a = s.get(m.AuditRun, c.audit_run_id)
        v = s.get(m.DatasetVersion, c.dataset_version_id)
        ctx = AuditContext(a.id, a.org_id, v.id, v.dataset_id, str(a.profile), a.config, load_samples(v.id))
        kn = ctx.load_npz("knn")
        i = ctx.samples.ids.index(c.sample_id)
        ids = [ctx.samples.ids[j] for j in kn["idx"][i][:8] if j >= 0]
        sims = [float(x) for x in kn["sims"][i][:8]]
        sm = {x.id: x for x in s.scalars(select(m.Sample).where(m.Sample.id.in_(ids)))}
        neighbors = [
            {"sample": sample_view(sm[sid]), "similarity": round(sim, 4)}
            for sid, sim in zip(ids, sims, strict=False)
            if sid in sm
        ]
    except Exception:  # noqa: BLE001 - neighbours are a convenience, not required
        neighbors = []
    siblings = s.execute(
        select(m.CourtCase.id, m.CourtCase.case_number)
        .where(m.CourtCase.audit_run_id == c.audit_run_id)
        .order_by(m.CourtCase.priority_score.desc())
    ).all()
    order = [str(x.id) for x in siblings]
    pos = order.index(str(c.id))
    return {
        **case_summary(c),
        "audit_id": str(c.audit_run_id),
        "dataset_version_id": str(c.dataset_version_id),
        "sample": sample_view(sample, full=True),
        "classes": [{"id": str(x.id), "name": x.name} for x in classes],
        "prediction": {"top_probs": pred.top_probs, "prob_given": pred.prob_given, "correct": pred.correct}
        if pred
        else None,
        "evidence": [
            {
                "id": str(e.id),
                "witness": str(e.witness),
                "stance": str(e.stance),
                "evidence_kind": str(e.evidence_kind),
                "code": e.code,
                "title": e.title,
                "detail": e.detail,
                "value": e.value,
                "weight": e.weight,
            }
            for e in evidence
        ],
        "verdict_record": {
            "jury_version": verdict.jury_version,
            "config_hash": verdict.config_hash,
            "rule_trace": verdict.rule_trace,
            "scores": verdict.scores,
            "reason_codes": verdict.reason_codes,
            "created_at": iso(verdict.created_at),
        }
        if verdict
        else None,
        "explanation": {
            "source": str(expl.source),
            "model": expl.model,
            "content": expl.content,
            "created_at": iso(expl.created_at),
        }
        if expl
        else None,
        "rare_or_wrong": {
            "hypothesis": str(rw.hypothesis),
            "scores": rw.scores,
            "reasons": rw.reasons,
            "valuable_reasons": rw.valuable_reasons,
        }
        if rw
        else None,
        "label_evidence": lf.evidence if lf else None,
        "neighbors": neighbors,
        "family_members": fam_members,
        "decisions": _decisions_view(s, c.id),
        "final_decision": (lambda d: {"id": str(d.id), "action": str(d.action)} if d else None)(
            review.final_decision(s, c.id)
        ),
        "notes": [
            {"id": str(n.id), "author": name, "body": n.body, "created_at": iso(n.created_at)}
            for n, name in notes
        ],
        "navigation": {
            "position": pos + 1,
            "total": len(order),
            "previous": order[pos - 1] if pos > 0 else None,
            "next": order[pos + 1] if pos + 1 < len(order) else None,
        },
    }


@router.post("/cases/{case_id}/explain", dependencies=[Depends(rate_limit("explain", 30))])
def explain_case(
    case_id: uuid.UUID, p: Principal = Depends(current_principal), s: Session = Depends(get_db)
) -> dict:
    c = get_case(s, p, case_id, Role.VIEWER)
    sample = s.get(m.Sample, c.sample_id)
    assert sample is not None
    ev = [
        {
            "witness": str(e.witness),
            "stance": str(e.stance),
            "evidence_kind": str(e.evidence_kind),
            "title": e.title,
            "detail": e.detail,
            "value": e.value,
        }
        for e in s.scalars(select(m.CaseEvidence).where(m.CaseEvidence.case_id == c.id))
    ]
    case = {
        "case_number": c.case_number,
        "verdict": str(c.verdict),
        "reason_codes": c.reason_codes,
        "uncertainty": str(c.uncertainty),
    }
    payload_hash = explain.sha256_json(
        explain.build_evidence_payload(case, ev, {"label": sample.label, "split": str(sample.split)})
    )
    cached = s.scalar(
        select(m.CaseExplanation)
        .where(m.CaseExplanation.case_id == c.id, m.CaseExplanation.input_hash == payload_hash)
        .order_by(m.CaseExplanation.created_at.desc())
        .limit(1)
    )
    if cached is not None and cached.source == "gemini":
        return {
            "source": str(cached.source),
            "model": cached.model,
            "content": cached.content,
            "cached": True,
        }
    out = explain.explain(case, ev, {"label": sample.label, "split": str(sample.split)})
    s.add(
        m.CaseExplanation(
            case_id=c.id,
            source=out["source"],
            model=out["model"],
            input_hash=out["input_hash"],
            content=out["content"],
            created_by=p.user_id,
        )
    )
    s.commit()
    return {**out, "cached": False}


class DecisionIn(BaseModel):
    action: DecisionAction
    target_class_id: uuid.UUID | None = None
    note: str = Field(default="", max_length=4000)
    adjudicate: bool = False


@router.post("/cases/{case_id}/decision")
def decide(
    case_id: uuid.UUID, body: DecisionIn, p: Principal = Depends(not_demo), s: Session = Depends(get_db)
) -> dict:
    if p.user is None:
        raise HTTPException(status_code=403, detail="decisions require a user session")
    c = get_case(s, p, case_id, Role.REVIEWER)
    if body.adjudicate:
        require_org(s, p, c.org_id, Role.ADMIN)
    try:
        d = review.record_decision(
            s,
            c,
            p.user.id,
            body.action,
            target_class_id=body.target_class_id,
            note=body.note,
            adjudicate=body.adjudicate,
        )
    except review.ReviewError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    s.commit()
    return {"decision_id": str(d.id), "case_status": str(c.status), "decisions": _decisions_view(s, c.id)}


@router.post("/decisions/{decision_id}/undo")
def undo(decision_id: uuid.UUID, p: Principal = Depends(not_demo), s: Session = Depends(get_db)) -> dict:
    d = s.get(m.HumanDecision, decision_id)
    if d is None:
        raise not_found()
    c = get_case(s, p, d.case_id, Role.REVIEWER)
    if p.user is None or (
        d.reviewer_id != p.user.id
        and require_org(s, p, c.org_id, Role.REVIEWER) not in (Role.ADMIN, Role.OWNER)
    ):
        raise HTTPException(status_code=403, detail="only the reviewer or an admin can undo a decision")
    try:
        review.undo_decision(s, d, p.user.id)
    except review.ReviewError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    s.commit()
    return {"case_status": str(c.status)}


class NoteIn(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


@router.post("/cases/{case_id}/notes")
def add_note(
    case_id: uuid.UUID, body: NoteIn, p: Principal = Depends(not_demo), s: Session = Depends(get_db)
) -> dict:
    c = get_case(s, p, case_id, Role.REVIEWER)
    if p.user is None:
        raise HTTPException(status_code=403, detail="notes require a user session")
    n = m.ReviewerNote(case_id=c.id, user_id=p.user.id, body=body.body)
    s.add(n)
    s.commit()
    return {"id": str(n.id)}


@router.get("/cases/{case_id}/counterfactuals")
def counterfactuals(
    case_id: uuid.UUID, p: Principal = Depends(current_principal), s: Session = Depends(get_db)
) -> dict:
    c = get_case(s, p, case_id)
    a = s.get(m.AuditRun, c.audit_run_id)
    v = s.get(m.DatasetVersion, c.dataset_version_id)
    assert a is not None and v is not None
    ctx = AuditContext(a.id, a.org_id, v.id, v.dataset_id, str(a.profile), a.config, load_samples(v.id))
    S = ctx.samples
    i = S.ids.index(c.sample_id)
    leak = "leakage" in (c.categories or [])
    lf = s.scalar(
        select(m.LabelFinding).where(
            m.LabelFinding.audit_run_id == a.id, m.LabelFinding.sample_id == c.sample_id
        )
    )
    density = (lf.evidence or {}).get("density_percentile") if lf else None
    qualitative = counterfactual.qualitative_options(
        S.split[i], leak, str(c.verdict) == "LIKELY_RARE", density
    )
    if not ctx.has_artifact("cf_basis"):
        return {
            "available": False,
            "reason": "Model-impact estimates need the DEEP audit profile (influence analysis).",
            "qualitative": qualitative,
        }
    basis = ctx.load_npz("cf_basis")
    fit_idx = list(basis["fit_idx"])
    if i not in fit_idx:
        return {
            "available": False,
            "reason": "Estimates apply to training samples; this is an evaluation sample.",
            "qualitative": qualitative,
        }
    model = ctx.load_npz("model")
    n_eval = int(len(model["ev_idx"])) or len(fit_idx)
    est = counterfactual.estimate(basis, fit_idx.index(i), int(S.labels[i]), S.class_names, n_eval)
    return {"available": True, **est, "qualitative": qualitative}


@router.get("/audits/{audit_id}/agreement")
def agreement(
    audit_id: uuid.UUID, p: Principal = Depends(current_principal), s: Session = Depends(get_db)
) -> dict:
    a = s.get(m.AuditRun, audit_id)
    if a is None:
        raise not_found()
    require_org(s, p, a.org_id)
    stats = review.agreement_stats(s, a.id)
    disputed = s.execute(
        select(m.CourtCase, m.Sample)
        .join(m.Sample, m.Sample.id == m.CourtCase.sample_id)
        .where(m.CourtCase.audit_run_id == a.id, m.CourtCase.status == "disputed")
        .order_by(m.CourtCase.priority_score.desc())
        .limit(100)
    ).all()
    activity = s.execute(
        select(m.User.name, func.count())
        .join(m.HumanDecision, m.HumanDecision.reviewer_id == m.User.id)
        .join(m.CourtCase, m.CourtCase.id == m.HumanDecision.case_id)
        .where(m.CourtCase.audit_run_id == a.id, m.HumanDecision.undone_at.is_(None))
        .group_by(m.User.name)
    ).all()
    return {
        **stats,
        "disputed": [case_summary(c, x) for c, x in disputed],
        "reviewer_activity": [{"reviewer": n, "decisions": k} for n, k in activity],
    }
