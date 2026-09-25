"""GitHub Actions execution backend: dispatch after commit, failure handling, slots, re-dispatch."""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sqlalchemy import delete, update

from datacourt import execution, jobs
from datacourt.config import get_settings
from datacourt.db import models as m
from datacourt.db.session import new_session
from datacourt.enums import JobType, RunStatus

TOKEN = "github_pat_test_token_never_to_be_logged_0123456789"


class FakeGitHub:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.status = 204
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers.get("content-length", 0)))
                outer.requests.append(
                    {"path": self.path, "headers": dict(self.headers), "json": json.loads(body)}
                )
                self.send_response(outer.status)
                if outer.status == 204:
                    self.end_headers()
                    return
                payload = json.dumps({"message": "Resource not accessible by personal access token"}).encode()
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture()
def github(monkeypatch):
    gh = FakeGitHub()
    settings = get_settings()
    for k, v in {
        "execution_backend": "github_actions",
        "github_api_url": gh.url,
        "github_repository": "acme/datacourt",
        "github_token": TOKEN,
        "github_ref": "main",
        "worker_max_parallel": 2,
    }.items():
        monkeypatch.setattr(settings, k, v)
    monkeypatch.setattr(execution, "_last_poll_check", 0.0)
    execution.install()
    with new_session() as s:
        s.execute(delete(m.Job))
        s.commit()
    yield gh
    gh.close()


def _enqueue(commit: bool = True, **kw) -> uuid.UUID:
    with new_session() as s:
        job = jobs.enqueue(s, JobType.REPORT, {"report_id": str(uuid.uuid4())}, **kw)
        job_id = job.id
        if commit:
            s.commit()
        else:
            s.rollback()
    return job_id


def test_dispatch_after_commit(github):
    job_id = _enqueue()
    assert len(github.requests) == 1
    req = github.requests[0]
    assert req["path"] == "/repos/acme/datacourt/actions/workflows/datacourt-worker.yml/dispatches"
    assert req["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert req["json"] == {
        "ref": "main",
        "inputs": {"task": "drain", "slot": "1", "job_id": str(job_id), "reason": "enqueued"},
    }
    with new_session() as s:
        job = s.get(m.Job, job_id)
        assert job.dispatched_at is not None and job.dispatch_attempts == 1
        assert job.dispatch_slot == 1 and job.dispatch_error is None
        status = execution.status_for(job)
    assert status["waiting"] == "starting_worker"


def test_no_dispatch_when_transaction_rolls_back(github):
    _enqueue(commit=False)
    assert github.requests == []


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(f"{record.getMessage()} {getattr(record, 'fields', '')}")


def test_dispatch_failure_is_recorded_without_the_token(github):
    github.status = 403
    collect = _Collect()
    logger = logging.getLogger("datacourt.execution")
    old_level = logger.level
    logger.addHandler(collect)
    logger.setLevel(logging.DEBUG)
    try:
        job_id = _enqueue()
    finally:
        logger.removeHandler(collect)
        logger.setLevel(old_level)
    with new_session() as s:
        job = s.get(m.Job, job_id)
        assert job.status == RunStatus.QUEUED  # the user's request still succeeded
        assert "403" in job.dispatch_error and "Actions write access" in job.dispatch_error
        assert TOKEN not in job.dispatch_error
        assert execution.status_for(job)["waiting"] == "dispatch_failed"
    assert collect.lines and all(TOKEN not in line for line in collect.lines)


def test_idle_slots_are_used_first(github):
    with new_session() as s:
        running = jobs.enqueue(s, JobType.REPORT, {"report_id": str(uuid.uuid4())}, dispatch=False)
        running.status = RunStatus.RUNNING
        running.locked_by = "gha-s1-r42-a1"
        s.commit()
    _enqueue()
    assert github.requests[-1]["json"]["inputs"]["slot"] == "2"
    _enqueue()  # slot 2 has a pending dispatch now; slot 1 is busy: least loaded wins
    assert github.requests[-1]["json"]["inputs"]["slot"] in ("1", "2")


def test_lost_dispatches_are_retried_when_status_is_polled(github):
    job_id = _enqueue(dispatch=False)
    assert github.requests == []
    outcome = execution.maybe_redispatch(force=True)
    assert outcome is not None and outcome.ok
    assert github.requests[-1]["json"]["inputs"]["reason"] == "retry"
    # Just dispatched: not again until the retry window has passed.
    assert execution.maybe_redispatch(force=True) is None
    with new_session() as s:
        s.execute(
            update(m.Job)
            .where(m.Job.id == job_id)
            .values(dispatched_at=datetime.now(UTC) - timedelta(hours=1))
        )
        s.commit()
    assert execution.maybe_redispatch(force=True) is not None
    assert len(github.requests) == 2


def test_no_redispatch_while_every_slot_has_a_live_worker(github):
    with new_session() as s:
        for slot in (1, 2):
            j = jobs.enqueue(s, JobType.REPORT, {"report_id": str(uuid.uuid4())}, dispatch=False)
            j.status = RunStatus.RUNNING
            j.locked_by = f"gha-s{slot}-r{slot}-a1"
            j.heartbeat_at = datetime.now(UTC)
        s.commit()
    _enqueue(dispatch=False)
    assert execution.maybe_redispatch(force=True) is None
    assert github.requests == []


def test_worker_enqueued_jobs_are_not_redispatched(github):
    execution.set_local_worker(1)
    try:
        with new_session() as s:
            job = jobs.enqueue(s, JobType.REPORT, {"report_id": str(uuid.uuid4())})
            s.commit()
            assert job.dispatch_slot == 1 and job.dispatched_at is not None
    finally:
        execution.set_local_worker(None)
    assert execution.maybe_redispatch(force=True) is None


def test_bad_configuration_is_reported_not_raised(github, monkeypatch):
    monkeypatch.setattr(get_settings(), "github_repository", "not a repo")
    job_id = _enqueue()
    assert github.requests == []
    with new_session() as s:
        assert "DATACOURT_GITHUB_REPO" in s.get(m.Job, job_id).dispatch_error


def test_ambient_ci_variables_are_not_mistaken_for_settings(monkeypatch):
    """CI systems set GITHUB_TOKEN, GITHUB_REF, GITHUB_WORKFLOW...; only DATACOURT_* names count."""
    from datacourt.config import Settings

    for k, v in {
        "GITHUB_TOKEN": "ambient",
        "GITHUB_REF": "refs/heads/x",
        "GITHUB_WORKFLOW": "CI",
        "GITHUB_REPOSITORY": "someone/else",
        "GITHUB_API_URL": "https://example.invalid",
        "ROLE": "worker",
    }.items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert s.github_token is None and s.github_ref is None and s.github_repository is None
    assert s.github_workflow == "datacourt-worker.yml" and s.github_api_url == "https://api.github.com"
    assert s.role == "api"
    monkeypatch.setenv("DATACOURT_GITHUB_TOKEN", "explicit")
    monkeypatch.setenv("DATACOURT_ROLE", "worker")
    assert Settings().github_token == "explicit" and Settings().role == "worker"


def test_vercel_deployments_never_run_with_development_defaults(monkeypatch):
    from datacourt.config import ConfigurationError, load_settings

    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("ENVIRONMENT", "development")
    with pytest.raises(ConfigurationError) as err:
        load_settings()
    problems = " ".join(err.value.problems)
    assert "ENVIRONMENT must be production on Vercel" in problems and "AUTH_SECRET" in problems
