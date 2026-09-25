"""Audit execution context: sample table, artifacts, image access."""

from __future__ import annotations

import io
import json
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from sqlalchemy import select

from datacourt.config import get_settings
from datacourt.db import models as m
from datacourt.db.session import new_session
from datacourt.ingest.hashes import hex_to_uint64
from datacourt.sample_objects import SampleObjectReader
from datacourt.storage import ObjectNotFound, audit_artifact_key, get_store


class _ArtifactMemo:
    """Small in-process cache of artifact bytes for API processes.

    Case pages read the same audit artifacts (neighbour lists, counterfactual bases) over and
    over; a warm serverless instance can reuse them instead of downloading them again. Artifacts
    of a finished audit do not change; entries still expire so a re-run is picked up.
    """

    def __init__(self, max_bytes: int = 64 * 1024**2, ttl: float = 600.0, missing_ttl: float = 60.0):
        self.max_bytes, self.ttl, self.missing_ttl = max_bytes, ttl, missing_ttl
        self._items: OrderedDict[str, tuple[float, bytes | None]] = OrderedDict()
        self._size = 0
        self._lock = threading.Lock()

    def get(self, key: str) -> tuple[bool, bytes | None]:
        with self._lock:
            hit = self._items.get(key)
            if hit is None:
                return False, None
            at, data = hit
            if time.monotonic() - at > (self.ttl if data is not None else self.missing_ttl):
                self._drop(key)
                return False, None
            self._items.move_to_end(key)
            return True, data

    def put(self, key: str, data: bytes | None) -> None:
        if data is not None and len(data) > self.max_bytes // 4:
            return
        with self._lock:
            self._drop(key)
            self._items[key] = (time.monotonic(), data)
            self._size += len(data or b"")
            while self._size > self.max_bytes and self._items:
                self._drop(next(iter(self._items)))

    def _drop(self, key: str) -> None:
        old = self._items.pop(key, None)
        if old is not None:
            self._size -= len(old[1] or b"")


_memo = _ArtifactMemo()


def _read_artifact(key: str) -> bytes | None:
    """Artifact bytes, or None if absent. API processes memoise; workers have a disk cache."""
    memo = get_settings().role == "api"
    if memo:
        hit, data = _memo.get(key)
        if hit:
            return data
    try:
        data = get_store().get_bytes(key)
    except ObjectNotFound:
        data = None
    if memo:
        _memo.put(key, data)
    return data


@dataclass
class SampleTable:
    ids: list[uuid.UUID]
    idx: np.ndarray
    split: list[str]
    labels: np.ndarray  # class index
    class_names: list[str]
    class_ids: list[uuid.UUID]
    sha: list[str]
    phash: np.ndarray
    phash_flip: np.ndarray
    width: np.ndarray
    height: np.ndarray
    meta: list[dict]
    storage_keys: list[str]
    paths: list[str]

    @property
    def n(self) -> int:
        return len(self.ids)

    @property
    def n_classes(self) -> int:
        return len(self.class_names)

    def mask(self, *splits: str) -> np.ndarray:
        return np.isin(np.array(self.split), list(splits))


def load_samples(version_id: uuid.UUID) -> SampleTable:
    with new_session() as s:
        classes = s.scalars(
            select(m.DatasetClass)
            .where(m.DatasetClass.dataset_version_id == version_id)
            .order_by(m.DatasetClass.index)
        ).all()
        cidx = {c.id: i for i, c in enumerate(classes)}
        rows = s.scalars(
            select(m.Sample).where(m.Sample.dataset_version_id == version_id).order_by(m.Sample.idx)
        ).all()
        return SampleTable(
            ids=[r.id for r in rows],
            idx=np.array([r.idx for r in rows]),
            split=[str(r.split) for r in rows],
            labels=np.array([cidx[r.class_id] for r in rows], dtype=np.int64),
            class_names=[c.name for c in classes],
            class_ids=[c.id for c in classes],
            sha=[r.sha256 for r in rows],
            phash=hex_to_uint64([r.phash for r in rows]),
            phash_flip=hex_to_uint64([r.phash_flip for r in rows]),
            width=np.array([r.width for r in rows]),
            height=np.array([r.height for r in rows]),
            meta=[{"format": r.format, "mode": r.mode, "attributes": r.attributes or {}} for r in rows],
            storage_keys=[r.storage_key for r in rows],
            paths=[r.relative_path for r in rows],
        )


