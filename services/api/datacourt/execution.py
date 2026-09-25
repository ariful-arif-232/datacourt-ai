"""Waking workers for queued jobs.

The job queue in PostgreSQL is the source of truth; this module only makes sure a worker
exists to drain it. `EXECUTION_BACKEND` selects how:

- `embedded`: a worker thread inside the API process polls the queue (local development);
- `external`: a separately managed `datacourt-worker` process polls the queue;
- `github_actions`: after a transaction that enqueued jobs commits, the API dispatches the
  DataCourt worker workflow (`workflow_dispatch`); the run drains the queue and exits;
- `none`: nothing (tests, and inside workers themselves).

Dispatching is best-effort and never fails the user's request. The outcome is recorded on the
job (`dispatched_at`, `dispatch_error`; never the token). Queued jobs that no worker picked up are
dispatched again when clients poll their status (`maybe_redispatch`) and by the workflow's
schedule. Parallelism is bounded by slots: each slot is a GitHub Actions concurrency group, so at
most `WORKER_MAX_PARALLEL` runs execute at once and extra dispatches wait or coalesce.

A dedicated worker or GPU backend can replace GitHub Actions without code changes elsewhere: run
`datacourt-worker` against the same database and set `EXECUTION_BACKEND=external`.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from sqlalchemy import event, func, select, text, update
from sqlalchemy.orm import Session

from datacourt.config import Settings, get_settings
from datacourt.db.models import Job
from datacourt.enums import RunStatus
from datacourt.logging_setup import log

logger = logging.getLogger("datacourt.execution")

_PENDING = "datacourt.enqueued_jobs"
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_WORKFLOW = re.compile(r"^([A-Za-z0-9_.-]+\.ya?ml|\d+)$")
_REF = re.compile(r"^[A-Za-z0-9_./-]{1,200}$")
_SLOT = re.compile(r"^gha-s(\d+)-")

# Status polls check for lost dispatches at most this often per process.
POLL_CHECK_SECONDS = 15.0

_installed = False
_install_lock = threading.Lock()
_local_slot: int | None = None
_last_poll_check = 0.0


# ---------------------------------------------------------------------------
# Transaction hooks: dispatch only after the jobs are durably committed
# ---------------------------------------------------------------------------


def install() -> None:
    """Register the session hooks (idempotent). Called by the API at startup."""
    global _installed
    with _install_lock:
        if _installed:
            return
        event.listen(Session, "after_commit", _after_commit)
        event.listen(Session, "after_soft_rollback", _after_soft_rollback)
        _installed = True


def note_enqueued(session: Session, job_id: uuid.UUID) -> None:
    if _installed:
        session.info.setdefault(_PENDING, []).append(job_id)


def _after_commit(session: Session) -> None:
    ids = session.info.pop(_PENDING, None)
    if ids:
        dispatch(list(dict.fromkeys(ids)), reason="enqueued")


def _after_soft_rollback(session: Session, previous_transaction: object) -> None:
    if not session.in_transaction():  # the outermost transaction rolled back: nothing was queued
        session.info.pop(_PENDING, None)


def set_local_worker(slot: int | None) -> None:
    """Inside a worker: jobs it enqueues (e.g. the audit after an ingest) are its own to run."""
    global _local_slot
    _local_slot = slot


def stamp_local(job: Job) -> None:
    if _local_slot is not None:
        job.dispatched_at = datetime.now(UTC)
        job.dispatch_slot = _local_slot


# ---------------------------------------------------------------------------
# GitHub Actions workflow dispatch
# ---------------------------------------------------------------------------


@dataclass
class DispatchOutcome:
    ok: bool
    slot: int | None
    error: str | None = None


def github_ref(settings: Settings) -> str:
    return settings.github_ref or os.environ.get("VERCEL_GIT_COMMIT_REF") or "main"


def configuration_problems(settings: Settings) -> list[str]:
    problems = []
    if not settings.github_token:
        problems.append("DATACOURT_GITHUB_TOKEN is not set")
    if not settings.github_repository or not _REPO.match(settings.github_repository):
        problems.append("DATACOURT_GITHUB_REPO must look like owner/repository")
    if not _WORKFLOW.match(settings.github_workflow):
        problems.append("DATACOURT_GITHUB_WORKFLOW must be a workflow file name or id")
    if not _REF.match(github_ref(settings)) or ".." in github_ref(settings):
        problems.append("DATACOURT_GITHUB_REF is not a valid branch or tag name")
    return problems


class GitHubDispatcher:
    """Starts a worker run with the Actions API. The token stays server-side: it is read from the
    environment, sent only to the GitHub API, and never logged or stored."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def dispatch(self, *, slot: int, job_id: uuid.UUID | None, reason: str) -> str | None:
        """Returns None on success, else a safe error description."""
        import httpx

        problems = configuration_problems(self.settings)
        if problems:
            return "; ".join(problems)
        s = self.settings
        url = (
            f"{s.github_api_url.rstrip('/')}/repos/{s.github_repository}"
            f"/actions/workflows/{quote(s.github_workflow, safe='')}/dispatches"
        )
        body = {
            "ref": github_ref(s),
            "inputs": {
                "task": "drain",
                "slot": str(slot),
                "job_id": str(job_id) if job_id else "",
                "reason": reason[:40],
            },
        }
        headers = {
            "Authorization": f"Bearer {s.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "datacourt-ai",
        }
        try:
            r = httpx.post(url, json=body, headers=headers, timeout=15.0)
        except httpx.HTTPError as exc:
            return f"GitHub API unreachable ({type(exc).__name__})"
        if r.status_code in (200, 201, 204):
            return None
        try:
            message = str(r.json().get("message", ""))[:160]
        except ValueError:
            message = ""
        hint = {
            401: "the token is invalid or expired",
            403: "the token lacks Actions write access to the repository",
            404: "repository or workflow not found (check DATACOURT_GITHUB_REPO and that the workflow exists)",
            422: "the workflow cannot run on this ref (check DATACOURT_GITHUB_REF)",
        }.get(r.status_code, "")
        return (
            f"GitHub API returned {r.status_code}"
            + (f": {hint}" if hint else "")
            + (f" ({message})" if message else "")
        )


