from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session

from datacourt import execution, ledger
from datacourt.api.deps import current_principal, not_demo
from datacourt.api.views import iso, sample_view, version_view
from datacourt.config import get_settings
from datacourt.db import models as m
from datacourt.db.session import get_db
from datacourt.enums import JobType, Role, RunStatus, VersionStatus
from datacourt.ingest.queue import enqueue_ingest
from datacourt.services import bootstrap, purge
from datacourt.storage import ObjectNotFound, ObjectStore, get_store, source_zip_key
from datacourt.tenancy import Principal, get_dataset, get_project, get_sample, get_version, resolve_audit

router = APIRouter(tags=["datasets"])

PROVENANCE_KEYS = {
    "source",
    "license",
    "collection_method",
    "consent_note",
    "collector",
    "capture_date_range",
    "source_url",
}


class ProvenanceIn(BaseModel):
    source: str | None = Field(default=None, max_length=500)
    source_url: str | None = Field(default=None, max_length=1000)
    license: str | None = Field(default=None, max_length=200)
    collection_method: str | None = Field(default=None, max_length=1000)
    consent_note: str | None = Field(default=None, max_length=2000)
    collector: str | None = Field(default=None, max_length=200)
    capture_date_range: str | None = Field(default=None, max_length=100)


class DatasetIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)
    provenance: ProvenanceIn | None = None


@router.post("/projects/{project_id}/datasets")
def create_dataset(
    project_id: uuid.UUID, body: DatasetIn, p: Principal = Depends(not_demo), s: Session = Depends(get_db)
) -> dict:
    proj = get_project(s, p, project_id, Role.REVIEWER)
    if s.scalar(
        select(m.Dataset.id).where(m.Dataset.project_id == project_id, m.Dataset.name == body.name.strip())
    ):
        raise HTTPException(status_code=409, detail="a dataset with this name already exists in the project")
    d = m.Dataset(
        org_id=proj.org_id,
        project_id=proj.id,
        name=body.name.strip(),
        description=body.description,
        provenance=body.provenance.model_dump(exclude_none=True) if body.provenance else {},
        created_by=p.user_id,
    )
    s.add(d)
    s.commit()
    return {"id": str(d.id), "name": d.name}


@router.get("/datasets/{dataset_id}")
def dataset_detail(
    dataset_id: uuid.UUID, p: Principal = Depends(current_principal), s: Session = Depends(get_db)
) -> dict:
    d = get_dataset(s, p, dataset_id)
    versions = s.scalars(
        select(m.DatasetVersion)
        .where(m.DatasetVersion.dataset_id == d.id)
        .order_by(m.DatasetVersion.version_number.desc())
    ).all()
    out = []
    for v in versions:
        a = s.scalar(
            select(m.AuditRun)
            .where(m.AuditRun.dataset_version_id == v.id)
            .order_by(m.AuditRun.created_at.desc())
            .limit(1)
        )
        out.append(
            {
                **version_view(v),
                "latest_audit": {
                    "id": str(a.id),
                    "status": str(a.status),
                    "profile": str(a.profile),
                    "progress": a.progress,
                    "summary": a.summary,
                }
                if a
                else None,
            }
        )
    return {
        "id": str(d.id),
        "project_id": str(d.project_id),
        "org_id": str(d.org_id),
        "name": d.name,
        "description": d.description,
        "provenance": d.provenance,
        "created_at": iso(d.created_at),
        "versions": out,
    }


@router.put("/datasets/{dataset_id}/provenance")
def set_provenance(
    dataset_id: uuid.UUID, body: ProvenanceIn, p: Principal = Depends(not_demo), s: Session = Depends(get_db)
) -> dict:
    d = get_dataset(s, p, dataset_id, Role.ADMIN)
    d.provenance = body.model_dump(exclude_none=True)
    ledger.record(
        s,
        org_id=d.org_id,
        event_type="dataset.provenance_updated",
        entity_type="dataset",
        entity_id=d.id,
        actor_id=p.user_id,
        payload=d.provenance,
    )
    s.commit()
    return {"provenance": d.provenance}


