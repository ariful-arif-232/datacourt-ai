"""PostgreSQL-backed job queue.

- Claiming uses `FOR UPDATE SKIP LOCKED`, so concurrent workers never take the same job.
- Every job has a soft time limit (`timeout_seconds`); a worker with a bounded lifetime
  (a GitHub Actions run) only claims jobs that fit in the time it has left.
- Running jobs heartbeat; a reaper re-queues jobs whose worker died (stale heartbeat).
- Handlers must be idempotent: they may be re-run after a crash. Pipeline stages and
  ingestion store their own completion markers so re-runs resume instead of duplicating.
- Enqueuing records the job for `datacourt.execution`, which wakes a worker after commit.
"""

from __future__ import annotations

import logging
import traceback
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from datacourt.db.models import Job, JobEvent
from datacourt.enums import JobType, RunStatus
from datacourt.logging_setup import log

logger = logging.getLogger("datacourt.jobs")


class PermanentJobError(Exception):
    """Error that retrying will not fix (bad input, unsupported data)."""

    error_class = "permanent"


class JobCancelled(Exception):
    error_class = "cancelled"


class JobTimeout(Exception):
    """The job ran past its time limit; retrying on the same input would time out again."""

    error_class = "timeout"


# Longest time limit any job gets; worker runs must last longer than this (see the workflow).
MAX_TIMEOUT_SECONDS = 3 * 3600

_DEFAULT_TIMEOUTS = {
    JobType.INGEST_VERSION: 30 * 60,
    JobType.RUN_AUDIT: 60 * 60,
    JobType.WHAT_IF: 45 * 60,
    JobType.EXPORT: 45 * 60,
    JobType.REPORT: 15 * 60,
    JobType.VERSION_DIFF: 20 * 60,
    JobType.CONTAMINATION: 30 * 60,
    JobType.PURGE_PREFIX: 30 * 60,
}


def classify_error(exc: BaseException) -> str:
    if isinstance(exc, PermanentJobError):
        return "permanent"
    if isinstance(exc, JobCancelled):
        return "cancelled"
    if isinstance(exc, JobTimeout):
        return "timeout"
    if isinstance(exc, MemoryError):
        return "resource_exhausted"
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return "transient"
    return "internal"


def enqueue(
    session: Session,
    job_type: JobType,
    payload: dict[str, Any],
    *,
    org_id: uuid.UUID | None = None,
    priority: int = 100,
    idempotency_key: str | None = None,
    max_attempts: int = 3,
    timeout_seconds: int | None = None,
    dispatch: bool = True,
) -> Job:
    """Queue a job in the caller's transaction. With `dispatch`, a worker is woken once the
    transaction commits (see `datacourt.execution`)."""
    from datacourt import execution

    if idempotency_key:
        existing = session.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
        if existing is not None:
            return existing
    timeout = min(MAX_TIMEOUT_SECONDS, max(60, int(timeout_seconds or _DEFAULT_TIMEOUTS.get(job_type, 1800))))
    job = Job(
        id=uuid.uuid4(),
        type=job_type,
        payload=payload,
        org_id=org_id,
        status=RunStatus.QUEUED,
        priority=priority,
        idempotency_key=idempotency_key,
        max_attempts=max_attempts,
        timeout_seconds=timeout,
    )
    execution.stamp_local(job)
    try:
        # A savepoint: losing an idempotency race must not roll back the caller's other changes.
        with session.begin_nested():
            session.add(job)
            session.flush()
    except IntegrityError:
        existing = session.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
        if existing is None:
            raise
        return existing
    add_event(session, job.id, "info", "enqueued", {"type": str(job_type)})
    if dispatch:
        execution.note_enqueued(session, job.id)
    return job


def add_event(session: Session, job_id: uuid.UUID, level: str, event: str, data: dict | None = None) -> None:
    session.add(JobEvent(job_id=job_id, level=level, event=event, data=data or {}))


_CLAIM_SQL = text(
    """
    UPDATE jobs SET status = 'running', locked_by = :worker, locked_at = now(), heartbeat_at = now(),
           started_at = COALESCE(started_at, now()), attempts = attempts + 1, runner_url = :runner_url
    WHERE id = (
        SELECT id FROM jobs
        WHERE status = 'queued' AND run_after <= now() AND cancel_requested = false
          AND timeout_seconds <= :budget
        ORDER BY (id = :prefer) DESC, priority ASC, created_at ASC
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id
    """
)


def claim(
    session: Session,
    worker_id: str,
    *,
    budget_seconds: float | None = None,
    prefer: uuid.UUID | None = None,
    runner_url: str | None = None,
) -> Job | None:
    """Claim the next due job whose time limit fits in `budget_seconds` (None: no limit),
    preferring `prefer` (the job a worker was started for)."""
    params = {
        "worker": worker_id,
        "budget": int(budget_seconds) if budget_seconds is not None else 2**31 - 1,
        "prefer": prefer or uuid.UUID(int=0),
        "runner_url": runner_url,
    }
    row = session.execute(_CLAIM_SQL, params).first()
    session.commit()
    if row is None:
        return None
    job = session.get(Job, row[0], populate_existing=True)
    if job is not None:
        add_event(session, job.id, "info", "claimed", {"worker": worker_id, "attempt": job.attempts})
        session.commit()
    return job


