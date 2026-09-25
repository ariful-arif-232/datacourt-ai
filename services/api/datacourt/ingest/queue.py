"""Enqueueing ingestion jobs (import-light: used by the API; the job itself runs in a worker)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from datacourt import jobs
from datacourt.db import models as m
from datacourt.enums import JobType


def ingest_timeout_seconds(source_bytes: int | None) -> int:
    """Download, validation, hashing, thumbnails and upload of every image: budget ~1.5 MB/s
    of archive on a small hosted runner, within 15 minutes to 2 hours."""
    return int(min(2 * 3600, max(15 * 60, 10 * 60 + (source_bytes or 0) / 1_500_000)))


def enqueue_ingest(
    s: Session, version: m.DatasetVersion, auto_audit: str | None = "fast", *, attempt: str | None = None
) -> m.Job:
    """`attempt` names a deliberate re-run (see `POST /versions/{id}/retry-ingest`), which gets its
    own idempotency key; the first ingestion of a version is enqueued once."""
    return jobs.enqueue(
        s,
        JobType.INGEST_VERSION,
        {"version_id": str(version.id), "auto_audit": auto_audit},
        org_id=version.org_id,
        priority=50,
        idempotency_key=f"ingest:{version.id}" + (f":retry:{attempt}" if attempt else ""),
        timeout_seconds=ingest_timeout_seconds(version.source_bytes),
    )