class VersionIn(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)
    notes: str = Field(default="", max_length=4000)
    auto_audit: str | None = Field(default="fast", pattern="^(fast|deep)$")


@router.post("/datasets/{dataset_id}/versions")
def create_version(
    dataset_id: uuid.UUID, body: VersionIn, p: Principal = Depends(not_demo), s: Session = Depends(get_db)
) -> dict:
    d = get_dataset(s, p, dataset_id, Role.REVIEWER)
    settings = get_settings()
    if not body.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=415, detail="upload a .zip archive")
    if body.size_bytes > settings.max_upload_bytes:
        raise HTTPException(
            status_code=413, detail=f"archive exceeds the {settings.max_upload_bytes // 1024**2} MiB limit"
        )
    v = bootstrap.create_version(s, d, filename=body.filename, created_by=p.user_id, notes=body.notes)
    key = source_zip_key(d.org_id, d.id, v.id)
    v.source_object_key = key
    v.layout = {"requested_audit": body.auto_audit}
    upload = _upload_plan(get_store(), v, key, body.size_bytes)
    s.commit()
    return {"version": version_view(v), "upload": upload}


def _part_length(meta: dict, n: int) -> int:
    part, size = int(meta["part_bytes"]), int(meta["size"])
    return min(part, size - (n - 1) * part)


def _upload_plan(store: ObjectStore, v: m.DatasetVersion, key: str, size: int) -> dict:
    """How the browser uploads the archive: straight to the bucket with presigned URLs (never
    through the API), in parts when large so a failed part is retried alone."""
    settings = get_settings()
    ttl = settings.upload_url_ttl_seconds
    started = datetime.now(UTC).isoformat()
    if not store.supports_multipart or size <= settings.s3_multipart_threshold_bytes:
        v.upload_meta = {"mode": "single", "size": size, "started_at": started}
        return {"mode": "single", "expires_in": ttl, **store.signed_put(key, ttl, "application/zip", size)}
    part = settings.s3_multipart_part_bytes
    while math.ceil(size / part) > 10_000:
        part *= 2
    count = math.ceil(size / part)
    v.upload_id = store.create_multipart_upload(key, "application/zip")
    v.upload_meta = {
        "mode": "multipart",
        "size": size,
        "part_bytes": part,
        "parts": count,
        "started_at": started,
    }
    return {
        "mode": "multipart",
        "expires_in": ttl,
        "part_bytes": part,
        "parts": [
            {
                "part_number": n,
                "url": store.presign_upload_part(key, v.upload_id, n, ttl, _part_length(v.upload_meta, n)),
            }
            for n in range(1, count + 1)
        ],
    }


def _awaiting(s: Session, p: Principal, version_id: uuid.UUID) -> m.DatasetVersion:
    v = get_version(s, p, version_id, Role.REVIEWER)
    if v.status != VersionStatus.AWAITING_UPLOAD:
        raise HTTPException(status_code=409, detail=f"version is {v.status}")
    if not v.source_object_key:
        raise HTTPException(status_code=409, detail="version has no upload")
    return v


class PartsIn(BaseModel):
    part_numbers: list[int] = Field(min_length=1, max_length=1000)


@router.post("/versions/{version_id}/upload/parts")
def refresh_part_urls(
    version_id: uuid.UUID, body: PartsIn, p: Principal = Depends(not_demo), s: Session = Depends(get_db)
) -> dict:
    """Fresh presigned URLs for parts of a multipart upload (after expiry, or to resume)."""
    v = _awaiting(s, p, version_id)
    meta = v.upload_meta or {}
    if not v.upload_id or meta.get("mode") != "multipart":
        raise HTTPException(status_code=409, detail="this upload is not a multipart upload")
    count = int(meta["parts"])
    if any(n < 1 or n > count for n in body.part_numbers):
        raise HTTPException(status_code=422, detail=f"part numbers must be between 1 and {count}")
    store, ttl = get_store(), get_settings().upload_url_ttl_seconds
    assert v.source_object_key is not None
    return {
        "expires_in": ttl,
        "parts": [
            {
                "part_number": n,
                "url": store.presign_upload_part(
                    v.source_object_key, v.upload_id, n, ttl, _part_length(meta, n)
                ),
            }
            for n in sorted(set(body.part_numbers))
        ],
    }


