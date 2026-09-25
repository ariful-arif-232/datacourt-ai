"""Time-based retention for redundant archives (`retention-v1`).

Only derived or duplicate archives are purged once they are older than the workspace's
`retention_days`: the original upload ZIP of a version that has finished ingesting (its images are
already stored individually), export ZIP archives and rendered report files. Dataset versions,
samples, findings, human decisions and the evidence ledger are never deleted by retention — those are
removed only by an explicit user deletion. Workspaces without `retention_days` keep everything.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from datacourt import algorithms, ledger
from datacourt.db import models as m
from datacourt.enums import RunStatus, VersionStatus
from datacourt.storage import get_store

VERSION = algorithms.RETENTION
logger = logging.getLogger("datacourt.retention")


def sweep(s: Session, now: datetime | None = None) -> dict[str, int]:
    """Purge expired archives for every workspace with a retention policy. Caller commits."""
    now = now or datetime.now(UTC)
    store = get_store()
    totals = {"source_archives": 0, "export_archives": 0, "report_files": 0}
    for org in s.scalars(select(m.Organization).where(m.Organization.retention_days.is_not(None))):
        cutoff = now - timedelta(days=int(org.retention_days or 0))
        counts = {"source_archives": 0, "export_archives": 0, "report_files": 0}
        for v in s.scalars(
            select(m.DatasetVersion).where(
                m.DatasetVersion.org_id == org.id,
                m.DatasetVersion.status == VersionStatus.READY,
                m.DatasetVersion.source_object_key.is_not(None),
                m.DatasetVersion.created_at < cutoff,
            )
        ):
            if v.source_object_key:
                store.delete(v.source_object_key)
            v.source_object_key = None
            counts["source_archives"] += 1
        for ex in s.scalars(
            select(m.Export).where(
                m.Export.org_id == org.id,
                m.Export.status == RunStatus.COMPLETED,
                m.Export.object_key.is_not(None),
                m.Export.created_at < cutoff,
            )
        ):
            if ex.object_key:
                store.delete(ex.object_key)
            ex.object_key = None
            ex.summary = {**(ex.summary or {}), "archive_expired_at": now.isoformat()}
            counts["export_archives"] += 1
        for rep in s.scalars(
            select(m.AuditReport).where(
                m.AuditReport.org_id == org.id,
                m.AuditReport.status == RunStatus.COMPLETED,
                m.AuditReport.created_at < cutoff,
            )
        ):
            keys = [k for k in (rep.html_object_key, rep.json_object_key) if k]
            if not keys:
                continue
            for k in keys:
                store.delete(k)
            rep.html_object_key = rep.json_object_key = None
            counts["report_files"] += len(keys)
        if any(counts.values()):
            ledger.security_event(
                s,
                "retention.purged",
                org_id=org.id,
                meta={**counts, "retention_days": org.retention_days, "policy": VERSION},
            )
            for k, n in counts.items():
                totals[k] += n
    return totals