def seconds_until_due(session: Session, budget_seconds: float | None = None) -> float | None:
    """Seconds until the earliest queued job (that fits the budget) is due; None if there is none."""
    q = select(func.min(Job.run_after)).where(Job.status == RunStatus.QUEUED, Job.cancel_requested.is_(False))
    if budget_seconds is not None:
        q = q.where(Job.timeout_seconds <= int(budget_seconds))
    first = session.scalar(q)
    session.rollback()
    if first is None:
        return None
    return max(0.0, (first - datetime.now(UTC)).total_seconds())


def queued_count(session: Session) -> int:
    n = session.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.status == RunStatus.QUEUED, Job.cancel_requested.is_(False))
    )
    session.rollback()
    return int(n or 0)


def heartbeat(
    session: Session, job_id: uuid.UUID, *, progress: float | None = None, stage: str | None = None
) -> bool:
    """Updates heartbeat (one round trip plus commit); returns False if cancellation was requested."""
    values: dict[str, Any] = {"heartbeat_at": datetime.now(UTC)}
    if progress is not None:
        values["progress"] = max(0.0, min(1.0, progress))
    if stage is not None:
        values["stage"] = stage[:60]
    cancel = session.execute(
        update(Job).where(Job.id == job_id).values(**values).returning(Job.cancel_requested)
    ).scalar()
    session.commit()
    return not bool(cancel)


def complete(session: Session, job_id: uuid.UUID, result: dict | None = None) -> None:
    session.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(
            status=RunStatus.COMPLETED,
            progress=1.0,
            finished_at=datetime.now(UTC),
            result=result or {},
            locked_by=None,
            error_class=None,
            error_message=None,
        )
    )
    add_event(session, job_id, "info", "completed")
    session.commit()


def fail(session: Session, job_id: uuid.UUID, exc: BaseException) -> str:
    """Records failure; re-queues with backoff if retryable. Returns resulting status."""
    session.rollback()
    job = session.get(Job, job_id)
    if job is None:
        return "missing"
    err_class = classify_error(exc)
    # Safe message: exception type + message, truncated; tracebacks go to logs only.
    message = f"{type(exc).__name__}: {str(exc)[:500]}"
    if err_class == "timeout":
        message = f"The job exceeded its time limit of {job.timeout_seconds // 60} minutes."
    retryable = (
        err_class in {"transient", "internal", "resource_exhausted"} and job.attempts < job.max_attempts
    )
    if err_class == "cancelled":
        job.status = RunStatus.CANCELLED
        job.finished_at = datetime.now(UTC)
    elif retryable:
        job.status = RunStatus.QUEUED
        job.run_after = datetime.now(UTC) + timedelta(seconds=min(300, 10 * 2 ** (job.attempts - 1)))
    else:
        job.status = RunStatus.FAILED
        job.finished_at = datetime.now(UTC)
    job.error_class = err_class
    job.error_message = message
    job.locked_by = None
    add_event(
        session, job.id, "error", "failed", {"error_class": err_class, "retry": retryable, "message": message}
    )
    session.commit()
    log(
        logger,
        logging.ERROR,
        "job failed",
        job_type=str(job.type),
        error_class=err_class,
        retry=retryable,
        exc_type=type(exc).__name__,
        # The message can name user files; logs of public runners must not contain it.
        trace=traceback.format_exception_only(type(exc), exc)[-1].strip()[:500],
    )
    return str(job.status)


def reap_stale(session: Session, stale_after_seconds: float) -> int:
    """Re-queue (or fail) running jobs whose worker stopped heartbeating."""
    cutoff = datetime.now(UTC) - timedelta(seconds=stale_after_seconds)
    stale = session.scalars(
        select(Job)
        .where(Job.status == RunStatus.RUNNING, Job.heartbeat_at < cutoff)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    ).all()
    for job in stale:
        if job.attempts < job.max_attempts:
            job.status = RunStatus.QUEUED
            job.run_after = datetime.now(UTC)
        else:
            job.status = RunStatus.FAILED
            job.finished_at = datetime.now(UTC)
        job.error_class = "worker_lost"
        job.error_message = "Worker stopped heartbeating; job recovered by reaper."
        job.locked_by = None
        add_event(session, job.id, "warning", "reaped", {"requeued": job.status == RunStatus.QUEUED})
    session.commit()
    return len(stale)


def request_cancel(session: Session, job_id: uuid.UUID) -> None:
    job = session.get(Job, job_id)
    if job is None:
        return
    if job.status == RunStatus.QUEUED:
        job.status = RunStatus.CANCELLED
        job.finished_at = datetime.now(UTC)
    job.cancel_requested = True
    add_event(session, job.id, "info", "cancel_requested")
