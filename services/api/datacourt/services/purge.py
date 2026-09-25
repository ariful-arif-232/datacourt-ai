"""Permanent deletion of stored objects when a user deletes data.

Deleting a dataset, version or workspace removes its database rows and schedules a
PURGE_PREFIX job in the same transaction, so the objects are always removed even if the
request dies half-way. After the commit the API purges inline for a bounded time; if that
finishes, the job is marked done without waking a worker, otherwise a worker finishes it.
Purges remove every stored version of every object (see `ObjectStore.purge_prefix`).
"""

from __future__ import annotations

import re
import time
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from datacourt import jobs
from datacourt.config import get_settings
from datacourt.db import models as m
from datacourt.db.session import new_session
from datacourt.enums import JobType, RunStatus
from datacourt.storage import (
    StorageUnavailable,
    audit_prefix,
    export_prefix,
    get_store,
    report_prefix,
    version_prefix,
)

_PREFIX = re.compile(
    r"^orgs/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(/[A-Za-z0-9_./-]+)?$"
)


def _valid(prefix: str) -> str:
    """Only tenant namespaces can be purged: never the bucket root or another area."""
    p = prefix.rstrip("/")
    if not _PREFIX.match(p) or ".." in p.split("/"):
        raise ValueError(f"refusing to purge {prefix!r}")
    return p


def version_prefixes(s: Session, versions: list[m.DatasetVersion]) -> list[str]:
    """Everything stored for these versions: images, thumbnails and archives, plus the artifacts
    of their audits and the files of their exports and reports (kept under workspace-level keys)."""
    if not versions:
        return []
    ids = [v.id for v in versions]
    org_id = versions[0].org_id
    audits = list(s.scalars(select(m.AuditRun.id).where(m.AuditRun.dataset_version_id.in_(ids))))
    exports = list(s.scalars(select(m.Export.id).where(m.Export.dataset_version_id.in_(ids))))
    reports: list[uuid.UUID] = (
        list(s.scalars(select(m.AuditReport.id).where(m.AuditReport.audit_run_id.in_(audits))))
        if audits
        else []
    )
    return (
        [version_prefix(org_id, v.dataset_id, v.id) for v in versions]
        + [audit_prefix(org_id, a) for a in audits]
        + [export_prefix(org_id, e) for e in exports]
        + [report_prefix(org_id, r) for r in reports]
    )


def schedule(s: Session, prefixes: list[str], *, org_id: uuid.UUID, reason: str) -> m.Job:
    """Enqueue the purge in the caller's transaction (no worker is woken yet).

    If a job of the workspace is running, it may still write objects under these prefixes, so
    the job waits long enough for that worker to notice the deletion and sweeps afterwards.
    """
    busy = s.scalar(
        select(m.Job.id).where(m.Job.org_id == org_id, m.Job.status == RunStatus.RUNNING).limit(1)
    )
    job = jobs.enqueue(
        s,
        JobType.PURGE_PREFIX,
        {"prefixes": [_valid(p) for p in prefixes], "reason": reason, "sweep": busy is not None},
        org_id=None,  # the workspace row may be deleted in this same transaction
        priority=150,
        max_attempts=10,
        dispatch=False,
    )
    if busy is not None:
        job.run_after = datetime.now(UTC) + timedelta(seconds=get_settings().job_stale_after_seconds + 300)
    return job


def run_inline(job_ids: list[uuid.UUID], budget_seconds: float = 20.0) -> dict:
    """Try to finish scheduled purges now (after the deletion committed)."""
    from datacourt import execution

    store = get_store()
    deadline = time.monotonic() + budget_seconds
    deleted, pending = 0, 0
    for job_id in job_ids:
        with new_session() as s:
            job = s.get(m.Job, job_id)
            if job is None or job.status != RunStatus.QUEUED:
                continue
            prefixes = list(job.payload.get("prefixes") or [])
            sweep = bool(job.payload.get("sweep"))
        complete, job_deleted = True, 0
        try:
            for prefix in prefixes:
                r = store.purge_prefix(prefix, deadline=deadline)
                job_deleted += r.deleted
                if not r.complete:
                    complete = False
                    break
        except StorageUnavailable:
            complete = False
        deleted += job_deleted
        if complete and not sweep:
            with new_session() as s:
                s.execute(
                    update(m.Job)
                    .where(m.Job.id == job_id, m.Job.status == RunStatus.QUEUED)
                    .values(
                        status=RunStatus.COMPLETED,
                        progress=1.0,
                        finished_at=datetime.now(UTC),
                        result={"deleted_objects": job_deleted, "inline": True},
                    )
                )
                jobs.add_event(s, job_id, "info", "completed", {"inline": True})
                s.commit()
        else:
            pending += 1
            if not sweep:  # a sweep waits for its run_after; the schedule or a poll wakes a worker
                execution.dispatch([job_id], reason="purge")
    return {"deleted_objects": deleted, "pending_purges": pending}


def run_purge_job(ctx) -> dict:
    store = get_store()
    prefixes = [_valid(p) for p in ctx.payload.get("prefixes") or []]
    total = 0
    for i, prefix in enumerate(prefixes):
        ctx.check_cancelled()
        total += store.purge_prefix(prefix).deleted
        ctx.report((i + 1) / max(1, len(prefixes)), "PURGING")
    return {"deleted_objects": total, "prefixes": len(prefixes)}
