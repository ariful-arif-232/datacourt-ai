from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from datacourt import execution, jobs
from datacourt.api.deps import current_principal, not_demo
from datacourt.api.views import audit_view
from datacourt.config import get_settings
from datacourt.db import models as m
from datacourt.db.session import get_db
from datacourt.enums import AuditProfile, Role, RunStatus, VersionStatus
from datacourt.pipeline.config import ConfigError
from datacourt.services.audits import audit_timeout_seconds, create_audit, estimate_workload
from datacourt.tenancy import Principal, get_audit, get_version

router = APIRouter(tags=["audits"])


class AuditIn(BaseModel):
    dataset_version_id: uuid.UUID
    profile: AuditProfile = AuditProfile.FAST
    overrides: dict | None = None


@router.post("/audits")
def start_audit(body: AuditIn, p: Principal = Depends(not_demo), s: Session = Depends(get_db)) -> dict:
    v = get_version(s, p, body.dataset_version_id, Role.REVIEWER)
    if v.status != VersionStatus.READY:
        raise HTTPException(
            status_code=409, detail=f"dataset version is {v.status}; wait for ingestion to finish"
        )
    running = s.scalar(
        select(m.AuditRun).where(
            m.AuditRun.dataset_version_id == v.id,
            m.AuditRun.status.in_([RunStatus.QUEUED, RunStatus.RUNNING]),
        )
    )
    if running is not None:
        raise HTTPException(status_code=409, detail="an audit is already queued or running for this version")
    try:
        a = create_audit(s, v, body.profile, created_by=p.user_id, overrides=body.overrides)
    except ConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    s.commit()
    return audit_view(a)


@router.get("/versions/{version_id}/audit-estimate")
def audit_estimate(
    version_id: uuid.UUID,
    profile: AuditProfile = AuditProfile.FAST,
    p: Principal = Depends(current_principal),
    s: Session = Depends(get_db),
) -> dict:
    v = get_version(s, p, version_id)
    return estimate_workload(
        int((v.stats or {}).get("sample_count", 0)), profile, get_settings().embedding_backend
    )


@router.get("/audits/{audit_id}")
def get_audit_detail(
    audit_id: uuid.UUID, p: Principal = Depends(current_principal), s: Session = Depends(get_db)
) -> dict:
    a = get_audit(s, p, audit_id)
    steps = s.scalars(select(m.AuditPipelineStep).where(m.AuditPipelineStep.audit_run_id == a.id)).all()
    out = audit_view(a, steps)
    out["config"] = a.config
    return out


@router.get("/audits/{audit_id}/progress")
def audit_progress(
    audit_id: uuid.UUID, p: Principal = Depends(current_principal), s: Session = Depends(get_db)
) -> dict:
    a = get_audit(s, p, audit_id)
    if a.status in (RunStatus.QUEUED, RunStatus.RUNNING):
        execution.maybe_redispatch()
    steps = s.scalars(select(m.AuditPipelineStep).where(m.AuditPipelineStep.audit_run_id == a.id)).all()
    job = s.get(m.Job, a.job_id) if a.job_id else None
    elapsed = None
    if a.started_at:
        elapsed = ((a.finished_at or datetime.now(UTC)) - a.started_at).total_seconds()
    progress = max(a.progress, job.progress if job and a.status == RunStatus.RUNNING else 0.0)
    return {
        "status": str(a.status),
        "progress": round(progress, 4),
        "current_stage": job.stage if job and job.stage else a.current_stage,
        "elapsed_seconds": round(elapsed, 1) if elapsed is not None else None,
        "warnings": a.warnings,
        "error": a.error,
        "job": execution.status_for(job),
        "steps": [
            {
                "stage": x.stage,
                "status": str(x.status),
                "duration_ms": x.duration_ms,
                "error": x.error,
                "attempts": x.attempts,
                "warnings": x.warnings,
                "reason": (x.output or {}).get("reason"),
            }
            for x in sorted(steps, key=lambda x: x.order)
        ],
    }


@router.post("/audits/{audit_id}/cancel")
def cancel_audit(audit_id: uuid.UUID, p: Principal = Depends(not_demo), s: Session = Depends(get_db)) -> dict:
    a = get_audit(s, p, audit_id, Role.REVIEWER)
    if a.status not in (RunStatus.QUEUED, RunStatus.RUNNING) or a.job_id is None:
        raise HTTPException(status_code=409, detail="audit is not running")
    jobs.request_cancel(s, a.job_id)
    if a.status == RunStatus.QUEUED:
        a.status = RunStatus.CANCELLED
    s.commit()
    return {"ok": True}


@router.post("/audits/{audit_id}/retry")
def retry_audit(audit_id: uuid.UUID, p: Principal = Depends(not_demo), s: Session = Depends(get_db)) -> dict:
    """Resume a failed/cancelled audit: completed stages are kept, the rest re-run."""
    a = get_audit(s, p, audit_id, Role.REVIEWER)
    if a.status not in (RunStatus.FAILED, RunStatus.CANCELLED):
        raise HTTPException(status_code=409, detail="only failed or cancelled audits can be resumed")
    version = s.get(m.DatasetVersion, a.dataset_version_id)
    assert version is not None
    job = jobs.enqueue(
        s,
        jobs.JobType.RUN_AUDIT,
        {"audit_run_id": str(a.id)},
        org_id=a.org_id,
        priority=85,
        idempotency_key=f"audit:{a.id}:retry:{datetime.now(UTC).timestamp():.0f}",
        timeout_seconds=audit_timeout_seconds(version, a.profile),
    )
    a.status = RunStatus.QUEUED
    a.error = None
    a.job_id = job.id
    s.commit()
    return audit_view(a)