@router.get("/versions/{version_id}/upload")
def upload_status(
    version_id: uuid.UUID, p: Principal = Depends(not_demo), s: Session = Depends(get_db)
) -> dict:
    """Parts already received, so an interrupted upload can resume where it stopped."""
    v = _awaiting(s, p, version_id)
    meta = v.upload_meta or {}
    if not v.upload_id or meta.get("mode") != "multipart":
        return {"mode": meta.get("mode", "single"), "uploaded_parts": []}
    assert v.source_object_key is not None
    parts = get_store().list_uploaded_parts(v.source_object_key, v.upload_id)
    return {
        "mode": "multipart",
        "part_bytes": meta["part_bytes"],
        "parts": meta["parts"],
        "uploaded_parts": [
            x["PartNumber"] for x in parts if x["Size"] == _part_length(meta, x["PartNumber"])
        ],
    }


@router.post("/versions/{version_id}/upload/abort")
def abort_upload(
    version_id: uuid.UUID, p: Principal = Depends(not_demo), s: Session = Depends(get_db)
) -> dict:
    v = _awaiting(s, p, version_id)
    store = get_store()
    assert v.source_object_key is not None
    if v.upload_id:
        store.abort_multipart_upload(v.source_object_key, v.upload_id)
        v.upload_id = None
    store.delete(v.source_object_key)
    v.status = VersionStatus.FAILED
    v.error = "Upload cancelled."
    s.commit()
    return {"version": version_view(v)}


def _reject(s: Session, v: m.DatasetVersion, status: int, detail: str) -> HTTPException:
    """Fail the version (the upload cannot be retried against the same URLs) and discard bytes."""
    store = get_store()
    assert v.source_object_key is not None
    if v.upload_id:
        store.abort_multipart_upload(v.source_object_key, v.upload_id)
        v.upload_id = None
    store.delete(v.source_object_key)
    v.status = VersionStatus.FAILED
    v.error = detail
    s.commit()
    return HTTPException(status_code=status, detail=detail)


@router.post("/versions/{version_id}/finalize-upload")
def finalize_upload(version_id: uuid.UUID, p: Principal = Depends(not_demo), s: Session = Depends(get_db)):
    v = _awaiting(s, p, version_id)
    store = get_store()
    key = v.source_object_key
    assert key is not None
    limit = get_settings().max_upload_bytes
    meta = v.upload_meta or {}
    if v.upload_id:
        expected = int(meta.get("parts", 0))
        parts = [
            x
            for x in store.list_uploaded_parts(key, v.upload_id)
            if 1 <= x["PartNumber"] <= expected and x["Size"] == _part_length(meta, x["PartNumber"])
        ]
        missing = sorted(set(range(1, expected + 1)) - {x["PartNumber"] for x in parts})
        if missing:
            return JSONResponse(
                {
                    "detail": f"{len(missing)} of {expected} parts have not been uploaded yet",
                    "missing_parts": missing[:1000],
                },
                status_code=409,
            )
        size = sum(x["Size"] for x in parts)
        if size > limit:
            raise _reject(s, v, 413, "archive too large")
        store.complete_multipart_upload(key, v.upload_id, parts)
        v.upload_id = None
    else:
        try:
            # One ranged read gives both the size and the ZIP signature.
            head, size = store.read_range(key, 0, 4)
        except ObjectNotFound as exc:
            raise HTTPException(status_code=400, detail="archive has not been uploaded yet") from exc
        if size > limit:
            raise _reject(s, v, 413, "archive too large")
        if head[:2] != b"PK":
            raise _reject(s, v, 415, "file is not a ZIP archive")
    v.status = VersionStatus.UPLOADED
    v.source_bytes = size
    auto = (v.layout or {}).get("requested_audit", "fast")
    job = enqueue_ingest(s, v, auto_audit=auto)
    s.commit()
    return {"version": version_view(v), "job_id": str(job.id)}


