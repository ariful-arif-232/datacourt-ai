"""Production-shaped end-to-end run.

The API runs with EXECUTION_BACKEND=github_actions and dispatches workflow runs to a fake GitHub
API. The fake starts real `datacourt.worker --drain` processes the way the worker workflow does
(one concurrency group per slot: one running run, at most one pending, newer pending replaces
older). Workers and API share PostgreSQL and an S3 endpoint with a versioned bucket standing in for
Backblaze B2. The browser side is played by HTTP calls against presigned URLs.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from datacourt import execution
from datacourt.benchmark.generator import generate
from datacourt.config import get_settings
from datacourt.storage import get_store
from tests.conftest import register
from tests.s3_fixture import BUCKET, moto_s3

API = "/api/v1"
API_DIR = Path(__file__).resolve().parents[1]
REPO = "acme/datacourt"


class FakeGitHubActions:
    def __init__(self, worker_env: dict[str, str], log_dir: Path):
        self.worker_env = worker_env
        self.log_dir = log_dir
        self.dispatches: list[dict] = []
        self.finished: list[dict] = []
        self.fail_next = 0
        self._lock = threading.Lock()
        self._running: dict[str, tuple[subprocess.Popen, dict]] = {}
        self._pending: dict[str, dict] = {}
        self._run_id = 1000
        self._stop = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                ok_path = self.path == f"/repos/{REPO}/actions/workflows/datacourt-worker.yml/dispatches"
                authorized = self.headers.get("authorization", "").startswith("Bearer ")
                with outer._lock:
                    if outer.fail_next > 0:
                        outer.fail_next -= 1
                        self.send_response(502)
                        self.end_headers()
                        return
                    outer.dispatches.append(body)
                    if ok_path and authorized:
                        outer._pending[body["inputs"]["slot"]] = body["inputs"]  # replaces older pending
                self.send_response(204 if ok_path and authorized else 404)
                self.end_headers()

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        threading.Thread(target=self._scheduler, daemon=True).start()

    def _scheduler(self) -> None:
        while not self._stop.wait(0.2):
            with self._lock:
                for slot, (proc, meta) in list(self._running.items()):
                    if proc.poll() is not None:
                        meta["returncode"] = proc.returncode
                        self.finished.append(meta)
                        del self._running[slot]
                for slot, inputs in list(self._pending.items()):
                    if slot not in self._running:
                        del self._pending[slot]
                        self._start(slot, inputs)

    def _start(self, slot: str, inputs: dict) -> None:
        self._run_id += 1
        log = self.log_dir / f"run-{self._run_id}.log"
        env = {
            **os.environ,
            **self.worker_env,
            "GITHUB_ACTIONS": "true",
            "GITHUB_RUN_ID": str(self._run_id),
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_REPOSITORY": REPO,
            "GITHUB_SERVER_URL": "https://github.example",
            "DATACOURT_WORKER_SLOT": slot,
        }
        cmd = [
            sys.executable,
            "-m",
            "datacourt.worker",
            "--drain",
            "--max-seconds",
            "900",
            "--idle-seconds",
            "4",
        ]
        if inputs.get("job_id"):
            cmd += ["--prefer-job", inputs["job_id"]]
        with open(log, "w") as fh:
            proc = subprocess.Popen(cmd, env=env, cwd=API_DIR, stdout=fh, stderr=subprocess.STDOUT)
        self._running[slot] = (proc, {"run_id": self._run_id, "slot": slot, "log": log, "inputs": inputs})

    def idle(self) -> bool:
        with self._lock:
            return not self._running and not self._pending

    def close(self) -> None:
        self._stop.set()
        self.server.shutdown()
        with self._lock:
            for proc, _ in self._running.values():
                proc.terminate()


@pytest.fixture()
def production_like(monkeypatch, tmp_path):
    with moto_s3(cors_origin="https://app.example") as s3_url:
        worker_env = {
            "ENVIRONMENT": "test",
            "DATACOURT_ROLE": "worker",
            "EXECUTION_BACKEND": "none",
            "PUBLIC_LOGS": "true",
            "STORAGE_BACKEND": "s3",
            "S3_ENDPOINT_URL": s3_url,
            "S3_BUCKET": BUCKET,
            "S3_ACCESS_KEY_ID": "k",
            "S3_SECRET_ACCESS_KEY": "s",
            "S3_REGION": "us-east-1",
            "OBJECT_CACHE_DIR": str(tmp_path / "worker-cache"),
            "DATACOURT_GITHUB_TOKEN": "ghs_run_token",
            "DATACOURT_GITHUB_REPO": REPO,
            "DATACOURT_GITHUB_REF": "main",
        }
        gh = FakeGitHubActions(worker_env, tmp_path)
        worker_env["DATACOURT_GITHUB_API_URL"] = gh.url
        settings = get_settings()
        for k, v in {
            "storage_backend": "s3",
            "s3_endpoint": s3_url,
            "s3_bucket": BUCKET,
            "s3_access_key_id": "k",
            "s3_secret_access_key": "s",
            "s3_multipart_threshold_bytes": 512 * 1024,
            "s3_multipart_part_bytes": 5 * 1024 * 1024,
            "execution_backend": "github_actions",
            "github_api_url": gh.url,
            "github_repository": REPO,
            "github_token": "github_pat_api_token",
            "github_ref": "main",
            "worker_max_parallel": 2,
            "dispatch_retry_after_seconds": 2,
        }.items():
            monkeypatch.setattr(settings, k, v)
        monkeypatch.setattr(execution, "POLL_CHECK_SECONDS", 0.0)
        get_store.cache_clear()
        execution.install()
        try:
            yield gh, get_store()
        finally:
            gh.close()
            get_store.cache_clear()


def _wait(client, url: str, done, timeout: float = 420.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        body = client.get(url).json()
        if done(body):
            return body
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {url}: {json.dumps(body)[:2000]}")
        time.sleep(1.0)


def test_upload_audit_review_export_report_delete(client, production_like):
    gh, store = production_like
    data, _ = generate(seed=11, per_class_train=36, per_class_eval=10, minority_train=18)
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    me = register(client, "e2e@example.com", "E2E Owner")
    org = me["organizations"][0]["id"]
    proj = client.post(f"{API}/orgs/{org}/projects", json={"name": "E2E"}).json()
    ds = client.post(f"{API}/projects/{proj['id']}/datasets", json={"name": "shapes"}).json()

    # 1. Browser uploads the archive straight to the bucket (presigned multipart), then finalizes.
    v = client.post(
        f"{API}/datasets/{ds['id']}/versions",
        json={"filename": "shapes.zip", "size_bytes": len(data), "auto_audit": "fast"},
    ).json()
    vid, up = v["version"]["id"], v["upload"]
    assert up["mode"] == "multipart"
    for part in up["parts"]:
        n = part["part_number"]
        chunk = data[(n - 1) * up["part_bytes"] : n * up["part_bytes"]]
        assert httpx.put(part["url"], content=chunk).status_code == 200
    gh.fail_next = 1  # the first dispatch is lost: status polling must recover it
    fin = client.post(f"{API}/versions/{vid}/finalize-upload")
    assert fin.status_code == 200, fin.text
    assert client.get(f"{API}/versions/{vid}").json()["ingest_job"]["waiting"] in (
        "dispatch_failed",
        "starting_worker",
    )

    # 2. A GitHub Actions run ingests and audits; the UI polls the version.
    detail = _wait(
        client,
        f"{API}/versions/{vid}",
        lambda b: (
            b["status"] == "failed"
            or (
                b["status"] == "ready" and b["audits"] and b["audits"][0]["status"] in ("completed", "failed")
            )
        ),
    )
    assert detail["status"] == "ready" and detail["audits"][0]["status"] == "completed", detail
    assert detail["ingest_job"]["runner_url"].startswith(f"https://github.example/{REPO}/actions/runs/")
    first = gh.dispatches[0]
    assert first["ref"] == "main" and first["inputs"]["task"] == "drain"
    assert first["inputs"]["reason"] == "retry"  # recovered after the lost dispatch
    audit_id = detail["audits"][0]["id"]
    prog = client.get(f"{API}/audits/{audit_id}/progress").json()
    assert prog["progress"] == 1.0 and prog["job"]["status"] == "completed"

    # 3. Thumbnails come through the cacheable media route; full images through presigned URLs.
    items = client.get(f"{API}/versions/{vid}/samples", params={"limit": 3}).json()["items"]
    thumb = client.get(items[0]["thumb_url"])
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/webp"
    full = client.get(f"{API}/samples/{items[0]['id']}").json()
    assert httpx.get(full["image_url"]).status_code == 200

    # 4. Review a case, run a what-if experiment, export the cleaned dataset, render a report.
    cases = client.get(f"{API}/versions/{vid}/cases", params={"sort": "priority"}).json()["items"]
    decided = client.post(f"{API}/cases/{cases[0]['id']}/decision", json={"action": "remove", "note": "e2e"})
    assert decided.status_code == 200, decided.text
    w = client.post(
        f"{API}/what-if",
        json={
            "dataset_version_id": vid,
            "name": "e2e",
            "actions": [{"type": "apply_jury_suggestions"}],
            "seeds": 1,
        },
    ).json()
    ex = client.post(f"{API}/exports", json={"dataset_version_id": vid, "auto_audit": None}).json()
    rep = client.post(f"{API}/reports", json={"dataset_version_id": vid}).json()
    wr = _wait(client, f"{API}/what-if/{w['id']}", lambda b: b["status"] in ("completed", "failed"))
    assert wr["status"] == "completed", wr
    exports = _wait(
        client, f"{API}/versions/{vid}/exports", lambda b: b and b[0]["status"] in ("completed", "failed")
    )
    assert exports[0]["status"] == "completed", exports
    dl = client.get(f"{API}/exports/{ex['id']}/download").json()["url"]
    archive = zipfile.ZipFile(io.BytesIO(httpx.get(dl).content))
    assert any(n.endswith("datacourt/SHA256SUMS") for n in archive.namelist())
    report = _wait(client, f"{API}/reports/{rep['id']}", lambda b: b["status"] in ("completed", "failed"))
    assert report["status"] == "completed", report
    assert "DataCourt Audit Report" in httpx.get(report["html_url"]).text

    # 5. Cancelling a queued or running audit.
    deep = client.post(f"{API}/audits", json={"dataset_version_id": vid, "profile": "deep"}).json()
    assert client.post(f"{API}/audits/{deep['id']}/cancel").status_code == 200
    cancelled = _wait(
        client, f"{API}/audits/{deep['id']}/progress", lambda b: b["status"] in ("cancelled", "failed")
    )
    assert cancelled["status"] == "cancelled"

    # 6. Every run exited cleanly, within the slot limit, and logged nothing from the dataset.
    deadline = time.monotonic() + 60
    while not gh.idle() and time.monotonic() < deadline:
        time.sleep(0.5)
    assert gh.finished and all(r["returncode"] == 0 for r in gh.finished)
    assert {r["slot"] for r in gh.finished} <= {"1", "2"}
    logs = "".join(Path(r["log"]).read_text() for r in gh.finished)
    assert "job finished" in logs
    leaked = [n for n in names if n.rsplit("/", 1)[-1] in logs or "synthetic-shapes" in logs]
    assert not leaked, leaked[:3]
    assert "E2E Owner" not in logs and "e2e@example.com" not in logs

    # 7. Deleting the dataset removes every stored object version: images, thumbnails, archives,
    #    audit artifacts, export and report files.
    gone = client.request("DELETE", f"{API}/datasets/{ds['id']}")
    assert gone.status_code == 200 and gone.json()["pending_purges"] == 0
    left = store.client.list_object_versions(Bucket=BUCKET, Prefix=f"orgs/{org}/")
    assert not left.get("Versions") and not left.get("DeleteMarkers")
