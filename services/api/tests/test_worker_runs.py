"""Worker behaviour for bounded runs (GitHub Actions): budgets, timeouts, cancellation, draining."""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete

from datacourt import execution, jobs, logging_setup
from datacourt.db import models as m
from datacourt.db.session import new_session
from datacourt.enums import JobType, RunStatus
from datacourt.worker import JobContext, Worker


@pytest.fixture(autouse=True)
def empty_queue():
    with new_session() as s:
        s.execute(delete(m.Job))
        s.commit()


def _job(timeout: int = 600, **kw) -> uuid.UUID:
    with new_session() as s:
        job = jobs.enqueue(
            s, JobType.REPORT, {"report_id": str(uuid.uuid4())}, timeout_seconds=timeout, dispatch=False, **kw
        )
        s.commit()
        return job.id


def _status(job_id: uuid.UUID) -> m.Job:
    with new_session() as s:
        job = s.get(m.Job, job_id)
        assert job is not None
        return job


def _worker(handler) -> Worker:
    w = Worker("test-runner")
    w._handlers = {JobType.REPORT: handler}
    w.maintenance_enabled = False
    return w


def test_timeouts_are_clamped_and_claims_respect_the_budget():
    long_job = _job(timeout=10 * 3600)
    assert _status(long_job).timeout_seconds == jobs.MAX_TIMEOUT_SECONDS
    short_job = _job(timeout=300)
    with new_session() as s:
        got = jobs.claim(s, "w", budget_seconds=600)
        assert got is not None and got.id == short_job
        assert jobs.claim(s, "w", budget_seconds=600) is None  # the long job does not fit
        assert jobs.claim(s, "w", budget_seconds=None).id == long_job


def test_preferred_job_is_claimed_first():
    _job(priority=1)
    wanted = _job(priority=500)
    with new_session() as s:
        assert (
            jobs.claim(s, "w", prefer=wanted, runner_url="https://github.com/a/b/actions/runs/1").id == wanted
        )
        assert s.get(m.Job, wanted).runner_url.endswith("/runs/1")


def test_soft_timeout_fails_the_job_without_retry():
    def slow(ctx: JobContext) -> dict:
        while True:
            ctx.check_cancelled()
            time.sleep(0.05)

    job_id = _job()
    w = _worker(slow)
    with new_session() as s:
        jobs.claim(s, w.worker_id)
    w._execute(job_id, JobType.REPORT, {"report_id": str(uuid.uuid4())}, 1, timeout=0.3)
    job = _status(job_id)
    assert job.status == RunStatus.FAILED and job.error_class == "timeout"
    assert "time limit" in job.error_message


def test_cancelling_a_running_job():
    started = threading.Event()

    def busy(ctx: JobContext) -> dict:
        started.set()
        for _ in range(400):
            ctx.check_cancelled()
            time.sleep(0.05)
        return {}

    job_id = _job()
    w = _worker(busy)

    def cancel() -> None:
        started.wait(5)
        with new_session() as s:
            jobs.request_cancel(s, job_id)
            s.commit()

    threading.Thread(target=cancel).start()
    result = w.drain(max_jobs=1)
    assert result.processed == 1
    assert _status(job_id).status == RunStatus.CANCELLED


def test_progress_is_flushed_by_the_heartbeat_not_the_handler():
    seen: list[float] = []

    def handler(ctx: JobContext) -> dict:
        t0 = time.monotonic()
        for i in range(200):
            ctx.report(i / 200, "WORKING")
        seen.append(time.monotonic() - t0)
        time.sleep(2.6)  # let the heartbeat thread persist the last report
        with new_session() as s:
            job = s.get(m.Job, ctx.job_id)
            seen.append(job.progress)
        return {}

    _job()
    _worker(handler).drain(max_jobs=1)
    assert seen[0] < 0.05  # 200 reports never waited on the database
    assert seen[1] == pytest.approx(199 / 200)


def test_drain_waits_for_a_retry_that_is_due_soon():
    job_id = _job()
    with new_session() as s:
        s.get(m.Job, job_id).run_after = datetime.now(UTC) + timedelta(seconds=2)
        s.commit()
    result = _worker(lambda ctx: {"ok": True}).drain(idle_seconds=0, max_wait_for_retry=10)
    assert result.processed == 1 and _status(job_id).status == RunStatus.COMPLETED


def test_fresh_run_takes_a_job_longer_than_its_budget():
    job_id = _job(timeout=3 * 3600)
    limits: list[float | None] = []
    result = _worker(lambda ctx: limits.append(ctx.timeout_seconds) or {}).drain(
        max_seconds=600, idle_seconds=0
    )
    assert result.processed == 1 and _status(job_id).status == RunStatus.COMPLETED
    assert limits[0] is not None and limits[0] <= 540  # cut to fit the run


def test_drain_stops_for_budget_and_hands_over(monkeypatch):
    _job(timeout=300)
    _job(timeout=3 * 3600)  # needs more time than this (no longer fresh) run has left
    calls = []

    def fake_dispatch(ids, reason, slot=None):
        calls.append((ids, reason, slot))
        return execution.DispatchOutcome(True, slot)

    monkeypatch.setattr(execution, "dispatch_github", fake_dispatch)
    from datacourt import worker as worker_mod

    monkeypatch.setattr(worker_mod.get_settings(), "github_token", "t")
    w = _worker(lambda ctx: {})
    result = w.drain(max_seconds=900, idle_seconds=0, fresh_seconds=0)
    assert result.processed == 1 and result.reason == "budget" and result.remaining == 1
    worker_mod._continue_elsewhere(w, result)
    assert calls and calls[0][1] == "continue"
    # A run that could not take anything never starts a successor (no dispatch loops).
    calls.clear()
    idle = w.drain(max_seconds=900, idle_seconds=0, fresh_seconds=0)
    assert idle.processed == 0 and idle.reason == "budget"
    worker_mod._continue_elsewhere(w, idle)
    assert calls == []


def test_public_logs_hold_no_user_content(capsys):
    root = logging.getLogger()
    logger = logging.getLogger("datacourt.worker")
    old_public, old_level = logging_setup._public, root.level
    handler = logging.StreamHandler()
    handler.setFormatter(logging_setup.JsonFormatter())
    logging_setup._public = True
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        logging_setup.log(
            logger, logging.INFO, "job finished", job_type="report", path="secret/patient-01.png"
        )
        try:
            raise ValueError("cannot read secret/patient-01.png")
        except ValueError:
            logger.exception("job error")
        logging.getLogger("botocore.test").warning("GET https://bucket/secret/patient-01.png")
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
        logging_setup._public = old_public
    err = capsys.readouterr().err
    assert "patient-01" not in err
    lines = [json.loads(x) for x in err.strip().splitlines()]
    assert len(lines) == 3
    assert lines[0]["job_type"] == "report" and "path" not in lines[0]
    assert lines[1]["exc_type"] == "ValueError" and lines[1]["frames"] and "exc" not in lines[1]
    assert lines[2]["logger"] == "botocore.test" and "withheld" in lines[2]["msg"]
