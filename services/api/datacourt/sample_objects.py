"""Read the original bytes of a version's samples with as few object-store downloads as possible.

Order of preference:
1. the local copy (local storage backend, or the worker cache that ingestion filled on this runner);
2. the version's source ZIP, downloaded once per worker process: one request instead of one per
   image, which matters under per-request download quotas (Backblaze B2's free tier);
3. one GET per object, e.g. after the retention policy removed the source archive.

Bytes read from the archive are verified against the sample's SHA-256 before use.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import uuid
import zipfile
from pathlib import Path

from sqlalchemy import select

from datacourt.db import models as m
from datacourt.db.session import new_session
from datacourt.storage import LocalObjectStore, ObjectNotFound, ObjectStore, get_store

logger = logging.getLogger("datacourt.objects")

MAX_ENTRY_BYTES = 64 * 1024 * 1024


class SampleObjectReader:
    def __init__(
        self, version_id: uuid.UUID, store: ObjectStore | None = None, source_key: str | None = None
    ):
        self.version_id = version_id
        self.store = store or get_store()
        self._source_key = source_key
        self._lock = threading.Lock()
        self._local = threading.local()
        self._zip_path: Path | None = None
        self._zip_state = "unknown"  # unknown | ready | unavailable

    def _archive(self) -> zipfile.ZipFile | None:
        if self._zip_state == "unknown":
            with self._lock:
                if self._zip_state == "unknown":
                    self._zip_path = self._fetch_archive()
                    self._zip_state = "ready" if self._zip_path else "unavailable"
        if self._zip_state != "ready":
            return None
        zf = getattr(self._local, "zf", None)
        if zf is None:  # one handle per thread: ZipFile reads are not thread-safe
            zf = zipfile.ZipFile(str(self._zip_path))
            self._local.zf = zf
        return zf

    def _fetch_archive(self) -> Path | None:
        local_copy = getattr(self.store, "local_copy", None)
        if local_copy is None:
            return None
        key = self._source_key
        if key is None:
            with new_session() as s:
                key = s.scalar(
                    select(m.DatasetVersion.source_object_key).where(m.DatasetVersion.id == self.version_id)
                )
        if not key:
            return None
        try:
            path = local_copy(key)
            zipfile.ZipFile(str(path)).close()
            return Path(path)
        except (ObjectNotFound, zipfile.BadZipFile, OSError):
            logger.info("source archive unavailable; reading objects individually")
            return None

    def _from_archive(self, relative_path: str, sha256: str) -> bytes | None:
        zf = self._archive()
        if zf is None:
            return None
        try:
            info = zf.getinfo(relative_path)
            if info.file_size > MAX_ENTRY_BYTES:
                return None
            with zf.open(info) as fh:
                data = fh.read(MAX_ENTRY_BYTES + 1)
        except (KeyError, zipfile.BadZipFile, RuntimeError, OSError, ValueError):
            return None
        if len(data) > MAX_ENTRY_BYTES or hashlib.sha256(data).hexdigest() != sha256:
            return None
        return data

    def read(self, storage_key: str, relative_path: str | None = None, sha256: str | None = None) -> bytes:
        store = self.store
        if isinstance(store, LocalObjectStore):
            return store.get_bytes(storage_key)
        cached_path = getattr(store, "cached_path", None)
        if cached_path is not None and (p := cached_path(storage_key)) is not None:
            return p.read_bytes()
        if relative_path and sha256:
            data = self._from_archive(relative_path, sha256)
            if data is not None:
                remember = getattr(store, "remember", None)
                if remember is not None:
                    remember(storage_key, data)
                return data
        return store.get_bytes(storage_key)

    def close(self) -> None:
        zf = getattr(self._local, "zf", None)
        if zf is not None:
            zf.close()
            self._local.zf = None