def choose_slot(s: Session, settings: Settings) -> int:
    """The least busy worker slot: running jobs, plus recent dispatches not yet picked up."""
    load = {n: 0 for n in range(1, settings.worker_max_parallel + 1)}
    for locked_by in s.scalars(
        select(Job.locked_by).where(Job.status == RunStatus.RUNNING, Job.locked_by.like("gha-s%"))
    ):
        m = _SLOT.match(locked_by or "")
        if m and int(m.group(1)) in load:
            load[int(m.group(1))] += 1
    recent = s.execute(
        select(Job.dispatch_slot, func.count())
        .where(
            Job.status == RunStatus.QUEUED,
            Job.dispatch_slot.is_not(None),
            Job.dispatched_at > datetime.now(UTC) - timedelta(seconds=settings.dispatch_retry_after_seconds),
        )
        .group_by(Job.dispatch_slot)
    ).all()
    for slot, n in recent:
        if slot in load:
            load[slot] += int(n)
    return min(load, key=lambda k: (load[k], k))


def dispatch(job_ids: list[uuid.UUID], reason: str, *, slot: int | None = None) -> DispatchOutcome:
    """Wake a worker for these (committed, queued) jobs, if this deployment dispatches workers.
    Never raises."""
    if get_settings().execution_backend != "github_actions" or not job_ids:
        return DispatchOutcome(False, None, None)
    return dispatch_github(job_ids, reason, slot=slot)


