"""Seed the public read-only demo workspace (`datacourt-demo`) through the real pipeline.

    python -m datacourt.demo [--profile deep] [--seed 7] [--drain]

Idempotent: does nothing if the demo workspace already exists. The data is the synthetic
benchmark (no real people or private data). After the audit completes, a "Demo curator"
account reviews part of the queue using the benchmark's ground truth, so visitors can see
decided, open and rare-protected cases. It then queues a What-if run and an audit report.
Visitors join as read-only viewers (see `bootstrap.join_demo`) and cannot change anything.

Set SEED_DEMO_ON_START=true to run this in the background when the API starts.
"""

from __future__ import annotations

import argparse
import logging
import threading
import time
import uuid

from sqlalchemy import select, text

from datacourt import jobs
from datacourt.benchmark.generator import generate
from datacourt.db import models as m
from datacourt.db.session import new_session
from datacourt.enums import DecisionAction, JobType, RunStatus, VersionStatus
from datacourt.ingest.pipeline import enqueue_ingest
from datacourt.logging_setup import configure_logging, log
from datacourt.security import new_token
from datacourt.services import bootstrap
from datacourt.services.review import ReviewError, record_decision
from datacourt.storage import get_store, source_zip_key

logger = logging.getLogger("datacourt.demo")
_LOCK_KEY = 0x44435F44454D4F  # "DC_DEMO"


def _create(seed: int, profile: str) -> tuple[uuid.UUID, uuid.UUID, dict] | None:
    with new_session() as s:
        if bootstrap.demo_org(s) is not None:
            return None
    data, gt = generate(seed=seed)
    with new_session() as s:
        s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})
        if bootstrap.demo_org(s) is not None:
            return None
        curator = bootstrap.create_user(
            s,
            f"demo-curator-{uuid.uuid4().hex[:6]}@demo.datacourt.local",
            new_token(),
            "Demo curator",
            demo=True,
        )
        org = bootstrap.create_org(s, "DataCourt demo", curator, demo=True)
        project = m.Project(
            org_id=org.id,
            name="Synthetic shapes benchmark",
            description="Read-only demo. A procedurally generated dataset with known, injected problems "
            "(label errors, duplicates, leakage, blur, a colour shortcut, rare valid examples).",
            created_by=curator.id,
        )
        s.add(project)
        s.flush()
        dataset = m.Dataset(
            org_id=org.id,
            project_id=project.id,
            name="synthetic-shapes",
            description="DataCourt Synthetic Shapes benchmark",
            provenance={
                "source": f"DataCourt benchmark generator (seed {seed})",
                "license": "Generated data, no third-party content",
                "collection_method": "procedural rendering",
            },
            created_by=curator.id,
        )
        s.add(dataset)
        s.flush()
        v = bootstrap.create_version(
            s, dataset, filename="synthetic-shapes.zip", created_by=curator.id, notes="Demo upload"
        )
        s.flush()
        key = source_zip_key(org.id, dataset.id, v.id)
        get_store().put_bytes(key, data, "application/zip")
        v.source_object_key, v.source_bytes, v.status = key, len(data), VersionStatus.UPLOADED
        enqueue_ingest(s, v, auto_audit=profile)
        s.commit()
        return v.id, curator.id, gt.__dict__


def _wait_for_audit(version_id: uuid.UUID, timeout: float) -> m.AuditRun | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with new_session() as s:
            a = s.scalar(
                select(m.AuditRun)
                .where(m.AuditRun.dataset_version_id == version_id)
                .order_by(m.AuditRun.created_at.desc())
                .limit(1)
            )
            if a is not None and a.status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED):
                s.expunge(a)
                return a
        time.sleep(3)
    return None


