"""Object storage abstraction.

Keys are always generated server-side from UUIDs and content hashes, never from user
input, and every tenant's data lives under `orgs/{org_id}/`. Objects are private: the
browser only receives short-lived signed URLs, or, for immutable thumbnails, a signed
capability URL on the app origin (`media_url`) that a CDN may cache.

Production uses Backblaze B2 through its S3-compatible API. Without a payment method
B2 caps downloads (Class B: GET/HEAD) and listings (Class C) per day, so this module
avoids existence checks before writes, reads byte ranges instead of HEAD + GET, and the
worker reads through a local write-through cache (`CachingObjectStore`).
"""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, BinaryIO, Protocol
from urllib.parse import quote

from datacourt.config import Settings, get_settings
from datacourt.security import sign_payload


class ObjectNotFound(LookupError):
    """The object does not exist."""


class TooLarge(Exception):
    pass


class StorageUnavailable(RuntimeError):
    """The object store refused the request for capacity reasons (e.g. a daily free-tier cap)."""


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    size: int


@dataclass(frozen=True)
class PurgeResult:
    deleted: int
    complete: bool


class ObjectStore(Protocol):
    supports_multipart: bool

    def put_bytes(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        cache_control: str | None = None,
    ) -> None: ...
    def put_file(
        self, key: str, path: str | Path, content_type: str = "application/octet-stream"
    ) -> None: ...
    def put_stream(self, key: str, stream: BinaryIO, max_bytes: int) -> int: ...
    def get_bytes(self, key: str) -> bytes: ...
    def read_range(self, key: str, start: int, length: int) -> tuple[bytes, int]: ...
    def download_to(self, key: str, path: str | Path) -> None: ...
    def exists(self, key: str) -> bool: ...
    def size(self, key: str) -> int: ...
    def list_objects(self, prefix: str, limit: int = 1000) -> list[ObjectInfo]: ...
    def delete(self, key: str) -> None: ...
    def delete_prefix(self, prefix: str) -> int: ...
    def purge_prefix(self, prefix: str, deadline: float | None = None) -> PurgeResult: ...
    def signed_get_url(self, key: str, ttl: int | None = None, filename: str | None = None) -> str: ...
    def signed_put(
        self,
        key: str,
        ttl: int | None = None,
        content_type: str = "application/zip",
        content_length: int | None = None,
    ) -> dict: ...
    def create_multipart_upload(self, key: str, content_type: str = "application/zip") -> str: ...
    def presign_upload_part(
        self,
        key: str,
        upload_id: str,
        part_number: int,
        ttl: int | None = None,
        content_length: int | None = None,
    ) -> str: ...
    def list_uploaded_parts(self, key: str, upload_id: str) -> list[dict]: ...
    def complete_multipart_upload(self, key: str, upload_id: str, parts: list[dict]) -> None: ...
    def abort_multipart_upload(self, key: str, upload_id: str) -> None: ...


def _check_key(key: str) -> str:
    if not key or key.startswith("/") or ".." in key.split("/") or "\\" in key or "\x00" in key:
        raise ValueError("invalid object key")
    return key


def _dir_prefix(prefix: str) -> str:
    """Prefixes are always directories: `orgs/<id>` must never match `orgs/<id>x/...`."""
    return _check_key(prefix.rstrip("/")) + "/"


def content_disposition(filename: str) -> str:
    ascii_name = "".join(c if 32 <= ord(c) < 127 and c not in '"\\;' else "_" for c in filename) or "download"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


# ---------------------------------------------------------------------------
# Local filesystem (development, tests, single-node deployments)
# ---------------------------------------------------------------------------