@router.post("/versions/{version_id}/retry-ingest")
def retry_ingest(
    version_id: uuid.UUID, p: Principal = Depends(not_demo), s: Session = Depends(get_db)
) -> dict:
    """Ingest a failed version again from the archive it already stored, for example after a
    format became supported. Files recorded by an earlier attempt are kept; the job resumes."""
    v = get_version(s, p, version_id, Role.REVIEWER)
    if v.status != VersionStatus.FAILED:
        raise HTTPException(
            status_code=409, detail="only a version whose ingestion failed can be ingested again"
        )
    if not v.source_object_key or v.upload_id or not get_store().exists(v.source_object_key):
        raise HTTPException(
            status_code=409, detail="the uploaded archive is no longer stored; upload it again"
        )
    last = s.scalar(
        select(m.Job)
        .where(m.Job.type == JobType.INGEST_VERSION, m.Job.payload["version_id"].as_string() == str(v.id))
        .order_by(m.Job.created_at.desc())
        .limit(1)
    )
    auto = (last.payload or {}).get("auto_audit", "fast") if last else "fast"
    job = enqueue_ingest(s, v, auto_audit=auto, attempt=f"{datetime.now(UTC).timestamp():.0f}")
    v.status = VersionStatus.UPLOADED
    v.error = None
    s.commit()
    return {"version": version_view(v), "job_id": str(job.id)}


@router.get("/versions/{version_id}")
def version_detail(
    version_id: uuid.UUID, p: Principal = Depends(current_principal), s: Session = Depends(get_db)
) -> dict:
    v = get_version(s, p, version_id)
    d = s.get(m.Dataset, v.dataset_id)
    assert d is not None
    proj = s.get(m.Project, d.project_id)
    audits = s.scalars(
        select(m.AuditRun).where(m.AuditRun.dataset_version_id == v.id).order_by(m.AuditRun.created_at.desc())
    ).all()
    siblings = s.scalars(
        select(m.DatasetVersion)
        .where(m.DatasetVersion.dataset_id == d.id)
        .order_by(m.DatasetVersion.version_number)
    ).all()
    ingest = s.scalar(select(m.Job).where(m.Job.idempotency_key == f"ingest:{v.id}"))
    if v.status in (VersionStatus.UPLOADED, VersionStatus.INGESTING) or any(
        a.status in (RunStatus.QUEUED, RunStatus.RUNNING) for a in audits
    ):
        execution.maybe_redispatch()
    rejected = s.scalars(
        select(m.DatasetFile)
        .where(m.DatasetFile.dataset_version_id == v.id, m.DatasetFile.status == "rejected")
        .limit(50)
    ).all()
    return {
        **version_view(v),
        "dataset": {"id": str(d.id), "name": d.name, "provenance": d.provenance},
        "project": {"id": str(proj.id), "name": proj.name, "org_id": str(proj.org_id)} if proj else None,
        "versions": [
            {"id": str(x.id), "version_number": x.version_number, "status": str(x.status), "origin": x.origin}
            for x in siblings
        ],
        "audits": [
            {
                "id": str(a.id),
                "status": str(a.status),
                "profile": str(a.profile),
                "progress": a.progress,
                "current_stage": a.current_stage,
                "created_at": iso(a.created_at),
                "finished_at": iso(a.finished_at),
                "summary": a.summary,
            }
            for a in audits
        ],
        "ingest_job": execution.status_for(ingest),
        "rejected_files": [{"path": f.relative_path, "reason": f.reason} for f in rejected],
    }


@router.delete("/versions/{version_id}")
def delete_version(
    version_id: uuid.UUID, p: Principal = Depends(not_demo), s: Session = Depends(get_db)
) -> dict:
    v = get_version(s, p, version_id, Role.ADMIN)
    d = s.get(m.Dataset, v.dataset_id)
    assert d is not None
    if v.upload_id and v.source_object_key:
        get_store().abort_multipart_upload(v.source_object_key, v.upload_id)
    prefixes = purge.version_prefixes(s, [v])
    job = purge.schedule(s, prefixes, org_id=v.org_id, reason="dataset_version.deleted")
    ledger.security_event(
        s,
        "dataset_version.deleted",
        org_id=v.org_id,
        actor_id=p.user_id,
        target_type="dataset_version",
        target_id=v.id,
    )
    s.delete(v)
    s.commit()
    return purge.run_inline([job.id])


