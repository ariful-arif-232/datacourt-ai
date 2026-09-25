"""Ingestion job: immutable source ZIP -> validated samples, hashes, thumbnails, manifest.

Idempotent and resumable: files already recorded for the version are skipped, objects
are content-addressed, and the final renumbering/aggregation steps are recomputed.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import tempfile
import uuid
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.orm import Session

from datacourt import jobs, ledger
from datacourt.config import get_settings
from datacourt.db import models as m
from datacourt.db.session import new_session
from datacourt.enums import AuditProfile, FileStatus, Split, VersionStatus
from datacourt.ingest import images
from datacourt.ingest.layout import detect_layout
from datacourt.ingest.queue import enqueue_ingest  # noqa: F401 - re-exported for callers
from datacourt.ingest.zip_safety import UnsafeArchive, read_entry, scan_archive
from datacourt.logging_setup import log
from datacourt.storage import (
    ObjectNotFound,
    browser_copy_key,
    get_store,
    local_copy,
    sample_object_key,
    thumb_key,
    version_prefix,
)

logger = logging.getLogger("datacourt.ingest")
IMMUTABLE = "private, max-age=31536000, immutable"
CHUNK = 200
MAX_ENTRY_BYTES = 64 * 1024 * 1024


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _load_provenance(
    zf: zipfile.ZipFile, entries: dict[str, zipfile.ZipInfo], root_prefix: str
) -> dict[str, dict]:
    """Optional per-file provenance from `provenance.csv` (columns: path, source, license, collector, ...)."""
    info = entries.get(f"{root_prefix}provenance.csv") or entries.get("provenance.csv")
    if info is None or info.file_size > 20 * 1024 * 1024:
        return {}
    allowed = {"source", "license", "collector", "collection_method", "capture_date", "consent_note"}
    out: dict[str, dict] = {}
    with zf.open(info) as fh:
        reader = csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8", errors="replace"))
        for row in reader:
            path = (row.get("path") or "").strip().lstrip("/")
            if not path:
                continue
            rec = {k: (row.get(k) or "").strip()[:500] for k in allowed if (row.get(k) or "").strip()}
            if rec:
                out[path] = rec
    return out


def run_ingest_job(ctx) -> dict:
    settings = get_settings()
    store = get_store()
    version_id = uuid.UUID(ctx.payload["version_id"])

    with new_session() as s:
        version = s.get(m.DatasetVersion, version_id)
        if version is None:
            raise jobs.PermanentJobError("dataset version not found")
        if version.status == VersionStatus.READY:
            return {"skipped": "already ingested"}
        dataset = s.get(m.Dataset, version.dataset_id)
        assert dataset is not None
        org_id, dataset_id = version.org_id, dataset.id
        src_key = version.source_object_key
        if not src_key:
            raise jobs.PermanentJobError("source archive not uploaded")
        version.status = VersionStatus.INGESTING
        version.error = None
        s.commit()

    ctx.report(0.02, "INGESTING", force=True)
    with tempfile.TemporaryDirectory(prefix="dc-ingest-") as tmpdir:
        try:
            # The worker cache keeps this copy, so an audit that follows on the same runner
            # can read samples from it instead of downloading each object.
            zip_path = local_copy(store, src_key)
            if zip_path is None:
                zip_path = Path(tmpdir) / "source.zip"
                store.download_to(src_key, zip_path)
        except ObjectNotFound as exc:
            raise jobs.PermanentJobError("source archive not uploaded") from exc
        if zip_path.stat().st_size > settings.max_upload_bytes:
            raise jobs.PermanentJobError("archive exceeds the maximum upload size")
        source_sha = _sha256_file(zip_path)
        try:
            zf = zipfile.ZipFile(zip_path)
        except zipfile.BadZipFile as exc:
            raise jobs.PermanentJobError("file is not a valid ZIP archive") from exc
        with zf:
            try:
                entries = scan_archive(
                    zf,
                    max_files=settings.max_files_per_archive,
                    max_total_bytes=settings.max_extracted_bytes,
                    max_entry_bytes=MAX_ENTRY_BYTES,
                )
            except UnsafeArchive as exc:
                raise jobs.PermanentJobError(f"unsafe archive: {exc}") from exc
            ctx.report(0.05, "VALIDATING", force=True)
            candidates = [e for e in entries if e.status == "candidate"]
            layout = detect_layout([e.name for e in candidates])
            if not layout.classes:
                raise jobs.PermanentJobError("no class folders with images were found in the archive")
            by_name = {e.name: e for e in entries}
            provenance = _load_provenance(zf, {e.name: e.info for e in entries}, layout.root_prefix)

            with new_session() as s:
                version = s.get(m.DatasetVersion, version_id)
                assert version is not None
                version.source_sha256 = source_sha
                version.source_bytes = zip_path.stat().st_size
                version.layout = layout.summary()
                class_ids = _ensure_classes(s, version_id, layout.classes)
                _ensure_splits(
                    s, version_id, layout.split_folders, {a.split for a in layout.assignments if a.split}
                )
                existing_paths = set(
                    s.scalars(
                        select(m.DatasetFile.relative_path).where(
                            m.DatasetFile.dataset_version_id == version_id
                        )
                    )
                )
                max_idx = s.scalar(
                    select(func.max(m.Sample.idx)).where(m.Sample.dataset_version_id == version_id)
                )
                s.commit()

            # Record non-image / unassignable / rejected entries.
            skipped_rows = []
            for e in entries:
                if e.status != "candidate" and e.name not in existing_paths:
                    skipped_rows.append(
                        (
                            e.name,
                            e.size,
                            FileStatus.IGNORED if e.status == "ignored" else FileStatus.REJECTED,
                            e.reason,
                        )
                    )
            for a in layout.assignments:
                if a.label is None and a.path not in existing_paths:
                    skipped_rows.append((a.path, by_name[a.path].size, FileStatus.IGNORED, a.reason))
            if skipped_rows:
                with new_session() as s:
                    for path, size, status, reason in skipped_rows:
                        s.add(
                            m.DatasetFile(
                                dataset_version_id=version_id,
                                relative_path=path,
                                byte_size=size,
                                status=status,
                                reason=reason,
                            )
                        )
                    s.commit()

            todo = [a for a in layout.assignments if a.label is not None and a.path not in existing_paths]
            # Objects are content-addressed, so they are written without an existence check (a
            # HEAD per object would cost more than the write); exact duplicates are written once.
            written: set[str] = set()
            total = max(1, len(todo))
            ordinal_base = max(1_000_000, (max_idx or 0) + 1)
            for start in range(0, len(todo), CHUNK):
                ctx.check_cancelled()
                chunk = todo[start : start + CHUNK]
                with new_session() as s:
                    new_samples: list[m.Sample] = []
                    for i, a in enumerate(chunk):
                        entry = by_name[a.path]
                        try:
                            data = read_entry(zf, entry, MAX_ENTRY_BYTES)
                            info = images.inspect_image(data, a.path)
                        except (images.InvalidImage, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
                            reason = (
                                str(exc)[:250] if isinstance(exc, images.InvalidImage) else "unreadable entry"
                            )
                            s.add(
                                m.DatasetFile(
                                    dataset_version_id=version_id,
                                    relative_path=a.path,
                                    byte_size=entry.size,
                                    status=FileStatus.REJECTED,
                                    reason=reason,
                                )
                            )
                            continue
                        # The original bytes, unchanged (HEIC stays HEIC); derived objects follow.
                        okey = sample_object_key(org_id, dataset_id, version_id, info.sha256, info.ext)
                        if okey not in written:
                            store.put_bytes(okey, data, info.content_type, cache_control=IMMUTABLE)
                            written.add(okey)
                        tkey: str | None = thumb_key(org_id, dataset_id, version_id, info.sha256)
                        if tkey not in written:
                            try:
                                store.put_bytes(
                                    tkey,
                                    images.make_thumbnail(data, settings.thumbnail_size, info),
                                    "image/webp",
                                    cache_control=IMMUTABLE,
                                )
                                written.add(tkey)
                            except Exception:  # noqa: BLE001 - thumbnail failure is non-fatal
                                tkey = None
                        if info.format in images.BROWSER_COPY_FORMATS:
                            bkey = browser_copy_key(org_id, dataset_id, version_id, info.sha256)
                            if bkey not in written:
                                try:
                                    store.put_bytes(
                                        bkey,
                                        images.make_browser_copy(data, info),
                                        "image/webp",
                                        cache_control=IMMUTABLE,
                                    )
                                    written.add(bkey)
                                except Exception:  # noqa: BLE001 - viewing falls back to the original
                                    info.attributes.pop("browser_copy", None)
                        info.normalized = None  # the full-size decode is no longer needed
                        # Client-side ids: rows are inserted in one batch per chunk instead of one
                        # round trip per file (the worker may be far from the database).
                        f = m.DatasetFile(
                            id=uuid.uuid4(),
                            dataset_version_id=version_id,
                            relative_path=a.path,
                            byte_size=entry.size,
                            status=FileStatus.ACCEPTED,
                            sha256=info.sha256,
                        )
                        s.add(f)
                        rel_in_root = (
                            a.path[len(layout.root_prefix) :]
                            if a.path.startswith(layout.root_prefix)
                            else a.path
                        )
                        new_samples.append(
                            m.Sample(
                                dataset_version_id=version_id,
                                file_id=f.id,
                                idx=ordinal_base + start + i,
                                relative_path=a.path,
                                split=a.split,
                                class_id=class_ids[a.label],
                                label=a.label,
                                sha256=info.sha256,
                                phash=info.phash,
                                phash_flip=info.phash_flip,
                                dhash=info.dhash,
                                width=info.width,
                                height=info.height,
                                format=info.format,
                                mode=info.mode,
                                byte_size=info.byte_size,
                                storage_key=okey,
                                thumb_key=tkey,
                                attributes=info.attributes,
                                provenance=provenance.get(rel_in_root) or provenance.get(a.path),
                            )
                        )
                    s.flush()  # files first (samples reference them), each table in one batch
                    s.add_all(new_samples)
                    s.commit()
                ctx.report(0.05 + 0.85 * min(1.0, (start + len(chunk)) / total), "INGESTING")

    ctx.report(0.92, "PROFILING", force=True)
    with new_session() as s:
        result = finalize_version(s, version_id)
        version = s.get(m.DatasetVersion, version_id)
        assert version is not None
        created_by = version.created_by
        auto_profile = ctx.payload.get("auto_audit")
        if auto_profile and result["sample_count"] > 0:
            from datacourt.services.audits import create_audit

            create_audit(s, version, AuditProfile(auto_profile), created_by=created_by)
        s.commit()
    log(
        logger,
        logging.INFO,
        "ingest complete",
        samples=result["sample_count"],
        rejected=result["rejected_count"],
    )
    return result


def _ensure_classes(s: Session, version_id: uuid.UUID, names: list[str]) -> dict[str, uuid.UUID]:
    existing = {
        c.name: c
        for c in s.scalars(select(m.DatasetClass).where(m.DatasetClass.dataset_version_id == version_id))
    }
    for i, name in enumerate(sorted(set(names) | set(existing))):
        if name in existing:
            continue
        c = m.DatasetClass(dataset_version_id=version_id, name=name, index=10_000 + i)
        s.add(c)
        existing[name] = c
    s.flush()
    # Deterministic contiguous indices by sorted name.
    ordered = sorted(existing.values(), key=lambda c: c.name)
    for i, c in enumerate(ordered):
        c.index = -(i + 1)
    s.flush()
    for i, c in enumerate(ordered):
        c.index = i
    s.flush()
    return {c.name: c.id for c in ordered}


def _ensure_splits(s: Session, version_id: uuid.UUID, folders: dict[str, str], present: set) -> None:
    existing = set(
        s.scalars(select(m.DatasetSplit.name).where(m.DatasetSplit.dataset_version_id == version_id))
    )
    for sp in present:
        if sp not in existing:
            s.add(m.DatasetSplit(dataset_version_id=version_id, name=sp, source_folder=folders.get(str(sp))))
    s.flush()


def finalize_version(s: Session, version_id: uuid.UUID) -> dict:
    """Renumber sample indices, compute aggregates and the manifest. Safe to re-run."""
    store = get_store()
    version = s.get(m.DatasetVersion, version_id)
    assert version is not None
    dataset = s.get(m.Dataset, version.dataset_id)
    assert dataset is not None
    # Contiguous idx by relative path (two-phase to avoid unique collisions).
    s.execute(update(m.Sample).where(m.Sample.dataset_version_id == version_id).values(idx=-m.Sample.idx - 1))
    s.execute(
        text(
            """
            UPDATE samples AS t SET idx = r.rn - 1
            FROM (SELECT id, row_number() OVER (ORDER BY relative_path) AS rn FROM samples WHERE dataset_version_id = :v) r
            WHERE t.id = r.id
            """
        ),
        {"v": version_id},
    )
    samples = s.scalars(
        select(m.Sample).where(m.Sample.dataset_version_id == version_id).order_by(m.Sample.idx)
    ).all()
    classes = s.scalars(select(m.DatasetClass).where(m.DatasetClass.dataset_version_id == version_id)).all()
    class_counts = Counter(x.class_id for x in samples)
    for c in classes:
        c.sample_count = class_counts.get(c.id, 0)
    # Drop classes that ended up with no valid images.
    empty = [c.id for c in classes if c.sample_count == 0]
    if empty:
        s.execute(delete(m.DatasetClass).where(m.DatasetClass.id.in_(empty)))
    split_counts = Counter(str(x.split) for x in samples)
    for sp in s.scalars(select(m.DatasetSplit).where(m.DatasetSplit.dataset_version_id == version_id)):
        sp.sample_count = split_counts.get(str(sp.name), 0)

    file_status = dict(
        s.execute(
            select(m.DatasetFile.status, func.count())
            .where(m.DatasetFile.dataset_version_id == version_id)
            .group_by(m.DatasetFile.status)
        ).all()
    )
    rejected_reasons = Counter(
        r
        for (r,) in s.execute(
            select(m.DatasetFile.reason).where(
                m.DatasetFile.dataset_version_id == version_id, m.DatasetFile.status == FileStatus.REJECTED
            )
        )
    )
    manifest = {
        "schema": "datacourt-manifest-v1",
        "dataset": dataset.name,
        "version": version.version_number,
        "source_sha256": version.source_sha256,
        "files": [
            {
                "path": x.relative_path,
                "sha256": x.sha256,
                "label": x.label,
                "split": str(x.split),
                "bytes": x.byte_size,
            }
            for x in samples
        ],
    }
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    mkey = f"{version_prefix(version.org_id, dataset.id, version.id)}/manifest.json"
    store.put_bytes(mkey, manifest_bytes, "application/json")

    res_buckets = Counter(_res_bucket(x.width, x.height) for x in samples)
    by_class_split: dict[str, Counter] = {}
    for x in samples:
        by_class_split.setdefault(x.label, Counter())[str(x.split)] += 1
    counts = [c for c in class_counts.values() if c > 0]
    stats = {
        "sample_count": len(samples),
        "class_count": len(counts),
        "splits": dict(split_counts),
        "class_counts": {c.name: c.sample_count for c in classes if c.sample_count},
        "class_split_counts": {k: dict(v) for k, v in by_class_split.items()},
        "imbalance_ratio": (max(counts) / min(counts)) if counts else None,
        "formats": dict(Counter(x.format for x in samples)),
        "modes": dict(Counter(x.mode for x in samples)),
        "resolution_buckets": dict(res_buckets),
        "median_width": _median([x.width for x in samples]),
        "median_height": _median([x.height for x in samples]),
        "total_bytes": sum(x.byte_size for x in samples),
        "files": {str(k): v for k, v in file_status.items()},
        "rejected_reasons": dict(rejected_reasons.most_common(20)),
        "unique_sha256": len({x.sha256 for x in samples}),
        "provenance_coverage": (sum(1 for x in samples if x.provenance) / len(samples)) if samples else 0.0,
    }
    version.stats = stats
    version.manifest_object_key = mkey
    version.manifest_sha256 = manifest_sha
    version.status = VersionStatus.READY if samples else VersionStatus.FAILED
    version.error = None if samples else "no valid images were found"
    version.ingested_at = datetime.now(UTC)
    ledger.record(
        s,
        org_id=version.org_id,
        event_type="dataset_version.ingested",
        entity_type="dataset_version",
        entity_id=version.id,
        dataset_version_id=version.id,
        actor_id=version.created_by,
        payload={
            "manifest_sha256": manifest_sha,
            "source_sha256": version.source_sha256,
            "samples": len(samples),
            "classes": len(counts),
            "hash_version": images.HASH_VERSION,
        },
    )
    s.flush()
    return {
        "sample_count": len(samples),
        "rejected_count": int(file_status.get(FileStatus.REJECTED, 0)),
        "manifest_sha256": manifest_sha,
    }


def _median(values: list[int]) -> float | None:
    if not values:
        return None
    v = sorted(values)
    n = len(v)
    return float(v[n // 2]) if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def _res_bucket(w: int, h: int) -> str:
    side = min(w, h)
    for limit, label in (
        (32, "<32"),
        (64, "32-63"),
        (128, "64-127"),
        (256, "128-255"),
        (512, "256-511"),
        (1024, "512-1023"),
    ):
        if side < limit:
            return label
    return ">=1024"


__all__ = ["Split", "enqueue_ingest", "finalize_version", "run_ingest_job"]