class LocalObjectStore:
    """Filesystem-backed store for development and single-node deployments."""

    supports_multipart = False

    def __init__(self, settings: Settings):
        self.root = Path(settings.local_storage_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.settings = settings

    def _path(self, key: str) -> Path:
        p = (self.root / _check_key(key)).resolve()
        if self.root not in p.parents:
            raise ValueError("invalid object key")
        return p

    def _existing(self, key: str) -> Path:
        p = self._path(key)
        if not p.is_file():
            raise ObjectNotFound(key)
        return p

    def put_bytes(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        cache_control: str | None = None,
    ) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + f".tmp-{uuid.uuid4().hex}")
        tmp.write_bytes(data)
        os.replace(tmp, p)

    def put_file(self, key: str, path: str | Path, content_type: str = "application/octet-stream") -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + f".tmp-{uuid.uuid4().hex}")
        shutil.copyfile(path, tmp)
        os.replace(tmp, p)

    def put_stream(self, key: str, stream: BinaryIO, max_bytes: int) -> int:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + f".tmp-{uuid.uuid4().hex}")
        total = 0
        try:
            with open(tmp, "wb") as fh:
                while chunk := stream.read(1024 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise TooLarge()
                    fh.write(chunk)
            os.replace(tmp, p)
        finally:
            if tmp.exists():
                tmp.unlink()
        return total

    def get_bytes(self, key: str) -> bytes:
        return self._existing(key).read_bytes()

    def read_range(self, key: str, start: int, length: int) -> tuple[bytes, int]:
        p = self._existing(key)
        with open(p, "rb") as fh:
            fh.seek(start)
            data = fh.read(length)
        return data, p.stat().st_size

    def download_to(self, key: str, path: str | Path) -> None:
        shutil.copyfile(self._existing(key), path)

    def local_path(self, key: str) -> Path:
        return self._path(key)

    def local_copy(self, key: str) -> Path:
        return self._existing(key)

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def size(self, key: str) -> int:
        return self._existing(key).stat().st_size

    def list_objects(self, prefix: str, limit: int = 1000) -> list[ObjectInfo]:
        base = self._path(_dir_prefix(prefix).rstrip("/"))
        if not base.is_dir():
            return []
        out: list[ObjectInfo] = []
        for f in sorted(base.rglob("*")):
            if f.is_file() and ".tmp-" not in f.name:
                out.append(ObjectInfo(f.relative_to(self.root).as_posix(), f.stat().st_size))
                if len(out) >= limit:
                    break
        return out

    def delete(self, key: str) -> None:
        p = self._path(key)
        if p.exists():
            p.unlink()

    def delete_prefix(self, prefix: str) -> int:
        return self.purge_prefix(prefix).deleted

    def purge_prefix(self, prefix: str, deadline: float | None = None) -> PurgeResult:
        p = self._path(_dir_prefix(prefix).rstrip("/"))
        if not p.is_dir():
            return PurgeResult(0, True)
        count = sum(1 for f in p.rglob("*") if f.is_file())
        shutil.rmtree(p)
        return PurgeResult(count, True)

    def _token(self, key: str, method: str, ttl: int | None, **extra: object) -> str:
        ttl = ttl or self.settings.signed_url_ttl_seconds
        return sign_payload(
            {"k": key, "m": method, "exp": int(time.time()) + ttl, **extra}, self.settings.signed_url_secret
        )

    def signed_get_url(self, key: str, ttl: int | None = None, filename: str | None = None) -> str:
        # Relative so it is served through the app origin (Next.js rewrite).
        extra = {"fn": filename} if filename else {}
        return f"/api/v1/storage/object/{self._token(_check_key(key), 'GET', ttl, **extra)}"

    def signed_put(
        self,
        key: str,
        ttl: int | None = None,
        content_type: str = "application/zip",
        content_length: int | None = None,
    ) -> dict:
        # The upload route enforces the size limit while streaming.
        token = self._token(_check_key(key), "PUT", ttl or self.settings.upload_url_ttl_seconds)
        return {
            "url": f"{self.settings.public_api_url.rstrip('/')}/api/v1/storage/upload/{token}",
            "method": "PUT",
            "headers": {"Content-Type": content_type},
        }

    def create_multipart_upload(self, key: str, content_type: str = "application/zip") -> str:
        raise NotImplementedError("the local store has no multipart uploads")

    def presign_upload_part(
        self,
        key: str,
        upload_id: str,
        part_number: int,
        ttl: int | None = None,
        content_length: int | None = None,
    ) -> str:
        raise NotImplementedError("the local store has no multipart uploads")

    def list_uploaded_parts(self, key: str, upload_id: str) -> list[dict]:
        raise NotImplementedError("the local store has no multipart uploads")

    def complete_multipart_upload(self, key: str, upload_id: str, parts: list[dict]) -> None:
        raise NotImplementedError("the local store has no multipart uploads")

    def abort_multipart_upload(self, key: str, upload_id: str) -> None:
        return None


# ---------------------------------------------------------------------------
# S3-compatible (Backblaze B2 in production; also AWS S3, MinIO)
# ---------------------------------------------------------------------------


def _error(exc: Any) -> tuple[str, int, str]:
    err = getattr(exc, "response", {}) or {}
    return (
        str(err.get("Error", {}).get("Code", "")),
        int(err.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0),
        str(err.get("Error", {}).get("Message", "")),
    )


def provider_name(endpoint: str | None) -> str:
    """Which object store an endpoint belongs to, for logs (never the endpoint itself)."""
    host = (endpoint or "").split("://")[-1].split("/")[0].lower()
    if host.endswith("backblazeb2.com"):
        return "Backblaze B2"
    if host.endswith("amazonaws.com"):
        return "AWS S3"
    return "S3-compatible" if host else "local filesystem"


def _add_content_md5(request: Any, **_: object) -> None:
    """DeleteObjects and PutBucketCors must carry an integrity header. Recent AWS SDKs send only
    CRC32, which not every S3-compatible service accepts; Content-MD5 is understood everywhere."""
    body = request.body
    if isinstance(body, str):
        body = body.encode()
    if isinstance(body, bytes) and "Content-MD5" not in request.headers:
        request.headers["Content-MD5"] = base64.b64encode(hashlib.md5(body).digest()).decode()  # noqa: S324


class S3ObjectStore:
    """S3-compatible object store (Backblaze B2, AWS S3, MinIO)."""

    supports_multipart = True

    def __init__(self, settings: Settings):
        import boto3
        from botocore.config import Config

        self.settings = settings
        self.bucket = settings.s3_bucket
        config = Config(
            signature_version="s3v4",
            s3={"addressing_style": settings.s3_addressing_style},
            retries={"max_attempts": 5, "mode": "standard"},
            connect_timeout=10,
            read_timeout=120,
            max_pool_connections=32,
            # Compute and validate checksums only where the protocol requires them. Services
            # compatible with S3 (B2 included) do not support every mode the AWS SDKs default to.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        )
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=settings.s3_secret_access_key,
            region_name=settings.s3_region,
            config=config,
        )
        for operation in ("DeleteObjects", "PutBucketCors"):
            self.client.meta.events.register(f"before-sign.s3.{operation}", _add_content_md5)

    @contextmanager
    def _translate(self, key: str) -> Iterator[None]:
        from botocore.exceptions import ClientError

        try:
            yield
        except ClientError as exc:
            code, status, message = _error(exc)
            if code in {"NoSuchKey", "NotFound", "404"} or status == 404:
                raise ObjectNotFound(key) from exc
            if "cap exceeded" in message.lower() or code in {
                "download_cap_exceeded",
                "transaction_cap_exceeded",
                "storage_cap_exceeded",
            }:
                raise StorageUnavailable("object storage daily free-tier cap reached") from exc
            raise

    def put_bytes(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        cache_control: str | None = None,
    ) -> None:
        extra = {"CacheControl": cache_control} if cache_control else {}
        with self._translate(key):
            self.client.put_object(
                Bucket=self.bucket, Key=_check_key(key), Body=data, ContentType=content_type, **extra
            )

    def put_file(self, key: str, path: str | Path, content_type: str = "application/octet-stream") -> None:
        from boto3.s3.transfer import TransferConfig

        part = self.settings.s3_multipart_part_bytes
        with self._translate(key):
            self.client.upload_file(
                str(path),
                self.bucket,
                _check_key(key),
                ExtraArgs={"ContentType": content_type},
                Config=TransferConfig(multipart_threshold=4 * part, multipart_chunksize=part),
            )

    def put_stream(self, key: str, stream: BinaryIO, max_bytes: int) -> int:
        with tempfile.NamedTemporaryFile() as tmp:
            total = 0
            while chunk := stream.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise TooLarge()
                tmp.write(chunk)
            tmp.flush()
            self.put_file(key, tmp.name)
        return total

    def get_bytes(self, key: str) -> bytes:
        with self._translate(key):
            return self.client.get_object(Bucket=self.bucket, Key=_check_key(key))["Body"].read()

    def read_range(self, key: str, start: int, length: int) -> tuple[bytes, int]:
        """`length` bytes from `start`, plus the object's total size: one GET instead of HEAD + GET."""
        from botocore.exceptions import ClientError

        try:
            with self._translate(key):
                r = self.client.get_object(
                    Bucket=self.bucket, Key=_check_key(key), Range=f"bytes={start}-{start + length - 1}"
                )
        except ClientError as exc:
            if _error(exc)[0] == "InvalidRange":  # empty object, or start beyond the end
                return b"", self.size(key)
            raise
        data = r["Body"].read()
        content_range = str(r.get("ContentRange") or "")
        if "/" in content_range and not content_range.endswith("/*"):
            total = int(content_range.rsplit("/", 1)[1])
        else:  # the server ignored the range and sent the whole object
            total = len(data)
            data = data[start : start + length]
        return data[:length], total

    def download_to(self, key: str, path: str | Path) -> None:
        """Stream the object to `path` with one GET (a ranged download would spend one
        billable request per chunk); an interrupted transfer resumes from where it stopped."""
        from botocore.exceptions import BotoCoreError

        written = 0
        with open(path, "wb") as fh:
            for attempt in range(4):
                extra = {"Range": f"bytes={written}-"} if written else {}
                try:
                    with self._translate(key):
                        body = self.client.get_object(Bucket=self.bucket, Key=_check_key(key), **extra)[
                            "Body"
                        ]
                        while chunk := body.read(8 * 1024 * 1024):
                            fh.write(chunk)
                            written += len(chunk)
                    return
                except (BotoCoreError, ConnectionError, TimeoutError):
                    if attempt == 3:
                        raise
                    time.sleep(2**attempt)

    def exists(self, key: str) -> bool:
        try:
            self.size(key)
        except ObjectNotFound:
            return False
        return True

    def size(self, key: str) -> int:
        with self._translate(key):
            return int(self.client.head_object(Bucket=self.bucket, Key=_check_key(key))["ContentLength"])

    def list_objects(self, prefix: str, limit: int = 1000) -> list[ObjectInfo]:
        out: list[ObjectInfo] = []
        paginator = self.client.get_paginator("list_objects_v2")
        pages = paginator.paginate(
            Bucket=self.bucket, Prefix=_dir_prefix(prefix), PaginationConfig={"PageSize": min(1000, limit)}
        )
        with self._translate(prefix):
            for page in pages:
                out.extend(ObjectInfo(o["Key"], int(o["Size"])) for o in page.get("Contents", []))
                if len(out) >= limit:
                    return out[:limit]
        return out

    def _delete_batch(self, items: list[dict]) -> None:
        """Delete up to 1000 (Key, VersionId) items; falls back to single deletes if the
        service rejects batch deletes."""
        from botocore.exceptions import ClientError

        if not items:
            return
        failed: list[dict] = items
        try:
            r = self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": items, "Quiet": True})
            failed = [
                {k: e[k] for k in ("Key", "VersionId") if e.get(k)}
                for e in r.get("Errors", [])
                if e.get("Key")
            ]
        except ClientError as exc:
            code, status, _ = _error(exc)
            if status in (401, 403) and code not in {"SignatureDoesNotMatch"}:
                raise
        for item in failed:
            try:
                self.client.delete_object(Bucket=self.bucket, **item)
            except ClientError as exc:
                if _error(exc)[1] != 404:
                    raise

    def purge_prefix(self, prefix: str, deadline: float | None = None) -> PurgeResult:
        """Permanently delete every object under `prefix`, including all stored versions
        (B2 buckets keep old versions; a plain delete would only hide the file).

        Lists from the start after each deleted batch instead of following continuation
        markers: a marker that names a just-deleted version is not reliably honoured by every
        S3-compatible service, and a purge that silently stops early would leave user data.
        """
        from botocore.exceptions import ClientError

        prefix = _dir_prefix(prefix)
        versioned = True
        deleted, previous = 0, None
        while True:
            try:
                with self._translate(prefix):
                    if versioned:
                        page = self.client.list_object_versions(
                            Bucket=self.bucket, Prefix=prefix, MaxKeys=1000
                        )
                    else:
                        page = self.client.list_objects_v2(Bucket=self.bucket, Prefix=prefix, MaxKeys=1000)
            except ClientError as exc:
                if versioned and _error(exc)[0] in {"NotImplemented", "MethodNotAllowed"}:
                    versioned = False  # a store without object versioning
                    continue
                raise
            if versioned:
                versions = page.get("Versions", [])
                items = [
                    {"Key": v["Key"], "VersionId": v["VersionId"]}
                    for v in versions + page.get("DeleteMarkers", [])
                ]
                count = len(versions)
            else:
                items = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                count = len(items)
            if not items:
                return PurgeResult(deleted, True)
            first = (items[0]["Key"], items[0].get("VersionId"))
            if first == previous:
                raise RuntimeError("objects under the prefix could not be deleted")
            previous = first
            with self._translate(prefix):
                self._delete_batch(items)
            deleted += count
            if not page.get("IsTruncated"):
                return PurgeResult(deleted, True)
            if deadline is not None and time.monotonic() > deadline:
                return PurgeResult(deleted, False)

    def delete_prefix(self, prefix: str) -> int:
        return self.purge_prefix(prefix).deleted

    def delete(self, key: str) -> None:
        """Permanently delete one object, all versions included."""
        from botocore.exceptions import ClientError

        key = _check_key(key)
        try:
            with self._translate(key):
                r = self.client.list_object_versions(Bucket=self.bucket, Prefix=key, MaxKeys=1000)
        except ClientError as exc:
            if _error(exc)[0] not in {"NotImplemented", "MethodNotAllowed"}:
                raise
            self._delete_batch([{"Key": key}])
            return
        items = [
            {"Key": v["Key"], "VersionId": v["VersionId"]}
            for v in r.get("Versions", []) + r.get("DeleteMarkers", [])
            if v["Key"] == key
        ]
        self._delete_batch(items)

    def signed_get_url(self, key: str, ttl: int | None = None, filename: str | None = None) -> str:
        params: dict = {"Bucket": self.bucket, "Key": _check_key(key)}
        if filename:
            params["ResponseContentDisposition"] = content_disposition(filename)
        return self.client.generate_presigned_url(
            "get_object", Params=params, ExpiresIn=ttl or self.settings.signed_url_ttl_seconds
        )

    def signed_put(
        self,
        key: str,
        ttl: int | None = None,
        content_type: str = "application/zip",
        content_length: int | None = None,
    ) -> dict:
        """Presigned PUT. With `content_length` the size is part of the signature, so the URL
        cannot be used to store a larger object than the one declared."""
        params: dict = {"Bucket": self.bucket, "Key": _check_key(key), "ContentType": content_type}
        if content_length is not None:
            params["ContentLength"] = content_length
        url = self.client.generate_presigned_url(
            "put_object", Params=params, ExpiresIn=ttl or self.settings.upload_url_ttl_seconds
        )
        return {"url": url, "method": "PUT", "headers": {"Content-Type": content_type}}

    # ---- presigned multipart uploads (large archives go browser -> bucket directly) ----
    def create_multipart_upload(self, key: str, content_type: str = "application/zip") -> str:
        with self._translate(key):
            r = self.client.create_multipart_upload(
                Bucket=self.bucket, Key=_check_key(key), ContentType=content_type
            )
        return str(r["UploadId"])

    def presign_upload_part(
        self,
        key: str,
        upload_id: str,
        part_number: int,
        ttl: int | None = None,
        content_length: int | None = None,
    ) -> str:
        params: dict = {
            "Bucket": self.bucket,
            "Key": _check_key(key),
            "UploadId": upload_id,
            "PartNumber": part_number,
        }
        if content_length is not None:
            params["ContentLength"] = content_length
        return self.client.generate_presigned_url(
            "upload_part", Params=params, ExpiresIn=ttl or self.settings.upload_url_ttl_seconds
        )

    def list_uploaded_parts(self, key: str, upload_id: str) -> list[dict]:
        """Parts the service has received. Completing from this list (instead of ETags reported
        by the browser) means the bucket's CORS rules need not expose the ETag header."""
        parts: list[dict] = []
        pages = self.client.get_paginator("list_parts").paginate(
            Bucket=self.bucket, Key=_check_key(key), UploadId=upload_id
        )
        with self._translate(key):
            for page in pages:
                parts.extend(
                    {"PartNumber": int(p["PartNumber"]), "ETag": p["ETag"], "Size": int(p["Size"])}
                    for p in page.get("Parts", [])
                )
        return sorted(parts, key=lambda p: p["PartNumber"])

    def complete_multipart_upload(self, key: str, upload_id: str, parts: list[dict]) -> None:
        with self._translate(key):
            self.client.complete_multipart_upload(
                Bucket=self.bucket,
                Key=_check_key(key),
                UploadId=upload_id,
                MultipartUpload={
                    "Parts": [{"PartNumber": p["PartNumber"], "ETag": p["ETag"]} for p in parts]
                },
            )

    def abort_multipart_upload(self, key: str, upload_id: str) -> None:
        from botocore.exceptions import ClientError

        try:
            self.client.abort_multipart_upload(Bucket=self.bucket, Key=_check_key(key), UploadId=upload_id)
        except ClientError as exc:
            if _error(exc)[0] not in {"NoSuchUpload", "NoSuchKey"} and _error(exc)[1] != 404:
                raise


# ---------------------------------------------------------------------------
# Worker-side write-through cache
# ---------------------------------------------------------------------------


class CachingObjectStore:
    """Write-through local cache in front of a remote store, for batch workers.

    Everything the worker writes (sample objects, thumbnails, audit artifacts) is also kept on
    local disk, so an audit that follows ingestion on the same runner never downloads what it
    just uploaded, and artifacts read many times per audit are downloaded at most once. The
    cache lives for one worker process; a job on another runner reads from the store again.
    """

    _MAX_CACHED_OBJECT = 256 * 1024**2
    _NEGATIVE_TTL = 600.0

    def __init__(self, inner: ObjectStore, root: str | Path, max_bytes: int):
        self.inner = inner
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self.supports_multipart = inner.supports_multipart
        self._lock = threading.Lock()
        self._lru: OrderedDict[str, int] = OrderedDict()
        self._bytes = 0
        self._missing: dict[str, float] = {}

    # ---- cache bookkeeping ----
    def _path(self, key: str) -> Path:
        p = (self.root / _check_key(key)).resolve()
        if self.root not in p.parents:
            raise ValueError("invalid object key")
        return p

    def _cached(self, key: str) -> Path | None:
        p = self._path(key)
        if p.is_file():
            with self._lock:
                if key in self._lru:
                    self._lru.move_to_end(key)
            return p
        return None

    def _admit(self, key: str, size: int) -> None:
        with self._lock:
            self._missing.pop(key, None)
            self._bytes += size - self._lru.pop(key, 0)
            self._lru[key] = size
            while self._bytes > self.max_bytes and len(self._lru) > 1:
                old, old_size = self._lru.popitem(last=False)
                self._bytes -= old_size
                self._path(old).unlink(missing_ok=True)

    def _remember(self, key: str, data: bytes) -> None:
        if len(data) > self._MAX_CACHED_OBJECT:
            return
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + f".tmp-{uuid.uuid4().hex}")
        tmp.write_bytes(data)
        os.replace(tmp, p)
        self._admit(key, len(data))

    def _remember_file(self, key: str, path: str | Path) -> None:
        size = os.path.getsize(path)
        if size > self.max_bytes // 4:
            return
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + f".tmp-{uuid.uuid4().hex}")
        try:
            os.link(path, tmp)  # same filesystem: no copy
        except OSError:
            shutil.copyfile(path, tmp)
        os.replace(tmp, p)
        self._admit(key, size)

    def cached_path(self, key: str) -> Path | None:
        """Local copy of `key` if this process already has it (no store request)."""
        return self._cached(key)

    def remember(self, key: str, data: bytes) -> None:
        """Add bytes known to equal the stored object (e.g. verified by content hash)."""
        self._remember(key, data)

    def _forget(self, key: str) -> None:
        p = self._path(key)
        with self._lock:
            self._bytes -= self._lru.pop(key, 0)
        if p.is_file():
            p.unlink()

    # ---- writes: store first, then cache ----
    def put_bytes(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        cache_control: str | None = None,
    ) -> None:
        self.inner.put_bytes(key, data, content_type, cache_control)
        self._remember(key, data)

    def put_file(self, key: str, path: str | Path, content_type: str = "application/octet-stream") -> None:
        self._forget(key)
        self.inner.put_file(key, path, content_type)
        self._remember_file(key, path)

    def put_stream(self, key: str, stream: BinaryIO, max_bytes: int) -> int:
        self._forget(key)
        return self.inner.put_stream(key, stream, max_bytes)

    # ---- reads: cache first ----
    def get_bytes(self, key: str) -> bytes:
        if (p := self._cached(key)) is not None:
            return p.read_bytes()
        data = self.inner.get_bytes(key)
        self._remember(key, data)
        return data

    def read_range(self, key: str, start: int, length: int) -> tuple[bytes, int]:
        if (p := self._cached(key)) is not None:
            with open(p, "rb") as fh:
                fh.seek(start)
                return fh.read(length), p.stat().st_size
        return self.inner.read_range(key, start, length)

    def local_copy(self, key: str) -> Path:
        """A local file with the object's bytes, downloaded at most once per worker process."""
        if (p := self._cached(key)) is not None:
            return p
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + f".tmp-{uuid.uuid4().hex}")
        try:
            self.inner.download_to(key, tmp)
            os.replace(tmp, p)
        finally:
            if tmp.exists():
                tmp.unlink()
        self._admit(key, p.stat().st_size)
        return p

    def download_to(self, key: str, path: str | Path) -> None:
        if (p := self._cached(key)) is not None:
            shutil.copyfile(p, path)
            return
        self.inner.download_to(key, path)

    def exists(self, key: str) -> bool:
        if self._cached(key) is not None:
            return True
        with self._lock:
            seen = self._missing.get(key)
        if seen is not None and time.monotonic() - seen < self._NEGATIVE_TTL:
            return False
        found = self.inner.exists(key)
        if not found:
            with self._lock:
                self._missing[key] = time.monotonic()
        return found

    def size(self, key: str) -> int:
        if (p := self._cached(key)) is not None:
            return p.stat().st_size
        return self.inner.size(key)

    def list_objects(self, prefix: str, limit: int = 1000) -> list[ObjectInfo]:
        return self.inner.list_objects(prefix, limit)

    # ---- deletes: store and cache ----
    def delete(self, key: str) -> None:
        self.inner.delete(key)
        self._forget(key)

    def _forget_prefix(self, prefix: str) -> None:
        d = self._path(_dir_prefix(prefix).rstrip("/"))
        with self._lock:
            for k in [k for k in self._lru if k.startswith(_dir_prefix(prefix))]:
                self._bytes -= self._lru.pop(k)
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)

    def delete_prefix(self, prefix: str) -> int:
        self._forget_prefix(prefix)
        return self.inner.delete_prefix(prefix)

    def purge_prefix(self, prefix: str, deadline: float | None = None) -> PurgeResult:
        self._forget_prefix(prefix)
        return self.inner.purge_prefix(prefix, deadline)

    # ---- pass-through ----
    def signed_get_url(self, key: str, ttl: int | None = None, filename: str | None = None) -> str:
        return self.inner.signed_get_url(key, ttl, filename)

    def signed_put(
        self,
        key: str,
        ttl: int | None = None,
        content_type: str = "application/zip",
        content_length: int | None = None,
    ) -> dict:
        return self.inner.signed_put(key, ttl, content_type, content_length)

    def create_multipart_upload(self, key: str, content_type: str = "application/zip") -> str:
        return self.inner.create_multipart_upload(key, content_type)

    def presign_upload_part(
        self,
        key: str,
        upload_id: str,
        part_number: int,
        ttl: int | None = None,
        content_length: int | None = None,
    ) -> str:
        return self.inner.presign_upload_part(key, upload_id, part_number, ttl, content_length)

    def list_uploaded_parts(self, key: str, upload_id: str) -> list[dict]:
        return self.inner.list_uploaded_parts(key, upload_id)

    def complete_multipart_upload(self, key: str, upload_id: str, parts: list[dict]) -> None:
        self.inner.complete_multipart_upload(key, upload_id, parts)

    def abort_multipart_upload(self, key: str, upload_id: str) -> None:
        self.inner.abort_multipart_upload(key, upload_id)