@router.delete("/datasets/{dataset_id}")
def delete_dataset(
    dataset_id: uuid.UUID, p: Principal = Depends(not_demo), s: Session = Depends(get_db)
) -> dict:
    d = get_dataset(s, p, dataset_id, Role.ADMIN)
    store = get_store()
    versions = list(s.scalars(select(m.DatasetVersion).where(m.DatasetVersion.dataset_id == d.id)))
    for v in versions:
        if v.source_object_key and v.upload_id:
            store.abort_multipart_upload(v.source_object_key, v.upload_id)
    # The dataset directory covers every version's own objects (including leftovers of versions
    # deleted earlier); audit, export and report files live under workspace-level keys.
    prefixes = [f"orgs/{d.org_id}/datasets/{d.id}"] + [
        p_ for p_ in purge.version_prefixes(s, versions) if "/datasets/" not in p_
    ]
    job = purge.schedule(s, prefixes, org_id=d.org_id, reason="dataset.deleted")
    ledger.security_event(
        s, "dataset.deleted", org_id=d.org_id, actor_id=p.user_id, target_type="dataset", target_id=d.id
    )
    s.delete(d)
    s.commit()
    return purge.run_inline([job.id])


# ---- samples ----------------------------------------------------------------------


@router.get("/versions/{version_id}/samples")
def list_samples(
    version_id: uuid.UUID,
    q: str | None = Query(None, max_length=200),
    class_name: str | None = None,
    split: str | None = None,
    finding: str | None = Query(
        None, description="quality finding type, or: label, duplicate, leakage, privacy"
    ),
    verdict: str | None = None,
    sort: str = Query("path", pattern="^(path|suspicion|priority)$"),
    offset: int = Query(0, ge=0),
    limit: int = Query(60, ge=1, le=200),
    p: Principal = Depends(current_principal),
    s: Session = Depends(get_db),
) -> dict:
    v = get_version(s, p, version_id)
    audit = None
    try:
        audit = resolve_audit(s, v)
    except HTTPException:
        audit = None
    stmt = select(m.Sample).where(m.Sample.dataset_version_id == v.id)
    if q:
        stmt = stmt.where(or_(m.Sample.relative_path.ilike(f"%{q}%"), m.Sample.sha256 == q.lower()))
    if class_name:
        stmt = stmt.where(m.Sample.label == class_name)
    if split:
        stmt = stmt.where(m.Sample.split == split)
    if audit is not None:
        if finding == "label":
            stmt = stmt.where(
                exists().where(
                    and_(
                        m.LabelFinding.sample_id == m.Sample.id,
                        m.LabelFinding.audit_run_id == audit.id,
                        m.LabelFinding.recommended_action != "NO_ACTION",
                    )
                )
            )
        elif finding == "duplicate":
            stmt = stmt.where(
                exists().where(
                    and_(
                        m.DuplicateFamilyMember.sample_id == m.Sample.id,
                        m.DuplicateFamilyMember.family_id == m.DuplicateFamily.id,
                        m.DuplicateFamily.audit_run_id == audit.id,
                    )
                )
            )
        elif finding == "leakage":
            ids = [
                uuid.UUID(x)
                for lk in s.scalars(select(m.LeakageFinding).where(m.LeakageFinding.audit_run_id == audit.id))
                for x in lk.sample_ids
            ]
            stmt = stmt.where(m.Sample.id.in_(ids or [uuid.uuid4()]))
        elif finding == "privacy":
            stmt = stmt.where(
                exists().where(
                    and_(m.PrivacyFinding.sample_id == m.Sample.id, m.PrivacyFinding.audit_run_id == audit.id)
                )
            )
        elif finding:
            stmt = stmt.where(
                exists().where(
                    and_(
                        m.QualityFinding.sample_id == m.Sample.id,
                        m.QualityFinding.audit_run_id == audit.id,
                        m.QualityFinding.finding_type == finding,
                    )
                )
            )
        if verdict:
            stmt = stmt.where(
                exists().where(
                    and_(
                        m.CourtCase.sample_id == m.Sample.id,
                        m.CourtCase.audit_run_id == audit.id,
                        m.CourtCase.verdict == verdict,
                    )
                )
            )
    total = s.scalar(select(func.count()).select_from(stmt.subquery()))
    if sort == "suspicion" and audit is not None:
        stmt = stmt.outerjoin(
            m.LabelFinding,
            and_(m.LabelFinding.sample_id == m.Sample.id, m.LabelFinding.audit_run_id == audit.id),
        ).order_by(m.LabelFinding.suspicion_score.desc().nulls_last(), m.Sample.relative_path)
    elif sort == "priority" and audit is not None:
        stmt = stmt.outerjoin(
            m.CourtCase, and_(m.CourtCase.sample_id == m.Sample.id, m.CourtCase.audit_run_id == audit.id)
        ).order_by(m.CourtCase.priority_score.desc().nulls_last(), m.Sample.relative_path)
    else:
        stmt = stmt.order_by(m.Sample.relative_path)
    rows = s.scalars(stmt.offset(offset).limit(limit)).all()
    cases = {}
    if audit is not None and rows:
        cases = {
            c.sample_id: c
            for c in s.scalars(
                select(m.CourtCase).where(
                    m.CourtCase.audit_run_id == audit.id, m.CourtCase.sample_id.in_([r.id for r in rows])
                )
            )
        }
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [
            {
                **sample_view(r),
                "case": {
                    "id": str(cases[r.id].id),
                    "number": cases[r.id].case_number,
                    "verdict": str(cases[r.id].verdict),
                }
                if r.id in cases
                else None,
            }
            for r in rows
        ],
    }


