"""Clean dataset export (`export-v1`).

Builds a *new* archive from the original objects plus approved human decisions — the
source version is never modified. Only final decisions (consensus or adjudication) are
applied: REMOVE excludes a sample, RELABEL moves it to the target class folder. Every
other sample is copied byte-for-byte. The archive contains the manifest, action log,
removed-samples list, relabel mapping and SHA-256 checksums, and is registered as the
next dataset version (`origin=export`) so Version Intelligence can compare them.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select

from datacourt import algorithms, jobs, ledger
from datacourt.db import models as m
from datacourt.db.session import new_session
from datacourt.enums import DecisionAction, RunStatus, VersionStatus
from datacourt.ingest.pipeline import enqueue_ingest
from datacourt.sample_objects import SampleObjectReader
from datacourt.services.review import final_decision
from datacourt.storage import export_key, get_store, source_zip_key

VERSION = algorithms.EXPORT


def _safe_name(name: str) -> str:
    keep = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in name.strip().lower())
    return "-".join(p for p in keep.split("-") if p)[:80] or "dataset"


def run_export_job(ctx) -> dict:
    export_id = uuid.UUID(ctx.payload["export_id"])
    store = get_store()
    reader: SampleObjectReader | None = None
    with new_session() as s:
        ex = s.get(m.Export, export_id)
        if ex is None:
            raise jobs.PermanentJobError("export not found")
        ex.status = RunStatus.RUNNING
        version = s.get(m.DatasetVersion, ex.dataset_version_id)
        assert version is not None
        reader = SampleObjectReader(version.id, store, version.source_object_key)
        dataset = s.get(m.Dataset, version.dataset_id)
        assert dataset is not None
        classes = {
            c.id: c.name
            for c in s.scalars(select(m.DatasetClass).where(m.DatasetClass.dataset_version_id == version.id))
        }
        samples = s.scalars(
            select(m.Sample).where(m.Sample.dataset_version_id == version.id).order_by(m.Sample.idx)
        ).all()
        decisions: dict[uuid.UUID, tuple[m.CourtCase, m.HumanDecision]] = {}
        if ex.audit_run_id:
            for case in s.scalars(select(m.CourtCase).where(m.CourtCase.audit_run_id == ex.audit_run_id)):
                d = final_decision(s, case.id)
                if d is not None and d.action in (
                    DecisionAction.REMOVE,
                    DecisionAction.RELABEL,
                    DecisionAction.KEEP,
                ):
                    decisions[case.sample_id] = (case, d)
        next_number = (
            s.scalar(
                select(func.max(m.DatasetVersion.version_number)).where(
                    m.DatasetVersion.dataset_id == dataset.id
                )
            )
            or 0
        ) + 1
        s.commit()
        org_id, dataset_id, dataset_name = version.org_id, dataset.id, dataset.name
        src_version_number, created_by = version.version_number, ex.created_by
    base = f"{_safe_name(dataset_name)}-datacourt-v{next_number}"
    removed, relabels, log_rows, manifest = [], [], [], []
    checks: list[str] = []
    applied_cases: list[str] = []
    with tempfile.TemporaryDirectory(prefix="dc-export-") as tmp:
        zpath = Path(tmp) / f"{base}.zip"
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
            for n, x in enumerate(samples):
                ctx.check_cancelled()
                label = x.label
                action = "keep"
                pair = decisions.get(x.id)
                if pair is not None:
                    case, d = pair
                    applied_cases.append(str(case.id))
                    if d.action == DecisionAction.REMOVE:
                        removed.append(
                            {
                                "path": x.relative_path,
                                "sha256": x.sha256,
                                "label": x.label,
                                "split": str(x.split),
                                "case_number": case.case_number,
                                "decision_id": str(d.id),
                                "note": d.note,
                            }
                        )
                        log_rows.append(
                            {
                                "case_number": case.case_number,
                                "action": "remove",
                                "path": x.relative_path,
                                "reviewer_id": str(d.reviewer_id),
                                "decided_at": d.created_at.isoformat(),
                                "adjudicated": d.is_adjudication,
                            }
                        )
                        continue
                    if d.action == DecisionAction.RELABEL and d.target_class_id in classes:
                        label = classes[d.target_class_id]
                        action = "relabel"
                        relabels.append(
                            {
                                "path": x.relative_path,
                                "sha256": x.sha256,
                                "from": x.label,
                                "to": label,
                                "case_number": case.case_number,
                                "decision_id": str(d.id),
                            }
                        )
                    log_rows.append(
                        {
                            "case_number": case.case_number,
                            "action": action,
                            "path": x.relative_path,
                            "reviewer_id": str(d.reviewer_id),
                            "decided_at": d.created_at.isoformat(),
                            "adjudicated": d.is_adjudication,
                        }
                    )
                assert reader is not None
                data = reader.read(x.storage_key, x.relative_path, x.sha256)
                sha = hashlib.sha256(data).hexdigest()
                if sha != x.sha256:
                    raise jobs.PermanentJobError(f"integrity check failed for {x.relative_path}")
                fname = x.relative_path.rsplit("/", 1)[-1]
                split_dir = "" if str(x.split) == "unsplit" else f"{x.split}/"
                arc = f"{base}/{split_dir}{label}/{fname}"
                if arc in {m_["path"] for m_ in manifest[-50:]}:  # rare name collision after relabel
                    arc = f"{base}/{split_dir}{label}/{x.sha256[:8]}-{fname}"
                zf.writestr(arc, data)
                manifest.append(
                    {
                        "path": arc,
                        "original_path": x.relative_path,
                        "sha256": sha,
                        "label": label,
                        "original_label": x.label,
                        "split": str(x.split),
                        "action": action,
                    }
                )
                checks.append(f"{sha}  {arc}")
                if n % 200 == 0:
                    ctx.report(0.9 * n / max(1, len(samples)), "EXPORT")
            meta = {
                "schema": "datacourt-export-v1",
                "dataset": dataset_name,
                "source_version": src_version_number,
                "export_version": next_number,
                "created_at": datetime.now(UTC).isoformat(),
                "algorithm": VERSION,
                "counts": {"kept": len(manifest), "removed": len(removed), "relabelled": len(relabels)},
                "note": "Built from the immutable source version plus final human decisions only.",
            }
            zf.writestr(
                f"{base}/datacourt/manifest.json", json.dumps({"meta": meta, "files": manifest}, indent=1)
            )
            zf.writestr(f"{base}/datacourt/action_log.json", json.dumps(log_rows, indent=1))
            zf.writestr(
                f"{base}/datacourt/removed_samples.csv",
                _csv(removed, ["path", "sha256", "label", "split", "case_number", "decision_id", "note"]),
            )
            zf.writestr(
                f"{base}/datacourt/relabel_mapping.csv",
                _csv(relabels, ["path", "sha256", "from", "to", "case_number", "decision_id"]),
            )
            zf.writestr(f"{base}/datacourt/SHA256SUMS", "\n".join(checks) + "\n")
            zf.writestr(
                f"{base}/datacourt/README.txt",
                f"{dataset_name} — DataCourt export v{next_number} (from v{src_version_number}).\n"
                "Only final human decisions were applied. See action_log.json for provenance.\n",
            )
        h = hashlib.sha256()
        with open(zpath, "rb") as fh:
            while block := fh.read(8 * 1024 * 1024):
                h.update(block)
        digest = h.hexdigest()
        key = export_key(org_id, export_id, f"{base}.zip")
        store.put_file(key, zpath, "application/zip")
        size = zpath.stat().st_size
        with new_session() as s:
            ex = s.get(m.Export, export_id)
            assert ex is not None
            new_version = m.DatasetVersion(
                org_id=org_id,
                dataset_id=dataset_id,
                version_number=next_number,
                parent_version_id=ex.dataset_version_id,
                origin="export",
                status=VersionStatus.UPLOADED,
                source_filename=f"{base}.zip",
                created_by=created_by,
                notes=f"DataCourt export of v{src_version_number}: {len(removed)} removed, {len(relabels)} relabelled.",
            )
            s.add(new_version)
            s.flush()
            skey = source_zip_key(org_id, dataset_id, new_version.id)
            store.put_file(skey, zpath, "application/zip")
            new_version.source_object_key = skey
            new_version.source_bytes = size
            enqueue_ingest(s, new_version, auto_audit=ctx.payload.get("auto_audit", "fast"))
            ex.status = RunStatus.COMPLETED
            ex.object_key = key
            ex.byte_size = size
            ex.sha256 = digest
            ex.name = f"{base}.zip"
            ex.new_version_id = new_version.id
            ex.finished_at = datetime.now(UTC)
            ex.summary = {
                **meta["counts"],
                "applied_case_ids": applied_cases,
                "new_version_number": next_number,
            }
            ledger.record(
                s,
                org_id=org_id,
                event_type="export.completed",
                entity_type="export",
                entity_id=export_id,
                actor_id=created_by,
                dataset_version_id=ex.dataset_version_id,
                payload={"sha256": digest, "counts": meta["counts"], "new_version_id": str(new_version.id)},
            )
            s.commit()
    return {"export_id": str(export_id), "sha256": digest}


def _csv(rows: list[dict], cols: list[str]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue()
