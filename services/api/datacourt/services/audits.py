"""Audit creation / workload estimation."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from datacourt import jobs
from datacourt.db import models as m
from datacourt.enums import AuditProfile, JobType, RunStatus
from datacourt.pipeline.config import resolve_config


def project_overrides(s: Session, project_id: uuid.UUID) -> dict:
    cfg = s.scalar(
        select(m.AuditConfig)
        .where(m.AuditConfig.project_id == project_id)
        .order_by(m.AuditConfig.version.desc())
        .limit(1)
    )
    return dict(cfg.config) if cfg else {}


def create_audit(
    s: Session,
    version: m.DatasetVersion,
    profile: AuditProfile,
    *,
    created_by: uuid.UUID | None,
    overrides: dict | None = None,
) -> m.AuditRun:
    dataset = s.get(m.Dataset, version.dataset_id)
    assert dataset is not None
    merged = project_overrides(s, dataset.project_id)
    if overrides:
        merged = _deep_merge(merged, overrides)
    config, config_hash = resolve_config(merged)
    audit = m.AuditRun(
        org_id=version.org_id,
        dataset_version_id=version.id,
        profile=profile,
        status=RunStatus.QUEUED,
        config=config,
        config_hash=config_hash,
        created_by=created_by,
    )
    s.add(audit)
    s.flush()
    job = jobs.enqueue(
        s,
        JobType.RUN_AUDIT,
        {"audit_run_id": str(audit.id)},
        org_id=version.org_id,
        priority=80 if profile == AuditProfile.FAST else 90,
        idempotency_key=f"audit:{audit.id}",
        timeout_seconds=audit_timeout_seconds(version, profile),
    )
    audit.job_id = job.id
    return audit


def audit_timeout_seconds(version: m.DatasetVersion, profile: AuditProfile) -> int:
    """Time limit for an audit job: generous multiple of the workload estimate (estimates assume
    4 vCPUs; hosted runners may have 2), within 15 minutes to 3 hours."""
    from datacourt.config import get_settings

    n = int((version.stats or {}).get("sample_count") or 0)
    est = estimate_workload(n, profile, get_settings().embedding_backend)["estimated_seconds"]
    return int(min(3 * 3600, max(15 * 60, 10 * 60 + 6 * est)))


def _deep_merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def estimate_workload(sample_count: int, profile: AuditProfile, backend: str) -> dict:
    """Rough CPU estimates (4 vCPU) so users see expected compute before starting."""
    per_image = {"dc-descriptor-v1": 0.012, "dinov2-small": 0.09}.get(backend, 0.02)
    decode = 0.006 * sample_count
    embed = per_image * sample_count
    model = (0.4 if profile == AuditProfile.FAST else 1.2) * max(1.0, sample_count / 1000)
    extra = 0.0 if profile == AuditProfile.FAST else 0.004 * sample_count + 8
    tsne = min(sample_count, 2500 if profile == AuditProfile.FAST else 6000) * 0.006
    seconds = decode + embed + model + extra + tsne + 5
    return {
        "sample_count": sample_count,
        "profile": str(profile),
        "embedding_backend": backend,
        "estimated_seconds": round(seconds),
        "note": "Estimate for a 4-vCPU worker. Actual time depends on image sizes and hardware.",
        "recommend_sampling": sample_count > 60000,
    }