@router.get("/samples/{sample_id}")
def sample_detail(
    sample_id: uuid.UUID, p: Principal = Depends(current_principal), s: Session = Depends(get_db)
) -> dict:
    x, v = get_sample(s, p, sample_id)
    out = sample_view(x, full=True)
    try:
        audit = resolve_audit(s, v)
    except HTTPException:
        return out
    aid = audit.id
    qm = s.scalar(
        select(m.SampleQualityMetrics).where(
            m.SampleQualityMetrics.audit_run_id == aid, m.SampleQualityMetrics.sample_id == x.id
        )
    )
    out["quality_metrics"] = qm.metrics if qm else None
    out["quality_findings"] = [
        {
            "type": f.finding_type,
            "severity": str(f.severity),
            "value": f.measured_value,
            "threshold": f.threshold,
            "comparator": f.comparator,
            "rule_version": f.rule_version,
            "deterministic": f.deterministic,
            "description": f.description,
        }
        for f in s.scalars(
            select(m.QualityFinding).where(
                m.QualityFinding.audit_run_id == aid, m.QualityFinding.sample_id == x.id
            )
        )
    ]
    pred = s.scalar(
        select(m.ModelPrediction).where(
            m.ModelPrediction.audit_run_id == aid, m.ModelPrediction.sample_id == x.id
        )
    )
    out["prediction"] = (
        {
            "top_probs": pred.top_probs,
            "prob_given": pred.prob_given,
            "margin": pred.margin,
            "correct": pred.correct,
            "out_of_sample": pred.is_out_of_sample,
        }
        if pred
        else None
    )
    lf = s.scalar(
        select(m.LabelFinding).where(m.LabelFinding.audit_run_id == aid, m.LabelFinding.sample_id == x.id)
    )
    out["label_finding"] = (
        {"suspicion": lf.suspicion_score, "action": str(lf.recommended_action), "evidence": lf.evidence}
        if lf
        else None
    )
    case = s.scalar(select(m.CourtCase).where(m.CourtCase.audit_run_id == aid, m.CourtCase.sample_id == x.id))
    out["case"] = (
        {"id": str(case.id), "number": case.case_number, "verdict": str(case.verdict)} if case else None
    )
    out["audit_id"] = str(aid)
    return out
