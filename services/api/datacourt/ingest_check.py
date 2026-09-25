"""Real ingestion check (`datacourt-ingest-check`).

Runs the worker's ingestion and a FAST audit through the real job queue, in-process (the code a
GitHub Actions worker runs), on a mixed JPG/HEIC dataset, then checks what users rely on:

  * the version becomes ready and every generated HEIC is decoded, rotated photos upright;
  * originals are stored byte for byte, and the uploaded archive is unchanged;
  * every sample has a thumbnail, and every HEIC/HEIF sample a browser-viewable copy;
  * a HEIC and its lossless and JPEG conversions form one duplicate family;
  * a truncated HEIC is rejected with a reason.

    datacourt-ingest-check [--real-dir DIR] [--zip ARCHIVE] [--keep]

The dataset is generated photos stored as JPEG or HEIC (encoded by libheif), plus every HEIC/HEIF
file under --real-dir (a phone's photo export, a public test corpus); or --zip, ingested as it is,
reported as counts only (no file or folder names). The workspace it creates is deleted afterwards
unless --keep. It refuses ENVIRONMENT=production: point DATABASE_URL at a scratch database.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import sys
import time
import uuid
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROTATE_CW = 6  # EXIF orientation: rotate 90° clockwise to display
CLASSES = ("class_a", "class_b")
ANCHOR = "check/train/class_a/IMG_0001.HEIC"  # a rotated HEIC, converted twice below
EXPORT_PNG = "check/val/class_a/IMG_0001 export.png"
EXPORT_JPG = "check/train/class_a/IMG_0001 converted.jpg"
TRUNCATED = "check/train/class_b/IMG_0999.HEIC"


@dataclass
class CheckSet:
    archive: bytes
    heic: list[str] = field(default_factory=list)  # generated HEIC entries
    rotated: list[str] = field(default_factory=list)
    real: dict[str, str] = field(default_factory=dict)  # entry -> path under --real-dir


@dataclass
class Result:
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)

    def check(self, name: str, ok: Any, detail: str = "") -> None:
        self.checks.append((name, bool(ok), detail))

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(ok for _, ok, _ in self.checks)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _photo(seed: int, w: int = 320, h: int = 240) -> Any:
    import cv2
    import numpy as np

    rng = np.random.default_rng(seed)
    base = cv2.GaussianBlur(rng.normal(128, 50, (h, w, 3)).astype(np.float32), (0, 0), 3)
    cv2.circle(base, (w // 3 + seed % 17, h // 2), h // 4, (30.0, 70.0, 210.0), -1)
    cv2.rectangle(base, (w // 2, h // 5), (w - 20, h // 2), (220.0, 200.0, 40.0), -1)
    return np.clip(base, 0, 255).astype(np.uint8)


def _encode(rgb: Any, fmt: str, orientation: int | None = None) -> bytes:
    from PIL import Image

    kw: dict[str, Any] = {"quality": 90} if fmt in ("HEIF", "JPEG") else {}
    if orientation:
        exif = Image.Exif()
        exif[0x0112] = orientation
        kw["exif"] = exif.tobytes()
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, format=fmt, **kw)
    return buf.getvalue()


def build_dataset(real_dir: Path | None = None) -> CheckSet:
    from datacourt.ingest import images  # registers the HEIF decoder
    from datacourt.ingest.layout import HEIF_EXTENSIONS

    files: dict[str, bytes] = {}
    ds = CheckSet(b"")
    for k, cls in enumerate(CLASSES):
        for i in range(14):
            split, n = ("train", i) if i < 10 else ("val", 40 + i)
            rgb = _photo(100 * k + i)
            if i % 2:
                name = f"check/{split}/{cls}/IMG_{k}{n:03d}.HEIC"
                rotate = i % 4 == 1
                files[name] = _encode(rgb, "HEIF", ROTATE_CW if rotate else None)
                ds.heic.append(name)
                if rotate:
                    ds.rotated.append(name)
            else:
                files[f"check/{split}/{cls}/IMG_{k}{n:03d}.JPG"] = _encode(rgb, "JPEG")
    decoded = images.decode_rgb(files[ANCHOR])
    files[EXPORT_PNG] = _encode(decoded, "PNG")  # lossless: the same pixels, another format
    files[EXPORT_JPG] = _encode(decoded, "JPEG")  # lossy: a near duplicate
    whole = _encode(_photo(999, 640, 480), "HEIF")
    files[TRUNCATED] = whole[: len(whole) // 2]
    if real_dir is not None:
        found = sorted(p for p in real_dir.rglob("*") if p.is_file() and p.suffix.lower() in HEIF_EXTENSIONS)
        for n, path in enumerate(found):
            rel = path.relative_to(real_dir).as_posix()
            entry = f"check/train/{CLASSES[n % 2]}/real_{rel.replace('/', '__')}"
            files[entry] = path.read_bytes()
            ds.real[entry] = rel
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    ds.archive = buf.getvalue()
    return ds


def _create_version(archive: bytes) -> tuple[uuid.UUID, uuid.UUID]:
    from datacourt.db import models as m
    from datacourt.db.session import new_session
    from datacourt.enums import VersionStatus
    from datacourt.ingest.queue import enqueue_ingest
    from datacourt.security import new_token
    from datacourt.services import bootstrap
    from datacourt.storage import get_store, source_zip_key

    with new_session() as s:
        owner = bootstrap.create_user(
            s, f"ingest-check-{uuid.uuid4().hex[:10]}@check.datacourt.local", new_token(), "Ingestion check"
        )
        org = bootstrap.create_org(s, "DataCourt ingestion check", owner)
        project = m.Project(org_id=org.id, name="Ingestion check", created_by=owner.id)
        s.add(project)
        s.flush()
        dataset = m.Dataset(org_id=org.id, project_id=project.id, name="mixed-jpg-heic", created_by=owner.id)
        s.add(dataset)
        s.flush()
        v = bootstrap.create_version(s, dataset, filename="ingest-check.zip", created_by=owner.id)
        key = source_zip_key(org.id, dataset.id, v.id)
        get_store().put_bytes(key, archive, "application/zip")
        v.source_object_key, v.source_bytes, v.status = key, len(archive), VersionStatus.UPLOADED
        enqueue_ingest(s, v, auto_audit="fast")
        s.commit()
        return owner.id, v.id


def _cleanup(owner_id: uuid.UUID) -> None:
    from datacourt.db import models as m
    from datacourt.db.session import new_session
    from datacourt.services import bootstrap, purge

    with new_session() as s:
        user = s.get(m.User, owner_id)
        if user is None:
            return
        out = bootstrap.delete_account(s, user)
        s.commit()
    purge.run_inline(list(out.get("purge_jobs") or []), budget_seconds=120)


def _seconds(start: Any, end: Any) -> float:
    return (end - start).total_seconds() if start and end else 0.0


def verify(version_id: uuid.UUID, ds: CheckSet, private: bool, seconds: float) -> Result:  # noqa: C901
    from sqlalchemy import select

    from datacourt.db import models as m
    from datacourt.db.session import new_session
    from datacourt.enums import FileStatus, RunStatus, VersionStatus
    from datacourt.storage import browser_copy_key_for, get_store

    store = get_store()
    r = Result()
    with zipfile.ZipFile(io.BytesIO(ds.archive)) as zf:
        entries = {i.filename: _sha(zf.read(i)) for i in zf.infolist() if not i.is_dir()}
    with new_session() as s:
        v = s.get(m.DatasetVersion, version_id)
        assert v is not None
        samples = s.scalars(select(m.Sample).where(m.Sample.dataset_version_id == version_id)).all()
        files = s.scalars(select(m.DatasetFile).where(m.DatasetFile.dataset_version_id == version_id)).all()
        audit = s.scalar(
            select(m.AuditRun)
            .where(m.AuditRun.dataset_version_id == version_id)
            .order_by(m.AuditRun.created_at.desc())
            .limit(1)
        )
        families: list[tuple[str, set[str]]] = []
        if audit is not None:
            fams = s.scalars(
                select(m.DuplicateFamily).where(m.DuplicateFamily.audit_run_id == audit.id)
            ).all()
            paths = {x.id: x.relative_path for x in samples}
            for f in fams:
                members = s.scalars(
                    select(m.DuplicateFamilyMember.sample_id).where(m.DuplicateFamilyMember.family_id == f.id)
                ).all()
                families.append((str(f.kind), {paths[x] for x in members if x in paths}))
        ingest = s.scalar(
            select(m.Job)
            .where(m.Job.payload["version_id"].as_string() == str(version_id))
            .order_by(m.Job.created_at.desc())
            .limit(1)
        )
        ingest_s = _seconds(ingest.started_at, ingest.finished_at) if ingest else 0.0
        audit_s = _seconds(audit.started_at, audit.finished_at) if audit else 0.0
        status, error = v.status, v.error
        source_key, source_sha = v.source_object_key, v.source_sha256
        audit_status = audit.status if audit else None

    rejected = [f for f in files if f.status == FileStatus.REJECTED]
    ignored = [f for f in files if f.status == FileStatus.IGNORED]
    heif = [x for x in samples if x.format in ("HEIC", "HEIF")]
    r.lines.append(
        f"{len(entries)} files -> {len(samples)} samples, {len(rejected)} rejected, {len(ignored)} ignored "
        f"(ingestion {ingest_s:.1f}s, FAST audit {audit_s:.1f}s, total {seconds:.1f}s)"
    )
    r.lines.append(
        "Formats: " + ", ".join(f"{k} {n}" for k, n in Counter(x.format for x in samples).most_common())
    )
    if heif:
        details = [(x.attributes or {}).get("heif", {}) for x in heif]
        r.lines.append(
            "HEIC/HEIF: bit depth "
            + ", ".join(
                f"{k}: {n}" for k, n in sorted(Counter(d.get("bit_depth") for d in details).items(), key=str)
            )
            + f"; rotated by the file {sum(1 for d in details if (d.get('exif_orientation') or 1) != 1)}"
            + "; colour profiles "
            + ", ".join(
                f"{k}: {n}"
                for k, n in Counter(d.get("color_profile") or "none" for d in details).most_common()
            )
        )
        per_image = ingest_s / max(1, len(samples))
        r.lines.append(
            f"Ingestion time per image: {per_image * 1000:.0f} ms (decode, hashes, thumbnail, copies)"
        )
    if rejected:
        reasons = Counter((f.reason or "").split(":")[0] for f in rejected)
        r.lines.append("Rejected: " + "; ".join(f"{k} ({n})" for k, n in reasons.most_common()))

    r.check("version ready", status == VersionStatus.READY, error or "")
    r.check("FAST audit completed", audit_status == RunStatus.COMPLETED, str(audit_status))
    stored = store.get_bytes(source_key) if source_key else b""
    r.check("uploaded archive unchanged", _sha(stored) == _sha(ds.archive) == source_sha)
    kept = sum(
        1 for x in samples if _sha(store.get_bytes(x.storage_key)) == entries.get(x.relative_path) == x.sha256
    )
    r.check("originals stored byte for byte", samples and kept == len(samples), f"{kept}/{len(samples)}")
    thumbs = sum(1 for x in samples if x.thumb_key and store.exists(x.thumb_key))
    r.check("thumbnail for every sample", thumbs == len(samples), f"{thumbs}/{len(samples)}")
    copies = sum(
        1
        for x in heif
        if (x.attributes or {}).get("browser_copy")
        and store.exists(browser_copy_key_for(x.storage_key, x.sha256))
    )
    r.check(
        "browser-viewable copy for every HEIC/HEIF sample",
        heif and copies == len(heif),
        f"{copies}/{len(heif)}",
    )
    if private:
        return r

    by_path = {x.relative_path: x for x in samples}
    decoded = [n for n in ds.heic if n in by_path and by_path[n].format == "HEIC"]
    r.check("every generated HEIC decoded", len(decoded) == len(ds.heic), f"{len(decoded)}/{len(ds.heic)}")
    upright = [n for n in ds.rotated if n in by_path and (by_path[n].width, by_path[n].height) == (240, 320)]
    r.check(
        "rotated HEIC photos stored upright",
        len(upright) == len(ds.rotated),
        f"{len(upright)}/{len(ds.rotated)}",
    )
    cut = next((f for f in rejected if f.relative_path == TRUNCATED), None)
    r.check(
        "truncated HEIC rejected with a reason",
        cut is not None and cut.reason,
        (cut.reason or "") if cut else "",
    )
    family = next((fam for fam in families if ANCHOR in fam[1]), None)
    r.check(
        "HEIC, lossless PNG and JPEG conversions in one duplicate family",
        family is not None and {EXPORT_PNG, EXPORT_JPG} <= family[1],
        f"kind {family[0]}" if family else "no family",
    )
    for entry, rel in ds.real.items():
        x = by_path.get(entry)
        if x is not None:
            h = (x.attributes or {}).get("heif", {})
            r.lines.append(
                f"  accepted  {x.format} {x.width}x{x.height} {h.get('bit_depth', '?')}-bit "
                f"{h.get('color_profile') or '-'} {(x.attributes or {}).get('camera_model') or '-'}  {rel}"
            )
        else:
            rec = next((f for f in files if f.relative_path == entry), None)
            r.lines.append(f"  rejected  {(rec.reason if rec else 'not recorded') or ''}  {rel}")
    return r


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="datacourt-ingest-check", description=__doc__.split("\n\n")[0])
    ap.add_argument("--real-dir", type=Path, help="also ingest every HEIC/HEIF file under this folder")
    ap.add_argument("--zip", type=Path, help="ingest this dataset archive instead (counts-only output)")
    ap.add_argument("--keep", action="store_true", help="keep the workspace for inspection")
    args = ap.parse_args(argv)

    from datacourt.config import get_settings
    from datacourt.logging_setup import configure_logging
    from datacourt.worker import Worker

    settings = get_settings()
    configure_logging(public=settings.public_logs)  # as the worker: public CI logs stay safe
    if settings.environment == "production":
        print("error: refusing to run with ENVIRONMENT=production; use a scratch database", file=sys.stderr)
        return 2
    settings.execution_backend = "none"  # every job runs here, in this process
    ds = CheckSet(args.zip.read_bytes()) if args.zip else build_dataset(args.real_dir)
    owner_id, version_id = _create_version(ds.archive)
    t0 = time.monotonic()
    try:
        Worker("ingest-check").drain(max_seconds=3 * 3600)
        result = verify(version_id, ds, private=bool(args.zip), seconds=time.monotonic() - t0)
    finally:
        if not args.keep:
            _cleanup(owner_id)
    print("Ingestion check" + (f" (workspace kept: version {version_id})" if args.keep else ""))
    for line in result.lines:
        print(line if line.startswith("  ") else f"  {line}")
    for name, ok, detail in result.checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{' - ' + detail if detail else ''}")
    print("All checks passed." if result.ok else "Some checks FAILED.")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(
                "### Ingestion check\n\n" + "\n".join(f"- {line.strip()}" for line in result.lines) + "\n\n"
            )
            fh.write("| check | result |\n|---|---|\n")
            for name, ok, detail in result.checks:
                fh.write(f"| {name} | {'pass' if ok else 'FAIL'}{': ' + detail if detail else ''} |\n")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
