"""S3-compatible storage (Backblaze B2 in production) against a local moto server."""

from __future__ import annotations

import hashlib
import io
import uuid
import zipfile

import httpx
import pytest

from datacourt.storage import CachingObjectStore, ObjectNotFound, S3ObjectStore
from tests.s3_fixture import BUCKET, moto_s3, s3_settings


@pytest.fixture(scope="module")
def endpoint():
    with moto_s3(cors_origin="https://app.example") as url:
        yield url


@pytest.fixture()
def store(endpoint) -> S3ObjectStore:
    return S3ObjectStore(s3_settings(endpoint, s3_multipart_part_bytes=5 * 1024 * 1024))


def _prefix() -> str:
    return f"orgs/{uuid.uuid4()}"


def _versions(store: S3ObjectStore, prefix: str) -> list:
    r = store.client.list_object_versions(Bucket=BUCKET, Prefix=prefix)
    return r.get("Versions", []) + r.get("DeleteMarkers", [])


def test_bytes_ranges_listing(store):
    p = _prefix()
    store.put_bytes(f"{p}/a.bin", b"PK\x03\x04rest-of-archive")
    store.put_bytes(f"{p}/sub/b.bin", b"x" * 10)
    assert store.get_bytes(f"{p}/a.bin").startswith(b"PK")
    head, total = store.read_range(f"{p}/a.bin", 0, 4)
    assert head == b"PK\x03\x04" and total == 19
    assert store.read_range(f"{p}/a.bin", 17, 10) == (b"ve", 19)
    assert store.exists(f"{p}/a.bin") and not store.exists(f"{p}/zzz.bin")
    assert store.size(f"{p}/sub/b.bin") == 10
    assert sorted(o.key for o in store.list_objects(p)) == [f"{p}/a.bin", f"{p}/sub/b.bin"]
    with pytest.raises(ObjectNotFound):
        store.get_bytes(f"{p}/missing")
    with pytest.raises(ObjectNotFound):
        store.read_range(f"{p}/missing", 0, 4)


def test_prefix_is_a_directory(store):
    p = _prefix()
    store.put_bytes(f"{p}/keep", b"1")
    store.put_bytes(f"{p}x/other", b"2")  # shares the string prefix, not the directory
    assert store.purge_prefix(p).deleted == 1
    assert store.exists(f"{p}x/other")


def test_delete_and_purge_remove_every_version(store):
    p = _prefix()
    for body in (b"v1", b"v2", b"v3"):
        store.put_bytes(f"{p}/obj", body)
    store.put_bytes(f"{p}/other", b"o")
    assert len(_versions(store, f"{p}/obj")) == 3
    store.delete(f"{p}/obj")
    assert _versions(store, f"{p}/obj") == []
    assert store.exists(f"{p}/other")
    store.client.delete_object(Bucket=BUCKET, Key=f"{p}/other")  # leaves a delete marker (B2: hide marker)
    assert _versions(store, p)
    result = store.purge_prefix(p)
    assert result.complete
    assert _versions(store, p) == []


def test_presigned_single_upload_and_named_download(store):
    key = f"{_prefix()}/source/original.zip"
    body = b"PK" + bytes(range(256)) * 40
    up = store.signed_put(key, 600, "application/zip", len(body))
    assert "content-length" in up["url"].lower()  # the declared size is part of the signature
    r = httpx.put(up["url"], content=body, headers=up["headers"])
    assert r.status_code == 200
    r = httpx.get(store.signed_get_url(key, 600, filename="report ü.zip"))
    assert r.status_code == 200 and r.content == body
    assert "attachment" in r.headers["content-disposition"]


def test_presigned_multipart_upload(store):
    key = f"{_prefix()}/source/original.zip"
    parts = [b"a" * (5 * 1024 * 1024), b"b" * 1000]
    upload_id = store.create_multipart_upload(key)
    for n, data in enumerate(parts, start=1):
        r = httpx.put(store.presign_upload_part(key, upload_id, n, 600, len(data)), content=data)
        assert r.status_code == 200
    listed = store.list_uploaded_parts(key, upload_id)
    assert [x["PartNumber"] for x in listed] == [1, 2]
    assert [x["Size"] for x in listed] == [len(x) for x in parts]
    store.complete_multipart_upload(key, upload_id, listed)
    assert store.read_range(key, 0, 2) == (b"aa", sum(map(len, parts)))
    other = store.create_multipart_upload(f"{key}.2")
    store.abort_multipart_upload(f"{key}.2", other)
    store.abort_multipart_upload(f"{key}.2", other)  # idempotent


