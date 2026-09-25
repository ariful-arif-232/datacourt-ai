"""What-if Lab, exports, reports, version comparison."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from datacourt import execution, jobs
from datacourt.api.deps import current_principal, not_demo
from datacourt.api.views import iso
from datacourt.db import models as m
from datacourt.db.session import get_db
from datacourt.enums import JobType, Role, RunStatus
from datacourt.services.versions import compute_diff
from datacourt.storage import get_store
from datacourt.tenancy import Principal, get_owned, get_version, not_found, resolve_audit
from datacourt.whatif.actions import ACTION_TYPES, InvalidChangeSet, validate_actions

router = APIRouter(tags=["experiments"])


class WhatIfIn(BaseModel):
    dataset_version_id: uuid.UUID
    name: str = Field(default="What-if experiment", max_length=160)
    actions: list[dict]
    seeds: int = Field(default=3, ge=1, le=10)
    failure_event_id: uuid.UUID | None = None
    source: str = Field(default="lab", pattern="^(lab|failure_replay|counterfactual|case)$")


def _whatif_view(s: Session, w: m.WhatIfRun, detail: bool = False) -> dict:
    out = {
        "id": str(w.id),
        "name": w.name,
        "source": w.source,
        "status": str(w.status),
        "config": w.config,
        "summary": w.summary,
        "error": w.error,
        "created_at": iso(w.created_at),
        "finished_at": iso(w.finished_at),
        "failure_event_id": str(w.failure_event_id) if w.failure_event_id else None,
    }
    job = s.get(m.Job, w.job_id) if w.job_id else None
    out["progress"] = job.progress if job else None
    out["execution"] = _execution(job)
    if detail:
        out["actions"] = [
            {"type": a.action_type, "params": a.params, "affected": a.affected_count}
            for a in s.scalars(select(m.WhatIfAction).where(m.WhatIfAction.run_id == w.id))
        ]
        # Stored rows hold {"n", "metrics", "bootstrap"}; expose them flat so clients read
        # result["metrics"]["per_class"] directly.
        out["results"] = [
            {
                "variant": r.variant,
                "eval_set": r.eval_set,
                "n": (r.metrics or {}).get("n"),
                "metrics": (r.metrics or {}).get("metrics", {}),
                "bootstrap": (r.metrics or {}).get("bootstrap"),
            }
            for r in s.scalars(select(m.WhatIfResult).where(m.WhatIfResult.run_id == w.id))
        ]
    return out


@router.get("/what-if/action-types")
def action_types() -> dict:
    return {
        "types": sorted(ACTION_TYPES),
        "descriptions": {
            "remove_samples": "Exclude specific samples ({sample_ids}).",
            "relabel_samples": "Relabel samples ({items: [{sample_id, target_class}]}).",
            "remove_duplicate_copies": "Keep only the source-like member of duplicate families in training ({family_ids?}).",
            "exclude_quality": "Exclude training samples with quality findings at or above {min_severity}.",
            "rebalance": "Cap samples per class ({max_per_class}) and/or toggle class-balanced loss ({class_balanced}).",
            "preserve_rare": "Protect samples judged likely rare-and-valid from every removal/relabel in the set.",
            "move_leakage_out_of_eval": "Drop evaluation copies involved in leakage at or above {min_risk}.",
            "apply_review_decisions": "Apply current final human decisions (remove/relabel).",
            "apply_jury_suggestions": "Apply POSSIBLE_RELABEL / POSSIBLE_REMOVE suggestions without review (experiment only).",
        },
    }


@router.post("/what-if")
def create_what_if(body: WhatIfIn, p: Principal = Depends(not_demo), s: Session = Depends(get_db)) -> dict:
    v = get_version(s, p, body.dataset_version_id, Role.REVIEWER)
    a = resolve_audit(s, v, None)
    try:
        validate_actions(body.actions)
    except InvalidChangeSet as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if body.failure_event_id:
        fe = s.get(m.FailureEvent, body.failure_event_id)
        if fe is None or fe.audit_run_id != a.id:
            raise not_found()
    w = m.WhatIfRun(
        org_id=v.org_id,
        dataset_version_id=v.id,
        audit_run_id=a.id,
        name=body.name,
        source=body.source,
        failure_event_id=body.failure_event_id,
        status=RunStatus.QUEUED,
        config={"seeds": body.seeds},
        created_by=p.user_id,
    )
    s.add(w)
    s.flush()
    for act in body.actions:
        s.add(
            m.WhatIfAction(
                run_id=w.id, action_type=act["type"], params={k: val for k, val in act.items() if k != "type"}
            )
        )
    job = jobs.enqueue(s, JobType.WHAT_IF, {"what_if_run_id": str(w.id)}, org_id=v.org_id, priority=60)
    w.job_id = job.id
    s.commit()
    return _whatif_view(s, w)


@router.get("/versions/{version_id}/what-if")
def list_what_if(
    version_id: uuid.UUID, p: Principal = Depends(current_principal), s: Session = Depends(get_db)
) -> list[dict]:
    v = get_version(s, p, version_id)
    return [
        _whatif_view(s, w)
        for w in s.scalars(
            select(m.WhatIfRun)
            .where(m.WhatIfRun.dataset_version_id == v.id)
            .order_by(m.WhatIfRun.created_at.desc())
            .limit(50)
        )
    ]


@router.get("/what-if/{run_id}")
def get_what_if(
    run_id: uuid.UUID, p: Principal = Depends(current_principal), s: Session = Depends(get_db)
) -> dict:
    return _whatif_view(s, get_owned(s, p, m.WhatIfRun, run_id), detail=True)


# ---- exports -----------------------------------------------------------------------------


class ExportIn(BaseModel):
    dataset_version_id: uuid.UUID
    auto_audit: str | None = Field(default="fast", pattern="^(fast|deep)$")


@router.post("/exports")
def create_export(body: ExportIn, p: Principal = Depends(not_demo), s: Session = Depends(get_db)) -> dict:
    v = get_version(s, p, body.dataset_version_id, Role.ADMIN)
    a = resolve_audit(s, v, None)
    ex = m.Export(
        org_id=v.org_id,
        dataset_version_id=v.id,
        audit_run_id=a.id,
        status=RunStatus.QUEUED,
        name="pending",
        created_by=p.user_id,
    )
    s.add(ex)
    s.flush()
    job = jobs.enqueue(
        s,
        JobType.EXPORT,
        {"export_id": str(ex.id), "auto_audit": body.auto_audit},
        org_id=v.org_id,
        priority=70,
    )
    ex.job_id = job.id
    s.commit()
    return _export_view(s, ex)


def _execution(job: m.Job | None) -> dict | None:
    """Worker status while a job is pending or running (and a re-dispatch check)."""
    if job is None or str(job.status) not in ("queued", "running"):
        return None
    execution.maybe_redispatch()
    return execution.status_for(job)


def _export_view(s: Session, ex: m.Export) -> dict:
    job = s.get(m.Job, ex.job_id) if ex.job_id else None
    return {
        "id": str(ex.id),
        "status": str(ex.status),
        "name": ex.name,
        "byte_size": ex.byte_size,
        "sha256": ex.sha256,
        "summary": {k: v for k, v in (ex.summary or {}).items() if k != "applied_case_ids"},
        "new_version_id": str(ex.new_version_id) if ex.new_version_id else None,
        "error": ex.error,
        "progress": job.progress if job else None,
        "execution": _execution(job),
        "expired": bool((ex.summary or {}).get("archive_expired_at")),
        "created_at": iso(ex.created_at),
        "finished_at": iso(ex.finished_at),
    }


@router.get("/versions/{version_id}/exports")
def list_exports(
    version_id: uuid.UUID, p: Principal = Depends(current_principal), s: Session = Depends(get_db)
) -> list[dict]:
    v = get_version(s, p, version_id)
    return [
        _export_view(s, ex)
        for ex in s.scalars(
            select(m.Export).where(m.Export.dataset_version_id == v.id).order_by(m.Export.created_at.desc())
        )
    ]


@router.get("/exports/{export_id}/download")
def download_export(
    export_id: uuid.UUID, p: Principal = Depends(current_principal), s: Session = Depends(get_db)
) -> dict:
    ex = get_owned(s, p, m.Export, export_id)
    if ex.status == RunStatus.COMPLETED and not ex.object_key:
        raise HTTPException(
            status_code=410, detail="export archive expired under the workspace retention policy"
        )
    if ex.status != RunStatus.COMPLETED or not ex.object_key:
        raise HTTPException(status_code=409, detail="export not ready")
    return {"url": get_store().signed_get_url(ex.object_key, ttl=600, filename=ex.name), "expires_in": 600}


# ---- reports ---------------------------------------------------------------------------------


class ReportIn(BaseModel):
    dataset_version_id: uuid.UUID


@router.post("/reports")
def create_report(body: ReportIn, p: Principal = Depends(not_demo), s: Session = Depends(get_db)) -> dict:
    v = get_version(s, p, body.dataset_version_id, Role.REVIEWER)
    a = resolve_audit(s, v, None)
    rep = m.AuditReport(org_id=v.org_id, audit_run_id=a.id, status=RunStatus.QUEUED, created_by=p.user_id)
    s.add(rep)
    s.flush()
    job = jobs.enqueue(s, JobType.REPORT, {"report_id": str(rep.id)}, org_id=v.org_id, priority=70)
    rep.job_id = job.id
    s.commit()
    return _report_view(rep)


def _report_view(r: m.AuditReport) -> dict:
    return {
        "id": str(r.id),
        "audit_run_id": str(r.audit_run_id),
        "status": str(r.status),
        "error": r.error,
        "expired": r.status == RunStatus.COMPLETED and not r.html_object_key,
        "created_at": iso(r.created_at),
    }


@router.get("/versions/{version_id}/reports")
def list_reports(
    version_id: uuid.UUID, p: Principal = Depends(current_principal), s: Session = Depends(get_db)
) -> list[dict]:
    v = get_version(s, p, version_id)
    rows = s.scalars(
        select(m.AuditReport)
        .join(m.AuditRun, m.AuditRun.id == m.AuditReport.audit_run_id)
        .where(m.AuditRun.dataset_version_id == v.id)
        .order_by(m.AuditReport.created_at.desc())
    ).all()
    return [_report_view(r) for r in rows]


@router.get("/reports/{report_id}")
def get_report(
    report_id: uuid.UUID, p: Principal = Depends(current_principal), s: Session = Depends(get_db)
) -> dict:
    r = get_owned(s, p, m.AuditReport, report_id)
    out = _report_view(r)
    if r.status == RunStatus.COMPLETED:
        store = get_store()
        out["html_url"] = store.signed_get_url(r.html_object_key, ttl=600) if r.html_object_key else None
        out["html_download_url"] = (
            store.signed_get_url(r.html_object_key, ttl=600, filename="datacourt-audit-report.html")
            if r.html_object_key
            else None
        )
        out["json_url"] = (
            store.signed_get_url(r.json_object_key, ttl=600, filename="datacourt-audit-report.json")
            if r.json_object_key
            else None
        )
    return out


# ---- version comparison --------------------------------------------------------------------------


@router.get("/versions/{version_id}/compare")
def compare_versions(
    version_id: uuid.UUID,
    other: uuid.UUID | None = None,
    p: Principal = Depends(current_principal),
    s: Session = Depends(get_db),
) -> dict:
    v = get_version(s, p, version_id)
    if other is None:
        prev = s.scalar(
            select(m.DatasetVersion)
            .where(
                m.DatasetVersion.dataset_id == v.dataset_id,
                m.DatasetVersion.version_number < v.version_number,
            )
            .order_by(m.DatasetVersion.version_number.desc())
            .limit(1)
        )
        if prev is None:
            return {"available": False, "reason": "This is the first version of the dataset."}
        a, b = prev, v
    else:
        o = get_version(s, p, other)
        if o.dataset_id != v.dataset_id:
            raise HTTPException(status_code=422, detail="versions must belong to the same dataset")
        a, b = (o, v) if o.version_number < v.version_number else (v, o)
    cached = s.scalar(
        select(m.DatasetVersionDiff).where(
            m.DatasetVersionDiff.from_version_id == a.id, m.DatasetVersionDiff.to_version_id == b.id
        )
    )
    diff = compute_diff(s, a.id, b.id)
    if cached is None:
        s.add(m.DatasetVersionDiff(from_version_id=a.id, to_version_id=b.id, diff=diff))
    else:
        cached.diff = diff
    s.commit()
    return {"available": True, **diff}