def dispatch_github(job_ids: list[uuid.UUID], reason: str, *, slot: int | None = None) -> DispatchOutcome:
    """Dispatch the worker workflow for `job_ids` and record the outcome on the jobs.
    Used directly by a worker run that hands remaining work to its successor."""
    from datacourt import jobs
    from datacourt.db.session import new_session

    settings = get_settings()
    error: str | None
    try:
        if slot is None:
            with new_session() as s:
                slot = choose_slot(s, settings)
        error = GitHubDispatcher(settings).dispatch(slot=slot, job_id=job_ids[0], reason=reason)
    except Exception as exc:  # noqa: BLE001 - waking a worker must never fail the request
        error = f"dispatch failed ({type(exc).__name__})"
    try:
        with new_session() as s:
            s.execute(
                update(Job)
                .where(Job.id.in_(job_ids))
                .values(
                    dispatched_at=func.now(),
                    dispatch_attempts=Job.dispatch_attempts + 1,
                    dispatch_slot=slot,
                    dispatch_error=error[:300] if error else None,
                )
            )
            for job_id in job_ids:
                jobs.add_event(
                    s,
                    job_id,
                    "warning" if error else "info",
                    "dispatch_failed" if error else "dispatched",
                    {"slot": slot, "reason": reason, **({"error": error} if error else {})},
                )
            s.commit()
    except Exception:  # noqa: BLE001
        logger.exception("could not record dispatch")
    log(
        logger,
        logging.WARNING if error else logging.INFO,
        "worker dispatch",
        ok=error is None,
        slot=slot,
        reason=reason,
        jobs=len(job_ids),
        error=error,
    )
    return DispatchOutcome(error is None, slot, error)


_REDISPATCH_SQL = text(
    """
    UPDATE jobs SET dispatched_at = now()
    WHERE id = (
        SELECT id FROM jobs
        WHERE status = 'queued' AND run_after <= now() AND cancel_requested = false
          AND (dispatched_at IS NULL OR dispatched_at < now() - make_interval(
                secs => :retry * LEAST(power(2, GREATEST(dispatch_attempts - 1, 0)), 8)))
        ORDER BY priority ASC, created_at ASC
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id
    """
)


def maybe_redispatch(force: bool = False) -> DispatchOutcome | None:
    """Called from job-status endpoints: recover jobs whose worker died (reaper) and re-dispatch
    queued jobs no worker picked up (lost dispatch, runner never started, retry now due).
    Throttled per process; never raises."""
    global _last_poll_check
    settings = get_settings()
    if settings.execution_backend != "github_actions":
        return None
    now = time.monotonic()
    if not force and now - _last_poll_check < POLL_CHECK_SECONDS:
        return None
    _last_poll_check = now
    from datacourt import jobs
    from datacourt.db.session import new_session

    try:
        with new_session() as s:
            jobs.reap_stale(s, settings.job_stale_after_seconds)
            row = s.execute(_REDISPATCH_SQL, {"retry": settings.dispatch_retry_after_seconds}).first()
            s.commit()
            if row is None:
                return None
            live_slots = {
                int(m.group(1))
                for locked_by in s.scalars(
                    select(Job.locked_by).where(
                        Job.status == RunStatus.RUNNING,
                        Job.locked_by.like("gha-s%"),
                        Job.heartbeat_at > datetime.now(UTC) - timedelta(seconds=60),
                    )
                )
                if (m := _SLOT.match(locked_by or ""))
            }
        if len(live_slots) >= settings.worker_max_parallel:
            return None  # every slot has a live worker; one of them will take the job
        return dispatch([row[0]], reason="retry")
    except Exception:  # noqa: BLE001
        logger.exception("re-dispatch check failed")
        return None


def status_for(job: Job | None) -> dict | None:
    """Execution details for the UI: where the job is running, or why it is waiting."""
    if job is None:
        return None
    settings = get_settings()
    state = str(job.status)
    waiting = None
    if state == "queued":
        if job.dispatch_error:
            waiting = "dispatch_failed"
        elif job.run_after and job.run_after > datetime.now(UTC):
            waiting = "retry_scheduled"
        elif settings.execution_backend == "github_actions":
            waiting = "starting_worker" if job.dispatched_at else "waiting_for_worker"
        else:
            waiting = "waiting_for_worker"
    return {
        "job_id": str(job.id),
        "type": str(job.type),
        "status": state,
        "progress": job.progress,
        "stage": job.stage,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "backend": settings.execution_backend,
        "waiting": waiting,
        "dispatch_error": job.dispatch_error,
        "dispatched_at": job.dispatched_at.isoformat() if job.dispatched_at else None,
        "runner_url": job.runner_url,
        "retry_at": job.run_after.isoformat() if state == "queued" and job.run_after else None,
        "error_class": job.error_class,
        "error": job.error_message if state in ("failed", "cancelled") or job.error_class else None,
        "timeout_seconds": job.timeout_seconds,
        "cancel_requested": job.cancel_requested,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }
