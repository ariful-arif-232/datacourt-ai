"""Browser upload flows (single and multipart presigned), thumbnails via the media route, purges."""

from __future__ import annotations

import subprocess
import sys
import textwrap

import httpx
import pytest
from sqlalchemy import select

from datacourt.benchmark.generator import generate
from datacourt.config import get_settings
from datacourt.db import models as m
from datacourt.db.session import new_session
from datacourt.enums import JobType
from datacourt.storage import get_store
from tests.conftest import register
from tests.s3_fixture import BUCKET, moto_s3
from tests.test_api_flow import drain

API = "/api/v1"
MIB = 1024 * 1024


@pytest.fixture(scope="module")
def small_zip() -> bytes:
    data, _ = generate(seed=5, per_class_train=36, per_class_eval=10, minority_train=18)
    return data


def _dataset(client, email: str) -> str:
    me = register(client, email)
    proj = client.post(f"{API}/orgs/{me['organizations'][0]['id']}/projects", json={"name": "P"}).json()
    return client.post(f"{API}/projects/{proj['id']}/datasets", json={"name": "D"}).json()["id"]


def _new_version(client, dataset_id: str, size: int, audit: str | None = None) -> dict:
    r = client.post(
        f"{API}/datasets/{dataset_id}/versions",
        json={"filename": "d.zip", "size_bytes": size, "auto_audit": audit},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_thumbnails_use_stable_cacheable_media_urls(client, small_zip):
    ds = _dataset(client, "media@example.com")
    v = _new_version(client, ds, len(small_zip))
    assert v["upload"]["mode"] == "single"
    client.put(
        v["upload"]["url"].replace("http://testserver", ""), content=small_zip, headers=v["upload"]["headers"]
    )
    assert client.post(f"{API}/versions/{v['version']['id']}/finalize-upload").status_code == 200
    drain()
    first = client.get(f"{API}/versions/{v['version']['id']}/samples?limit=5").json()["items"]
    again = client.get(f"{API}/versions/{v['version']['id']}/samples?limit=5").json()["items"]
    url = first[0]["thumb_url"]
    assert url.startswith("/api/v1/media/") and url == again[0]["thumb_url"]
    r = client.get(url)
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    cache = r.headers["cache-control"]
    assert "public" in cache and "s-maxage=" in cache and "immutable" in cache
    assert client.get(url + "?v=2").status_code == 400  # cannot be varied to bypass the CDN cache
    assert client.get(url[:-3] + "AAA").status_code == 403
    # Capabilities are only minted for thumbnails.
    from datacourt.security import sign_payload

    token = sign_payload(
        {"k": "orgs/x/source/original.zip", "m": "MEDIA", "exp": 4e9}, get_settings().signed_url_secret
    )
    assert client.get(f"{API}/media/{token}").status_code == 403


def test_single_upload_rejects_non_zip(client):
    ds = _dataset(client, "notzip@example.com")
    v = _new_version(client, ds, 11)
    client.put(
        v["upload"]["url"].replace("http://testserver", ""),
        content=b"hello world",
        headers=v["upload"]["headers"],
    )
    r = client.post(f"{API}/versions/{v['version']['id']}/finalize-upload")
    assert r.status_code == 415
    assert client.get(f"{API}/versions/{v['version']['id']}").json()["status"] == "failed"


def test_finalize_before_upload_and_abort(client):
    ds = _dataset(client, "abort@example.com")
    v = _new_version(client, ds, 1000)
    assert client.post(f"{API}/versions/{v['version']['id']}/finalize-upload").status_code == 400
    r = client.post(f"{API}/versions/{v['version']['id']}/upload/abort")
    assert r.status_code == 200 and r.json()["version"]["status"] == "failed"
    assert client.post(f"{API}/versions/{v['version']['id']}/finalize-upload").status_code == 409


@pytest.fixture()
def s3_backend(monkeypatch):
    """Point the running app at an S3 endpoint (moto) for one test."""
    with moto_s3() as endpoint:
        settings = get_settings()
        for k, val in {
            "storage_backend": "s3",
            "s3_endpoint": endpoint,
            "s3_bucket": BUCKET,
            "s3_access_key_id": "k",
            "s3_secret_access_key": "s",
            "s3_multipart_threshold_bytes": 6 * MIB,
            "s3_multipart_part_bytes": 5 * MIB,
        }.items():
            monkeypatch.setattr(settings, k, val)
        get_store.cache_clear()
        try:
            yield get_store()
        finally:
            get_store.cache_clear()


def test_multipart_upload_resume_finalize_and_purge(client, s3_backend):
    store = s3_backend
    ds = _dataset(client, "multipart@example.com")
    body = b"PK\x03\x04" + b"z" * (11 * MIB)
    v = _new_version(client, ds, len(body))
    vid, up = v["version"]["id"], v["upload"]
    assert up["mode"] == "multipart" and up["part_bytes"] == 5 * MIB and len(up["parts"]) == 3

    def put(n: int, url: str) -> None:
        chunk = body[(n - 1) * up["part_bytes"] : n * up["part_bytes"]]
        assert httpx.put(url, content=chunk).status_code == 200

    put(1, up["parts"][0]["url"])
    # Interrupted: the server knows which parts arrived and refuses to finalize.
    assert client.get(f"{API}/versions/{vid}/upload").json()["uploaded_parts"] == [1]
    r = client.post(f"{API}/versions/{vid}/finalize-upload")
    assert r.status_code == 409 and r.json()["missing_parts"] == [2, 3]
    # Resume with fresh URLs for the missing parts.
    fresh = client.post(f"{API}/versions/{vid}/upload/parts", json={"part_numbers": [2, 3]}).json()["parts"]
    for p in fresh:
        put(p["part_number"], p["url"])
    r = client.post(f"{API}/versions/{vid}/finalize-upload")
    assert r.status_code == 200, r.text
    assert r.json()["version"]["status"] == "uploaded" and r.json()["version"]["source_bytes"] == len(body)
    with new_session() as s:
        version = s.get(m.DatasetVersion, r.json()["version"]["id"])
        assert version.upload_id is None
        source_key = version.source_object_key
        job = s.scalar(select(m.Job).where(m.Job.idempotency_key == f"ingest:{vid}"))
        assert job is not None and job.type == JobType.INGEST_VERSION and job.timeout_seconds >= 900
    assert store.read_range(source_key, 0, 4) == (b"PK\x03\x04", len(body))
    # Deleting the dataset purges every stored version of every object, inline.
    store.put_bytes(source_key, b"PK-second-version")
    r = client.request("DELETE", f"{API}/datasets/{ds}")
    assert r.status_code == 200 and r.json()["pending_purges"] == 0
    org_prefix = source_key.split("/datasets/")[0] + "/"
    left = store.client.list_object_versions(Bucket=BUCKET, Prefix=org_prefix)
    assert not left.get("Versions") and not left.get("DeleteMarkers")


def test_api_import_does_not_load_the_ml_stack():
    code = textwrap.dedent(
        """
        import sys
        import datacourt.main  # noqa: F401
        heavy = sorted(
            m for m in ("cv2", "sklearn", "scipy", "PIL", "pillow_heif", "faiss", "torch") if m in sys.modules
        )
        print(",".join(heavy))
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=True
    )
    assert out.stdout.strip() == "", f"API import pulled in: {out.stdout.strip()}"