def test_cors_preflight_for_browser_uploads(store):
    up = store.signed_put(f"{_prefix()}/c.zip", 600, "application/zip", 3)
    r = httpx.options(
        up["url"],
        headers={
            "Origin": "https://app.example",
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert r.headers.get("access-control-allow-origin") == "https://app.example"


def test_download_to_streams_whole_object(store, tmp_path):
    key = f"{_prefix()}/big.bin"
    data = bytes(range(256)) * 50_000
    store.put_bytes(key, data)
    out = tmp_path / "copy.bin"
    store.download_to(key, out)
    assert hashlib.sha256(out.read_bytes()).digest() == hashlib.sha256(data).digest()


def test_caching_store_is_write_through(store, tmp_path):
    cache = CachingObjectStore(store, tmp_path / "cache", max_bytes=10_000)
    p = _prefix()
    cache.put_bytes(f"{p}/art.npz", b"artifact")
    # Deleted behind the cache's back: reads are still served locally (no store request).
    store.client.delete_object(Bucket=BUCKET, Key=f"{p}/art.npz")
    assert cache.get_bytes(f"{p}/art.npz") == b"artifact"
    assert cache.exists(f"{p}/art.npz")
    # Negative lookups are remembered until something is written.
    assert not cache.exists(f"{p}/later.npz")
    store.put_bytes(f"{p}/later.npz", b"x")
    assert not cache.exists(f"{p}/later.npz")
    cache.put_bytes(f"{p}/later.npz", b"y")
    assert cache.exists(f"{p}/later.npz")
    # Eviction keeps the cache within its size budget.
    for i in range(5):
        cache.put_bytes(f"{p}/blob{i}", b"z" * 4000)
    assert sum(f.stat().st_size for f in (tmp_path / "cache").rglob("*") if f.is_file()) <= 10_000
    # Local copies are downloaded once.
    store.put_bytes(f"{p}/src.zip", b"zipbytes")
    path = cache.local_copy(f"{p}/src.zip")
    store.client.delete_object(Bucket=BUCKET, Key=f"{p}/src.zip")
    assert cache.local_copy(f"{p}/src.zip") == path and path.read_bytes() == b"zipbytes"
    cache.purge_prefix(p)
    assert not path.exists()


def test_sample_reader_uses_the_source_archive(store, tmp_path):
    from datacourt.sample_objects import SampleObjectReader

    img = b"\x89PNG fake image bytes"
    sha = hashlib.sha256(img).hexdigest()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("data/cats/1.png", img)
    p = _prefix()
    store.put_bytes(f"{p}/source/original.zip", buf.getvalue())
    cache = CachingObjectStore(store, tmp_path / "cache", max_bytes=10**8)
    reader = SampleObjectReader(uuid.uuid4(), cache, source_key=f"{p}/source/original.zip")
    # The per-object copy does not exist: the bytes must come from the archive, verified by hash.
    assert reader.read(f"{p}/objects/{sha[:2]}/{sha}.png", "data/cats/1.png", sha) == img
    assert cache.cached_path(f"{p}/objects/{sha[:2]}/{sha}.png") is not None
    # A hash mismatch falls back to the object itself.
    with pytest.raises(ObjectNotFound):
        reader.read(f"{p}/objects/xx/other.png", "data/cats/1.png", "0" * 64)
    reader.close()


def test_purge_spanning_several_listing_pages(store):
    p = _prefix()
    for i in range(1100):
        store.client.put_object(Bucket=BUCKET, Key=f"{p}/k{i:05d}", Body=b"x")
    partial = store.purge_prefix(p, deadline=0.0)  # deadline already passed: one batch, then stop
    assert not partial.complete and partial.deleted == 1000
    rest = store.purge_prefix(p)
    assert rest.complete and rest.deleted == 100
    assert _versions(store, p) == []