def _review_and_experiment(version_id: uuid.UUID, curator_id: uuid.UUID, gt: dict, review_top: int) -> dict:
    """The curator reviews every other case among the top `review_top` (ground truth) so the demo
    shows both decided and open work, then queues a What-if run and an audit report."""
    leaked = {b for _, b in gt["cross_split_exact"]} | {d["copy"] for d in gt["cross_split_variant"]}
    copies = {b for _, b in gt["exact_duplicates"]}
    leak_originals = {a for a, _ in gt["cross_split_exact"]} | {
        d["original"] for d in gt["cross_split_variant"]
    }
    rare = set(gt["rare_valid"])
    decided = 0
    with new_session() as s:
        audit = s.scalar(
            select(m.AuditRun)
            .where(m.AuditRun.dataset_version_id == version_id, m.AuditRun.status == RunStatus.COMPLETED)
            .order_by(m.AuditRun.created_at.desc())
            .limit(1)
        )
        assert audit is not None
        classes = {
            c.name: c.id
            for c in s.scalars(select(m.DatasetClass).where(m.DatasetClass.dataset_version_id == version_id))
        }
        rows = s.execute(
            select(m.CourtCase, m.Sample.relative_path)
            .join(m.Sample, m.Sample.id == m.CourtCase.sample_id)
            .where(m.CourtCase.audit_run_id == audit.id)
            .order_by(m.CourtCase.priority_score.desc())
            .limit(review_top)
        ).all()
        for i, (case, path) in enumerate(rows):
            if i % 2:
                continue
            if path in gt["label_errors"]:
                action, target = DecisionAction.RELABEL, classes.get(gt["label_errors"][path]["true"])
                note = "Checked against the source image: wrong class folder."
            elif path in leaked:
                action, target, note = DecisionAction.REMOVE, None, "Evaluation copy of a training image."
            elif path in copies:
                action, target, note = DecisionAction.REMOVE, None, "Redundant byte-identical copy."
            elif path in leak_originals:
                action, target = DecisionAction.KEEP, None
                note = "Training original: keep it here; the evaluation copy is removed in its own case."
            elif path in rare:
                action, target, note = DecisionAction.KEEP, None, "Unusual but valid example; keep."
            else:
                action, target, note = DecisionAction.KEEP, None, "Reviewed; no problem found."
            try:
                record_decision(
                    s, case, curator_id, action, target_class_id=target, note=note, adjudicate=False
                )
                decided += 1
            except ReviewError as exc:
                log(logger, logging.WARNING, "demo decision skipped", reason=str(exc))
        run = m.WhatIfRun(
            org_id=audit.org_id,
            dataset_version_id=version_id,
            audit_run_id=audit.id,
            name="Apply reviewed decisions and fix leakage",
            source="demo",
            status=RunStatus.QUEUED,
            config={"seeds": 5},
            created_by=curator_id,
        )
        s.add(run)
        s.flush()
        s.add_all(
            [
                m.WhatIfAction(run_id=run.id, action_type="apply_review_decisions", params={}),
                m.WhatIfAction(run_id=run.id, action_type="move_leakage_out_of_eval", params={}),
            ]
        )
        run.job_id = jobs.enqueue(
            s, JobType.WHAT_IF, {"what_if_run_id": str(run.id)}, org_id=audit.org_id, priority=60
        ).id
        rep = m.AuditReport(
            org_id=audit.org_id, audit_run_id=audit.id, status=RunStatus.QUEUED, created_by=curator_id
        )
        s.add(rep)
        s.flush()
        rep.job_id = jobs.enqueue(
            s, JobType.REPORT, {"report_id": str(rep.id)}, org_id=audit.org_id, priority=70
        ).id
        s.commit()
    return {"decisions": decided}


def seed_demo(
    *, seed: int = 7, profile: str = "deep", drain: bool = False, timeout: float = 3600, review_top: int = 60
) -> dict:
    created = _create(seed, profile)
    if created is None:
        return {"status": "exists"}
    version_id, curator_id, gt = created
    log(logger, logging.INFO, "demo workspace created; waiting for audit", version_id=str(version_id))
    if drain:
        from datacourt.worker import Worker

        Worker("demo-seed").drain()
    audit = _wait_for_audit(version_id, timeout)
    if audit is None or audit.status != RunStatus.COMPLETED:
        return {"status": "audit_not_completed", "version_id": str(version_id)}
    out = _review_and_experiment(version_id, curator_id, gt, review_top)
    if drain:
        from datacourt.worker import Worker

        Worker("demo-seed").drain()
    return {"status": "seeded", "version_id": str(version_id), **out}


def seed_in_background() -> threading.Thread:
    """Used by the API at startup when SEED_DEMO_ON_START=true; failures are logged, never raised."""

    def run() -> None:
        try:
            log(logger, logging.INFO, "demo seed", **seed_demo())
        except Exception:  # noqa: BLE001
            logger.exception("demo seeding failed")

    t = threading.Thread(target=run, name="demo-seed", daemon=True)
    t.start()
    return t


def main() -> None:
    ap = argparse.ArgumentParser(description="Seed the DataCourt demo workspace.")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--profile", default="deep", choices=["fast", "deep"])
    ap.add_argument("--drain", action="store_true", help="process jobs in this process instead of a worker")
    args = ap.parse_args()
    configure_logging("INFO")
    print(seed_demo(seed=args.seed, profile=args.profile, drain=args.drain))


if __name__ == "__main__":
    main()
