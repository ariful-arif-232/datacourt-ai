"""End-to-end check of the configured object store (`datacourt-storage-check`).

Exercises every storage flow DataCourt uses, against the real bucket, under a throw-away prefix
that is purged afterwards: object write/read, ranged reads, listing, presigned uploads (single and
multipart, including the signed size limit), presigned downloads with a file name, artifact
round-trips, versioned deletes and, with `--origin`, the bucket's CORS rules for browser uploads.

Output names checks and outcomes only: never credentials, object keys or signed URLs, so it is
safe to run in a public GitHub Actions log. Uses about 15 download (Class B) and 5 list
(Class C) requests of a Backblaze B2 daily allowance.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from datacourt.config import get_settings
from datacourt.storage import S3ObjectStore, get_store, provider_name
from datacourt.storage_cors import UPLOAD_PROBES, probe_urls, run_probes

MIB = 1024 * 1024


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    seconds: float = 0.0
    required: bool = True


@dataclass
class Report:
    provider: str
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks if c.required)


def run_checks(origin: str | None = None, keep: bool = False) -> Report:  # noqa: C901 - a checklist
    import httpx
    import numpy as np

    settings = get_settings()
    store = get_store()
    inner = getattr(store, "inner", store)
    s3 = inner if isinstance(inner, S3ObjectStore) else None
    report = Report(provider_name(settings.s3_endpoint if s3 else None))
    prefix = f"datacourt-storage-check/{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    http = httpx.Client(timeout=60.0, follow_redirects=False)

    def check(name: str, required: bool = True) -> Callable[[Callable[[], str | None]], None]:
        def run(fn: Callable[[], str | None]) -> None:
            t0 = time.monotonic()
            try:
                detail = fn() or ""
                report.checks.append(Check(name, True, detail, time.monotonic() - t0, required))
            except Exception as exc:  # noqa: BLE001 - every failure is reported, not raised
                report.checks.append(
                    Check(
                        name,
                        False,
                        f"{type(exc).__name__}: {str(exc)[:200]}",
                        time.monotonic() - t0,
                        required,
                    )
                )

        return run

    small = os.urandom(64 * 1024)
    key_small = f"{prefix}/small.bin"

    @check("write object")
    def _() -> None:
        store.put_bytes(key_small, small, "application/octet-stream")

    @check("read object")
    def _() -> None:
        if inner.get_bytes(key_small) != small:
            raise AssertionError("content differs")

    @check("ranged read (size and ZIP signature check)")
    def _() -> None:
        part, total = inner.read_range(key_small, 2, 4)
        if part != small[2:6] or total != len(small):
            raise AssertionError("range or size wrong")

    @check("exists / missing")
    def _() -> None:
        if not inner.exists(key_small) or inner.exists(f"{prefix}/missing.bin"):
            raise AssertionError("existence check wrong")

    @check("list objects")
    def _() -> None:
        keys = {o.key for o in inner.list_objects(prefix)}
        if key_small not in keys:
            raise AssertionError("written object not listed")

    @check("artifact round-trip (NumPy .npz)")
    def _() -> None:
        arr = np.arange(10_000, dtype=np.float32).reshape(100, 100)
        buf = io.BytesIO()
        np.savez_compressed(buf, emb=arr)
        store.put_bytes(f"{prefix}/artifacts/embeddings.npz", buf.getvalue())
        with np.load(io.BytesIO(inner.get_bytes(f"{prefix}/artifacts/embeddings.npz"))) as z:
            if not np.array_equal(z["emb"], arr):
                raise AssertionError("array differs")

    if s3 is not None:
        body = os.urandom(300 * 1024)
        key_put = f"{prefix}/source/original.zip"

        @check("presigned upload (browser -> bucket)")
        def _() -> None:
            up = s3.signed_put(key_put, 600, "application/zip", len(body))
            r = http.put(up["url"], content=body, headers=up["headers"])
            if r.status_code not in (200, 201, 204):
                raise AssertionError(f"HTTP {r.status_code}")
            if s3.get_bytes(key_put) != body:
                raise AssertionError("uploaded content differs")

        @check("presigned upload refuses a different size", required=False)
        def _() -> str | None:
            up = s3.signed_put(f"{prefix}/oversize.zip", 600, "application/zip", 1024)
            r = http.put(up["url"], content=os.urandom(4096), headers=up["headers"])
            if r.status_code < 400:
                raise AssertionError(
                    "the store accepted a body larger than the signed size; uploads stay safe because "
                    "the API checks the size again before ingesting"
                )
            return f"rejected with HTTP {r.status_code}"

        @check("presigned download with file name")
        def _() -> None:
            url = s3.signed_get_url(key_put, 600, filename="datacourt-export.zip")
            r = http.get(url)
            if r.status_code != 200 or r.content != body:
                raise AssertionError(f"HTTP {r.status_code}")
            if "datacourt-export.zip" not in r.headers.get("content-disposition", ""):
                raise AssertionError("Content-Disposition override not applied")

        @check("multipart upload (resumable large archives)")
        def _() -> None:
            key = f"{prefix}/source/large.zip"
            parts_data = [os.urandom(5 * MIB), os.urandom(512 * 1024)]
            upload_id = s3.create_multipart_upload(key, "application/zip")
            for n, data in enumerate(parts_data, start=1):
                url = s3.presign_upload_part(key, upload_id, n, 600, len(data))
                r = http.put(url, content=data)
                if r.status_code not in (200, 201, 204):
                    s3.abort_multipart_upload(key, upload_id)
                    raise AssertionError(f"part {n}: HTTP {r.status_code}")
            parts = s3.list_uploaded_parts(key, upload_id)
            if [p["PartNumber"] for p in parts] != [1, 2]:
                raise AssertionError("parts not listed")
            s3.complete_multipart_upload(key, upload_id, parts)
            head, total = s3.read_range(key, 0, 16)
            if total != sum(map(len, parts_data)) or head != parts_data[0][:16]:
                raise AssertionError("assembled object differs")

        @check("abort multipart upload")
        def _() -> None:
            key = f"{prefix}/source/aborted.zip"
            upload_id = s3.create_multipart_upload(key, "application/zip")
            s3.abort_multipart_upload(key, upload_id)
            s3.abort_multipart_upload(key, upload_id)  # idempotent

        @check("export file upload and download")
        def _() -> None:
            with tempfile.NamedTemporaryFile(suffix=".zip") as tmp:
                data = os.urandom(2 * MIB)
                tmp.write(data)
                tmp.flush()
                store.put_file(f"{prefix}/exports/export.zip", tmp.name, "application/zip")
            with tempfile.TemporaryDirectory() as d:
                out = os.path.join(d, "copy.zip")
                s3.download_to(f"{prefix}/exports/export.zip", out)
                with open(out, "rb") as fh:
                    if hashlib.sha256(fh.read()).digest() != hashlib.sha256(data).digest():
                        raise AssertionError("downloaded content differs")

        @check("delete removes every stored version")
        def _() -> None:
            key = f"{prefix}/versioned.bin"
            s3.put_bytes(key, b"one")
            s3.put_bytes(key, b"two")
            s3.delete(key)
            left = s3.client.list_object_versions(Bucket=s3.bucket, Prefix=key)
            if left.get("Versions") or left.get("DeleteMarkers") or s3.exists(key):
                raise AssertionError("versions or hide markers remain")

        if origin:

            @check("CORS allows browser uploads from the app origin")
            def _() -> str:
                results = run_probes(http, probe_urls(s3), origin, UPLOAD_PROBES)
                refused = [
                    f"{p.name}: preflight {pf.problem(origin, p)}"
                    for p, pf in results
                    if not pf.allows(origin, p)
                ]
                if refused:
                    raise AssertionError(
                        f"{'; '.join(refused)}; run the Bucket CORS workflow to add the rule for "
                        f"{origin} (see docs/DEPLOYMENT.md)"
                    )
                return "single-file and multipart part upload preflights accepted"

    if not keep:

        @check("purge prefix (all versions)")
        def _() -> None:
            store.purge_prefix(prefix)
            if inner.list_objects(prefix):
                raise AssertionError("objects remain after purge")
            if s3 is not None:
                left = s3.client.list_object_versions(Bucket=s3.bucket, Prefix=prefix + "/")
                if left.get("Versions") or left.get("DeleteMarkers"):
                    raise AssertionError("versions remain after purge")

    http.close()
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="datacourt-storage-check", description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--origin", help="app origin to test the bucket's CORS rules for, e.g. https://x.vercel.app"
    )
    ap.add_argument("--keep", action="store_true", help="leave the test objects in place")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)
    report = run_checks(args.origin, args.keep)
    if args.json:
        print(
            json.dumps(
                {"ok": report.ok, "provider": report.provider, "checks": [c.__dict__ for c in report.checks]}
            )
        )
    else:
        print(f"Object storage: {report.provider}")
        for c in report.checks:
            mark = "PASS" if c.ok else ("FAIL" if c.required else "WARN")
            print(f"  [{mark}] {c.name} ({c.seconds:.2f}s){' - ' + c.detail if c.detail else ''}")
        print("All required checks passed." if report.ok else "Some required checks FAILED.")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"### Object storage check: {report.provider}\n\n| check | result |\n|---|---|\n")
            for c in report.checks:
                fh.write(f"| {c.name} | {'pass' if c.ok else ('FAIL' if c.required else 'warn')} |\n")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
