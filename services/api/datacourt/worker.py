"""Background worker: claims jobs from Postgres and runs handlers.

Ways to run it:
- `datacourt-worker --drain` (GitHub Actions, cron, scripts): process queued jobs until the queue
  stays empty or the time budget runs out, then exit. Only jobs whose time limit fits in the
  remaining budget are claimed; if work is left when the budget ends, the run dispatches a
  successor before exiting.
- `datacourt-worker` (a dedicated server, container or GPU box): poll the queue forever.
- embedded in the API process (`RUN_EMBEDDED_WORKER=true`, local development).

Progress is kept in memory and flushed by the heartbeat thread, so a worker far from the
database (e.g. a GitHub-hosted runner and a database in another region) never waits on a
round trip to report progress.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import sys
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from datacourt import jobs
from datacourt.config import get_settings
from datacourt.db.models import Job
from datacourt.db.session import new_session
from datacourt.enums import JobType
from datacourt.logging_setup import configure_logging, job_id_var, log

logger = logging.getLogger("datacourt.worker")

PROGRESS_FLUSH_SECONDS = 2.0


@dataclass
class JobContext:
    job_id: uuid.UUID
    payload: dict
    attempt: int
    timeout_seconds: float | None = None
    _progress: float = 0.0
    _stage: str | None = None
    _dirty: bool = True
    _started: float = field(default_factory=time.monotonic)
    _wake: threading.Event = field(default_factory=threading.Event)
    cancelled: bool = False
    timed_out: bool = False

    def report(self, progress: float, stage: str | None = None, force: bool = False) -> None:
        """Record progress in memory; the heartbeat thread persists it (at once when `force`)."""
        self._progress = progress
        if stage is not None:
            self._stage = stage
        self._dirty = True
        if force:
            self._wake.set()

    def flush(self) -> None:
        self._dirty = False
        with new_session() as s:
            ok = jobs.heartbeat(s, self.job_id, progress=self._progress, stage=self._stage)
        if not ok:
            self.cancelled = True
        if self.timeout_seconds and time.monotonic() - self._started > self.timeout_seconds:
            self.timed_out = True

    def check_cancelled(self) -> None:
        """Checkpoint called by handlers between units of work."""
        if self.cancelled:
            raise jobs.JobCancelled("cancel requested")
        if self.timed_out or (
            self.timeout_seconds and time.monotonic() - self._started > self.timeout_seconds
        ):
            self.timed_out = True
            raise jobs.JobTimeout(f"time limit of {int(self.timeout_seconds or 0)}s exceeded")


Handler = Callable[[JobContext], dict | None]


def _handlers() -> dict[JobType, Handler]:
    # Imported lazily so the API can start without loading ML modules.
    from datacourt.exporting import run_export_job
    from datacourt.ingest.pipeline import run_ingest_job
    from datacourt.pipeline.runner import run_audit_job
    from datacourt.reports.builder import run_report_job
    from datacourt.services.contamination import run_contamination_job
    from datacourt.services.purge import run_purge_job
    from datacourt.services.versions import run_version_diff_job
    from datacourt.whatif.lab import run_what_if_job

    return {
        JobType.INGEST_VERSION: run_ingest_job,
        JobType.RUN_AUDIT: run_audit_job,
        JobType.WHAT_IF: run_what_if_job,
        JobType.EXPORT: run_export_job,
        JobType.REPORT: run_report_job,
        JobType.VERSION_DIFF: run_version_diff_job,
        JobType.CONTAMINATION: run_contamination_job,
        JobType.PURGE_PREFIX: run_purge_job,
    }


@dataclass
class RunnerIdentity:
    worker_id: str
    runner_url: str | None = None
    slot: int | None = None

    @classmethod
    def detect(cls) -> RunnerIdentity:
        """On GitHub Actions the id names the slot and run; `locked_by` then shows where a job runs."""
        run_id = os.environ.get("GITHUB_RUN_ID")
        if os.environ.get("GITHUB_ACTIONS") == "true" and run_id:
            slot_raw = os.environ.get("DATACOURT_WORKER_SLOT", "1")
            slot = int(slot_raw) if slot_raw.isdigit() else 1
            attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
            server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
            repo = os.environ.get("GITHUB_REPOSITORY", "")
            return cls(f"gha-s{slot}-r{run_id}-a{attempt}", f"{server}/{repo}/actions/runs/{run_id}", slot)
        return cls(f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}")


@dataclass
class DrainResult:
    processed: int = 0
    failed: int = 0
    reason: str = "empty"
    remaining: int = 0
    jobs: list[dict] = field(default_factory=list)


class Worker:
    def __init__(self, worker_id: str | None = None, identity: RunnerIdentity | None = None):
        self.settings = get_settings()
        self.identity = identity or (RunnerIdentity(worker_id) if worker_id else RunnerIdentity.detect())
        self.worker_id = self.identity.worker_id
        self.stop_event = threading.Event()
        self._handlers: dict[JobType, Handler] | None = None
        self._last_reap = 0.0
        self._last_retention = float("-inf")
        self.maintenance_enabled = True

    @property
    def handlers(self) -> dict[JobType, Handler]:
        if self._handlers is None:
            self._handlers = _handlers()
        return self._handlers

    # ---- housekeeping ---------------------------------------------------------
    def _housekeeping(self) -> None:
        if not self.maintenance_enabled:
            return
        now = time.monotonic()
        if now - self._last_reap > 30:
            self._last_reap = now
            try:
                with new_session() as s:
                    n = jobs.reap_stale(s, self.settings.job_stale_after_seconds)
                if n:
                    log(logger, logging.WARNING, "reaped stale jobs", count=n)
            except Exception:  # noqa: BLE001 - never stop processing for housekeeping
                logger.exception("reaper failed")
        if now - self._last_retention > self.settings.retention_sweep_seconds:
            self._last_retention = now
            maintenance()

    # ---- claiming and running ---------------------------------------------------
    def run_once(
        self,
        *,
        budget_seconds: float | None = None,
        prefer: uuid.UUID | None = None,
        allow_oversized: bool = False,
    ) -> dict | None:
        """Claim and run one job. Returns a summary, or None if nothing was claimable.

        Only jobs whose time limit fits in `budget_seconds` are claimed, unless `allow_oversized`
        (a fresh run: no later run would have more time). A job's time limit is always cut to
        the budget, so it ends before the run is killed.
        """
        self._housekeeping()
        with new_session() as s:
            job = jobs.claim(
                s,
                self.worker_id,
                budget_seconds=None if allow_oversized else budget_seconds,
                prefer=prefer,
                runner_url=self.identity.runner_url,
            )
            if job is None:
                return None
            timeout = float(job.timeout_seconds)
            if budget_seconds is not None:
                timeout = min(timeout, max(60.0, budget_seconds - 60.0))
            snapshot = (job.id, job.type, dict(job.payload), job.attempts, timeout)
        return self._execute(*snapshot)

    def _execute(
        self, job_id: uuid.UUID, job_type: JobType, payload: dict, attempt: int, timeout: float | None = None
    ) -> dict:
        token = job_id_var.set(str(job_id))
        ctx = JobContext(job_id=job_id, payload=payload, attempt=attempt, timeout_seconds=timeout)
        stop = threading.Event()
        beat_every = max(PROGRESS_FLUSH_SECONDS, float(self.settings.job_heartbeat_seconds))

        def _beat() -> None:
            last = time.monotonic()
            while not stop.is_set():
                ctx._wake.wait(PROGRESS_FLUSH_SECONDS)
                ctx._wake.clear()
                if stop.is_set():
                    return
                now = time.monotonic()
                if ctx._dirty or now - last >= beat_every:
                    try:
                        ctx.flush()
                        last = now
                    except Exception:  # noqa: BLE001 - heartbeat must never crash the job
                        logger.warning("heartbeat failed")

        hb = threading.Thread(target=_beat, name="job-heartbeat", daemon=True)
        hb.start()
        started = time.monotonic()
        outcome = "completed"
        log(logger, logging.INFO, "job started", job_type=str(job_type), attempt=attempt)
        try:
            handler = self.handlers.get(job_type)
            if handler is None:
                raise jobs.PermanentJobError(f"no handler for job type {job_type}")
            result = handler(ctx) or {}
            stop.set()
            ctx._wake.set()
            hb.join(timeout=30)
            with new_session() as s:
                jobs.complete(s, job_id, result)
        except Exception as exc:  # noqa: BLE001
            stop.set()
            ctx._wake.set()
            hb.join(timeout=30)
            _log_exception(exc)
            with new_session() as s:
                outcome = jobs.fail(s, job_id, exc)
            _on_job_failure(job_type, payload, exc, final=outcome in {"failed", "cancelled"})
        finally:
            job_id_var.reset(token)
        duration = int((time.monotonic() - started) * 1000)
        log(
            logger, logging.INFO, "job finished", job_type=str(job_type), status=outcome, duration_ms=duration
        )
        return {"job_id": str(job_id), "type": str(job_type), "status": outcome, "duration_ms": duration}

    # ---- loops -----------------------------------------------------------------------
    def run_forever(self) -> None:
        log(logger, logging.INFO, "worker started", worker_id=self.worker_id, **self.settings.redacted())
        while not self.stop_event.is_set():
            try:
                if self.run_once() is None:
                    self.stop_event.wait(self.settings.worker_poll_seconds)
            except Exception:  # noqa: BLE001 - keep the loop alive on DB hiccups
                logger.exception("worker loop error")
                self.stop_event.wait(5)

    def drain(
        self,
        max_jobs: int = 10_000,
        *,
        max_seconds: float | None = None,
        idle_seconds: float = 0.0,
        prefer: uuid.UUID | None = None,
        max_wait_for_retry: float = 300.0,
        fresh_seconds: float = 120.0,
    ) -> DrainResult:
        """Process queued jobs until none is due (after waiting `idle_seconds` for new ones), the
        time budget is spent, or `max_jobs` ran. A retry due within `max_wait_for_retry` is
        waited for rather than left to a future run."""
        res = DrainResult()
        started = time.monotonic()
        deadline = started + max_seconds if max_seconds else None
        idle_since: float | None = None
        while not self.stop_event.is_set():
            if res.processed >= max_jobs:
                res.reason = "max_jobs"
                break
            budget = deadline - time.monotonic() if deadline is not None else None
            if budget is not None and budget < 60:
                res.reason = "budget"
                break
            try:
                done = self.run_once(budget_seconds=budget, prefer=prefer)
                if done is None and budget is not None and time.monotonic() - started < fresh_seconds:
                    # A fresh run: a due job that needs more than the whole budget runs anyway
                    # (time limit cut to fit); handing it to another run could not help.
                    done = self.run_once(budget_seconds=budget, prefer=prefer, allow_oversized=True)
            except Exception:  # noqa: BLE001 - database hiccup: back off, then continue
                logger.exception("drain loop error")
                self.stop_event.wait(5)
                continue
            if done is not None:
                res.processed += 1
                res.failed += done["status"] != "completed"
                res.jobs.append(done)
                prefer, idle_since = None, None
                continue
            with new_session() as s:
                due_in = jobs.seconds_until_due(s, budget)
                if due_in is None and budget is not None:
                    any_due = jobs.seconds_until_due(s, None)
                    if any_due is not None and any_due < 1:
                        res.reason = "budget"  # due work that needs more time than this run has left
                        break
            now = time.monotonic()
            if (
                due_in is not None
                and due_in <= max(idle_seconds, max_wait_for_retry)
                and (deadline is None or now + due_in < deadline - 60)
            ):
                self.stop_event.wait(min(due_in + 0.5, 15.0))
                continue
            idle_since = idle_since or now
            if now - idle_since >= idle_seconds:
                res.reason = "empty"
                break
            self.stop_event.wait(min(5.0, max(0.5, idle_seconds / 10)))
        if self.stop_event.is_set():
            res.reason = "stopped"
        with new_session() as s:
            res.remaining = jobs.queued_count(s)
        return res


def _log_exception(exc: BaseException) -> None:
    """Full tracebacks normally; on public runners only the exception type and code locations
    (messages can contain file names from users' datasets)."""
    if not get_settings().public_logs:
        logger.exception("job error")
        return
    frames = [
        f"{f.filename.rsplit('/', 1)[-1]}:{f.lineno} {f.name}"
        for f in traceback.extract_tb(exc.__traceback__)
    ]
    log(logger, logging.ERROR, "job error", exc_type=type(exc).__name__, frames=frames[-12:])


def _on_job_failure(job_type: JobType, payload: dict, exc: BaseException, final: bool) -> None:
    """Propagate terminal failures to the owning domain row so the UI can show them."""
    if not final:
        return
    from datetime import UTC, datetime

    from datacourt.db import models as m
    from datacourt.enums import RunStatus, VersionStatus

    if isinstance(exc, jobs.JobTimeout):
        msg = "The job exceeded its time limit."
    else:
        msg = f"{type(exc).__name__}: {str(exc)[:300]}"
    with new_session() as s:
        if job_type == JobType.INGEST_VERSION and (
            v := s.get(m.DatasetVersion, uuid.UUID(payload["version_id"]))
        ):
            v.status = VersionStatus.FAILED
            v.error = msg
        elif job_type == JobType.RUN_AUDIT and (a := s.get(m.AuditRun, uuid.UUID(payload["audit_run_id"]))):
            a.status = RunStatus.CANCELLED if isinstance(exc, jobs.JobCancelled) else RunStatus.FAILED
            a.error = msg
            a.finished_at = datetime.now(UTC)
        elif job_type == JobType.WHAT_IF and (w := s.get(m.WhatIfRun, uuid.UUID(payload["what_if_run_id"]))):
            w.status = RunStatus.FAILED
            w.error = msg
        elif job_type == JobType.EXPORT and (e := s.get(m.Export, uuid.UUID(payload["export_id"]))):
            e.status = RunStatus.FAILED
            e.error = msg
        elif job_type == JobType.REPORT and (r := s.get(m.AuditReport, uuid.UUID(payload["report_id"]))):
            r.status = RunStatus.FAILED
            r.error = msg
        elif job_type == JobType.CONTAMINATION and (
            c := s.get(m.ContaminationCheck, uuid.UUID(payload["check_id"]))
        ):
            c.status = RunStatus.FAILED
            c.result = {"error": msg}
        s.commit()


def maintenance() -> dict:
    """Periodic upkeep: retention policy, abandoned uploads. Never raises."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from datacourt.db import models as m
    from datacourt.enums import VersionStatus
    from datacourt.services.retention import sweep
    from datacourt.storage import get_store

    out: dict[str, int] = {"abandoned_uploads": 0}
    try:
        with new_session() as s:
            purged = sweep(s)
            s.commit()
        out.update(purged)
    except Exception:  # noqa: BLE001 - retention must never stop job processing
        logger.exception("retention sweep failed")
    try:
        # Presigned upload URLs last hours; after a day an unfinished upload is abandoned. Its
        # parts are discarded (they count against storage) and the version is marked failed.
        cutoff = datetime.now(UTC) - timedelta(hours=24)
        store = get_store()
        with new_session() as s:
            stale = s.scalars(
                select(m.DatasetVersion).where(
                    m.DatasetVersion.status == VersionStatus.AWAITING_UPLOAD,
                    m.DatasetVersion.created_at < cutoff,
                )
            ).all()
            for v in stale:
                if v.upload_id and v.source_object_key:
                    store.abort_multipart_upload(v.source_object_key, v.upload_id)
                    v.upload_id = None
                v.status = VersionStatus.FAILED
                v.error = "The upload was not completed within 24 hours."
                out["abandoned_uploads"] += 1
            s.commit()
    except Exception:  # noqa: BLE001
        logger.exception("upload cleanup failed")
    return out


def start_embedded_worker() -> Worker:
    worker = Worker()
    t = threading.Thread(target=worker.run_forever, name="embedded-worker", daemon=True)
    t.start()
    return worker


# ---------------------------------------------------------------------------
# Command line (also the GitHub Actions entrypoint)
# ---------------------------------------------------------------------------


def _write_github_output(**values: object) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            for k, v in values.items():
                fh.write(f"{k}={v}\n")


def _write_step_summary(title: str, result: DrainResult | None, extra: dict | None = None) -> None:
    """Ids, types and timings only: summaries of public repositories are public."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [f"### {title}", ""]
    if result is not None:
        lines += [
            f"Processed **{result.processed}** job(s), {result.failed} not completed; "
            f"stopped because: `{result.reason}`; queued afterwards: {result.remaining}.",
            "",
        ]
        if result.jobs:
            lines += ["| job | type | status | seconds |", "|---|---|---|---|"]
            lines += [
                f"| `{j['job_id']}` | {j['type']} | {j['status']} | {j['duration_ms'] / 1000:.1f} |"
                for j in result.jobs
            ]
    for k, v in (extra or {}).items():
        lines.append(f"- {k}: {v}")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _continue_elsewhere(worker: Worker, result: DrainResult) -> None:
    """The budget ran out with work left: dispatch the next run (same slot, so it starts as soon
    as this one ends). Uses the run's own GITHUB_TOKEN, which may dispatch workflows."""
    from sqlalchemy import select

    from datacourt import execution
    from datacourt.enums import RunStatus

    # Only after progress: a run that could not take any job must not start another like it.
    if result.reason != "budget" or result.remaining == 0 or result.processed == 0:
        return
    if not get_settings().github_token:
        return
    with new_session() as s:
        ids = list(
            s.scalars(
                select(Job.id)
                .where(Job.status == RunStatus.QUEUED, Job.cancel_requested.is_(False))
                .order_by(Job.priority, Job.created_at)
                .limit(1)
            )
        )
    if ids:
        outcome = execution.dispatch_github(ids, reason="continue", slot=worker.identity.slot)
        log(logger, logging.INFO, "continuation dispatched", ok=outcome.ok, slot=outcome.slot)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="datacourt-worker", description="DataCourt background worker.")
    ap.add_argument("--drain", action="store_true", help="exit when the queue is empty")
    ap.add_argument(
        "--task",
        choices=["drain", "check", "maintenance", "seed-demo"],
        default=None,
        help="check: report whether work is queued; maintenance: retention and upload cleanup",
    )
    ap.add_argument("--max-seconds", type=float, default=None, help="time budget for --drain")
    ap.add_argument("--idle-seconds", type=float, default=90.0, help="wait this long for new jobs")
    ap.add_argument("--prefer-job", type=str, default=None, help="claim this job first if it is queued")
    args = ap.parse_args(argv)

    settings = get_settings()
    configure_logging(public=settings.public_logs)
    identity = RunnerIdentity.detect()
    from datacourt import execution

    execution.set_local_worker(identity.slot or 0)
    task = args.task or ("drain" if args.drain else "forever")

    if task == "check":
        with new_session() as s:
            jobs.reap_stale(s, settings.job_stale_after_seconds)
            queued = jobs.queued_count(s)
            due_in = jobs.seconds_until_due(s)
        has_work = queued > 0 and due_in is not None and due_in < 300
        _write_github_output(has_work=str(has_work).lower(), queued=queued)
        log(logger, logging.INFO, "queue check", queued=queued, has_work=has_work)
        return 0
    if task == "maintenance":
        out = maintenance()
        _write_step_summary("DataCourt maintenance", None, out)
        log(logger, logging.INFO, "maintenance", **out)
        return 0
    if task == "seed-demo":
        from datacourt.demo import seed_demo

        out = seed_demo(drain=True)
        log(logger, logging.INFO, "demo seed", status=out.get("status"))
        return 0

    worker = Worker(identity=identity)

    def _stop(*_: object) -> None:
        worker.stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    if task == "forever":
        worker.run_forever()
        return 0
    prefer = None
    if args.prefer_job:
        try:
            prefer = uuid.UUID(args.prefer_job)
        except ValueError:
            prefer = None
    log(logger, logging.INFO, "worker draining", worker_id=worker.worker_id, budget_seconds=args.max_seconds)
    result = worker.drain(max_seconds=args.max_seconds, idle_seconds=args.idle_seconds, prefer=prefer)
    log(
        logger,
        logging.INFO,
        "worker finished",
        processed=result.processed,
        failed=result.failed,
        reason=result.reason,
        remaining=result.remaining,
    )
    _write_step_summary("DataCourt worker", result)
    _continue_elsewhere(worker, result)
    return 0


# Keep a reference type for handlers module imports.
__all__ = ["Job", "JobContext", "Worker", "main", "maintenance", "start_embedded_worker"]

if __name__ == "__main__":
    sys.exit(main())