@dataclass
class AuditContext:
    audit_id: uuid.UUID
    org_id: uuid.UUID
    version_id: uuid.UUID
    dataset_id: uuid.UUID
    profile: str
    config: dict
    samples: SampleTable
    report: Any = None  # callable(progress_within_stage: float)
    warnings: list[str] = field(default_factory=list)
    _reader: SampleObjectReader | None = None

    @property
    def deep(self) -> bool:
        return self.profile == "deep"

    # ---- artifacts -------------------------------------------------------
    def _key(self, name: str) -> str:
        return audit_artifact_key(self.org_id, self.audit_id, name)

    def save_npz(self, name: str, **arrays: np.ndarray) -> None:
        buf = io.BytesIO()
        np.savez_compressed(buf, **arrays)
        self._write(f"{name}.npz", buf.getvalue(), "application/octet-stream")

    def _write(self, name: str, data: bytes, content_type: str) -> None:
        key = self._key(name)
        get_store().put_bytes(key, data, content_type)
        if get_settings().role == "api":  # embedded worker (development): keep the memo coherent
            _memo.put(key, data)

    def _read(self, name: str) -> bytes:
        data = _read_artifact(self._key(name))
        if data is None:
            raise ObjectNotFound(f"audit artifact {name} is missing")
        return data

    def load_npz(self, name: str) -> dict[str, np.ndarray]:
        with np.load(io.BytesIO(self._read(f"{name}.npz")), allow_pickle=False) as z:
            return {k: z[k] for k in z.files}

    def has_artifact(self, name: str, ext: str = "npz") -> bool:
        key = self._key(f"{name}.{ext}")
        if get_settings().role == "api":
            # One GET (memoised) answers both "exists?" and the load that usually follows.
            return _read_artifact(key) is not None
        return get_store().exists(key)

    def save_json(self, name: str, obj: Any) -> None:
        self._write(f"{name}.json", json.dumps(obj, default=_json_default).encode(), "application/json")

    def load_json(self, name: str) -> Any:
        return json.loads(self._read(f"{name}.json"))

    # ---- images ----------------------------------------------------------
    def image_bytes(self, i: int) -> bytes:
        if self._reader is None:
            self._reader = SampleObjectReader(self.version_id)
        S = self.samples
        return self._reader.read(S.storage_keys[i], S.paths[i], S.sha[i])

    def image(self, i: int, max_side: int = 512) -> np.ndarray:
        from datacourt.ingest.images import decode_rgb  # OpenCV/Pillow: worker-only

        return decode_rgb(self.image_bytes(i), max_side=max_side)

    def map_images(
        self,
        fn,
        indices: list[int],
        max_side: int = 512,
        chunk: int = 256,
        progress_base: float = 0.0,
        progress_span: float = 1.0,
    ) -> list[Any]:
        """Applies fn(i, rgb) over images with a thread pool, reporting progress."""
        workers = int(self.config["profiling"]["workers"])
        out: list[Any] = [None] * len(indices)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for s in range(0, len(indices), chunk):
                part = indices[s : s + chunk]
                res = list(pool.map(lambda i: fn(i, self.image(i, max_side)), part))
                out[s : s + len(part)] = res
                if self.report:
                    self.report(
                        progress_base + progress_span * min(1.0, (s + len(part)) / max(1, len(indices)))
                    )
        return out


def _json_default(o: Any) -> Any:
    if isinstance(o, np.generic):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, uuid.UUID):
        return str(o)
    raise TypeError(type(o))