@lru_cache
def get_store() -> ObjectStore:
    settings = get_settings()
    if settings.storage_backend == "s3":
        store = S3ObjectStore(settings)
        if settings.role == "worker":
            root = settings.object_cache_dir or os.path.join(tempfile.gettempdir(), "datacourt-objects")
            return CachingObjectStore(store, root, settings.object_cache_max_bytes)
        return store
    return LocalObjectStore(settings)


def local_copy(store: ObjectStore, key: str) -> Path | None:
    """Local file for `key` when the store can provide one cheaply (local disk or worker cache)."""
    fn = getattr(store, "local_copy", None)
    return fn(key) if fn is not None else None


def media_url(key: str) -> str:
    """Stable, CDN-cacheable URL for an immutable, content-addressed thumbnail.

    The expiry is rounded up to a period boundary, so the URL is identical for every request in
    that period and a CDN in front of `/api/v1/media/` can serve repeat views without touching
    object storage. Valid for between one and two periods.
    """
    settings = get_settings()
    period = settings.media_url_period_seconds
    exp = (int(time.time()) // period + 2) * period
    token = sign_payload({"k": _check_key(key), "m": "MEDIA", "exp": exp}, settings.signed_url_secret)
    return f"/api/v1/media/{token}"


# ---------------------------------------------------------------------------
# Key layout (single source of truth)
# ---------------------------------------------------------------------------


def version_prefix(org_id: uuid.UUID, dataset_id: uuid.UUID, version_id: uuid.UUID) -> str:
    return f"orgs/{org_id}/datasets/{dataset_id}/versions/{version_id}"


def source_zip_key(org_id: uuid.UUID, dataset_id: uuid.UUID, version_id: uuid.UUID) -> str:
    return f"{version_prefix(org_id, dataset_id, version_id)}/source/original.zip"


def sample_object_key(
    org_id: uuid.UUID, dataset_id: uuid.UUID, version_id: uuid.UUID, sha256: str, ext: str
) -> str:
    return f"{version_prefix(org_id, dataset_id, version_id)}/objects/{sha256[:2]}/{sha256}{ext}"


def thumb_key(org_id: uuid.UUID, dataset_id: uuid.UUID, version_id: uuid.UUID, sha256: str) -> str:
    return f"{version_prefix(org_id, dataset_id, version_id)}/thumbs/{sha256[:2]}/{sha256}.webp"


def browser_copy_key(org_id: uuid.UUID, dataset_id: uuid.UUID, version_id: uuid.UUID, sha256: str) -> str:
    """A WebP copy of a sample in a format browsers cannot display (HEIC/HEIF), for viewing only."""
    return f"{version_prefix(org_id, dataset_id, version_id)}/display/{sha256[:2]}/{sha256}.webp"


def browser_copy_key_for(sample_key: str, sha256: str) -> str:
    """`browser_copy_key` of the version a `sample_object_key` belongs to."""
    prefix, sep, _ = sample_key.rpartition("/objects/")
    if not sep or not prefix:
        raise ValueError("not a sample object key")
    return f"{prefix}/display/{sha256[:2]}/{sha256}.webp"


def audit_prefix(org_id: uuid.UUID, audit_id: uuid.UUID) -> str:
    return f"orgs/{org_id}/audits/{audit_id}"


def audit_artifact_key(org_id: uuid.UUID, audit_id: uuid.UUID, name: str) -> str:
    return f"{audit_prefix(org_id, audit_id)}/artifacts/{name}"


def export_prefix(org_id: uuid.UUID, export_id: uuid.UUID) -> str:
    return f"orgs/{org_id}/exports/{export_id}"


def export_key(org_id: uuid.UUID, export_id: uuid.UUID, filename: str) -> str:
    return f"{export_prefix(org_id, export_id)}/{filename}"


def report_prefix(org_id: uuid.UUID, report_id: uuid.UUID) -> str:
    return f"orgs/{org_id}/reports/{report_id}"


def report_key(org_id: uuid.UUID, report_id: uuid.UUID, filename: str) -> str:
    return f"{report_prefix(org_id, report_id)}/{filename}"
