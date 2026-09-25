"""Run the controlled benchmark end to end and write results.

    python -m datacourt.benchmark.run --out ../../docs/benchmark [--profile deep] [--seed 7]

Creates an isolated workspace, uploads the generated archive through the normal
ingestion path, runs the audit, then measures every detector against ground truth,
review-budget coverage, ranking-based review effort, and guided-cleanup what-if results.
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path

import numpy as np
from sqlalchemy import select

from datacourt.benchmark.evaluate import evaluate
from datacourt.benchmark.generator import generate
from datacourt.db import models as m
from datacourt.db.session import new_session
from datacourt.enums import Role, RunStatus, VersionStatus
from datacourt.ingest.pipeline import enqueue_ingest
from datacourt.logging_setup import configure_logging
from datacourt.security import hash_password
from datacourt.services.budget import optimize
from datacourt.storage import get_store, source_zip_key
from datacourt.whatif.lab import run_experiment
from datacourt.worker import Worker


def _setup(data: bytes, profile: str) -> uuid.UUID:
    with new_session() as s:
        u = m.User(
            email=f"bench-{uuid.uuid4().hex[:8]}@bench.local",
            name="Benchmark",
            password_hash=hash_password(uuid.uuid4().hex),
        )
        o = m.Organization(name="Benchmark", slug=f"bench-{uuid.uuid4().hex[:8]}")
        s.add_all([u, o])
        s.flush()
        s.add(m.OrganizationMember(org_id=o.id, user_id=u.id, role=Role.OWNER))
        p = m.Project(org_id=o.id, name="Benchmark", created_by=u.id)
        s.add(p)
        s.flush()
        d = m.Dataset(org_id=o.id, project_id=p.id, name="synthetic-shapes", created_by=u.id)
        s.add(d)
        s.flush()
        v = m.DatasetVersion(
            org_id=o.id,
            dataset_id=d.id,
            version_number=1,
            status=VersionStatus.UPLOADED,
            source_filename="synthetic-shapes.zip",
            created_by=u.id,
        )
        s.add(v)
        s.flush()
        key = source_zip_key(o.id, d.id, v.id)
        get_store().put_bytes(key, data, "application/zip")
        v.source_object_key = key
        enqueue_ingest(s, v, auto_audit=profile)
        s.commit()
        return v.id


def _budget_eval(s, audit_id: uuid.UUID, gt: dict) -> dict:
    rows = s.execute(
        select(m.CourtCase, m.Sample.label, m.Sample.split, m.Sample.relative_path)
        .join(m.Sample, m.Sample.id == m.CourtCase.sample_id)
        .where(m.CourtCase.audit_run_id == audit_id)
    ).all()
    cases = [
        {
            "id": str(c.id),
            "case_number": c.case_number,
            "verdict": str(c.verdict),
            "priority": c.priority_score,
            "impact": c.impact_score,
            "strength": c.strength_score,
            "uncertainty": str(c.uncertainty),
            "categories": c.categories or [],
            "status": "open",
            "family_id": str(c.family_id) if c.family_id else None,
            "label": label,
            "split": str(split),
            "est_minutes": c.est_review_minutes,
            "path": path,
        }
        for c, label, split, path in rows
    ]
    issues = set(gt["label_errors"]) | set(gt["quality"])
    issues |= {b for _, b in gt["exact_duplicates"]} | {b for _, b in gt["cross_split_exact"]}
    issues |= {d["copy"] for d in gt["near_duplicates"] + gt["cross_split_variant"]}
    high = (
        set(gt["label_errors"])
        | {b for _, b in gt["cross_split_exact"]}
        | {d["copy"] for d in gt["cross_split_variant"]}
    )
    out = {}
    for k in (25, 50, 100):
        for obj in ("balanced", "label_errors", "leakage"):
            res = optimize([dict(c) for c in cases], max_items=k, max_minutes=None, objective=obj)
            chosen = {c["path"] for c in res["selected"]}
            out[f"k{k}_{obj}"] = {
                "issue_coverage": round(len(chosen & issues) / len(issues), 4),
                "high_impact_coverage": round(len(chosen & high) / len(high), 4),
                "precision": round(len(chosen & issues) / max(1, len(chosen)), 4),
                "minutes": res["estimated_minutes"],
            }
    out["issues_total"] = len(issues)
    out["high_impact_total"] = len(high)
    return out


def _effort(s, audit_id: uuid.UUID, gt: dict, n_samples: int) -> dict:
    """Reviews needed to find 50%/80% of label errors in suspicion order vs random order."""
    rows = s.execute(
        select(m.Sample.relative_path, m.LabelFinding.suspicion_score)
        .join(m.LabelFinding, m.LabelFinding.sample_id == m.Sample.id)
        .where(m.LabelFinding.audit_run_id == audit_id)
        .order_by(m.LabelFinding.suspicion_score.desc())
    ).all()
    errors = set(gt["label_errors"])
    found, needed = 0, {}
    for i, (path, _) in enumerate(rows, start=1):
        found += path in errors
        for target in (0.5, 0.8):
            if target not in needed and found >= target * len(errors):
                needed[target] = i
    out = {}
    for target in (0.5, 0.8):
        random_expected = target * n_samples
        k = needed.get(target)
        out[f"recall_{int(target * 100)}"] = {
            "reviews_with_datacourt_ranking": k,
            "reviews_expected_random_order": round(random_expected),
            "reduction": None if k is None else round(1 - k / random_expected, 4),
        }
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="benchmark-results")
    ap.add_argument("--profile", default="deep", choices=["fast", "deep"])
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    configure_logging("WARNING")
    t0 = time.time()
    data, gt_obj = generate(seed=args.seed)
    gt = gt_obj.__dict__
    version_id = _setup(data, args.profile)
    Worker("benchmark").drain()
    wall = time.time() - t0
    with new_session() as s:
        audit = s.scalar(select(m.AuditRun).where(m.AuditRun.dataset_version_id == version_id))
        if audit is None or audit.status != RunStatus.COMPLETED:
            raise SystemExit(
                f"audit did not complete: {audit.status if audit else 'missing'} {audit.error if audit else ''}"
            )
        results = evaluate(s, audit.id, gt)
        results["review_budget"] = _budget_eval(s, audit.id, gt)
        results["review_effort"] = _effort(s, audit.id, gt, results["samples"])
        top = s.execute(
            select(m.Sample.id, m.Sample.relative_path)
            .join(m.LabelFinding, m.LabelFinding.sample_id == m.Sample.id)
            .where(m.LabelFinding.audit_run_id == audit.id)
            .order_by(m.LabelFinding.suspicion_score.desc())
            .limit(80)
        ).all()
    # Guided cleanup: a simulated reviewer checks the top-80 suspicious samples and fixes only real errors.
    relabel = [
        {"sample_id": str(sid), "target_class": gt["label_errors"][p]["true"]}
        for sid, p in top
        if p in gt["label_errors"]
    ]
    experiments = {
        "guided_review_top80": [
            {"type": "relabel_samples", "items": relabel},
            {"type": "remove_duplicate_copies"},
            {"type": "move_leakage_out_of_eval"},
        ],
        "jury_suggestions_unreviewed": [
            {"type": "apply_jury_suggestions"},
            {"type": "move_leakage_out_of_eval"},
            {"type": "preserve_rare"},
        ],
        "random_removal_control": None,
    }
    rng = np.random.default_rng(args.seed)
    with new_session() as s:
        train_ids = [
            str(x)
            for x in s.scalars(
                select(m.Sample.id).where(
                    m.Sample.dataset_version_id == version_id, m.Sample.split == "train"
                )
            )
        ]
    experiments["random_removal_control"] = [
        {
            "type": "remove_samples",
            "sample_ids": list(rng.choice(train_ids, size=len(relabel), replace=False)),
        }
    ]
    results["what_if"] = {}
    for name, actions in experiments.items():
        out = run_experiment(audit.id, actions, seeds=5)
        sm = out["summary"]
        results["what_if"][name] = {
            "changes": {k: v for k, v in sm["changes"].items() if k != "class_counts_after"},
            "deltas": sm["deltas"],
            "preserved_holdout_ci": sm.get("preserved_holdout_ci"),
            "eval_sets": sm["eval_sets"],
        }
    results["wall_clock_seconds"] = round(wall, 1)
    results["ground_truth_counts"] = gt["counts"]
    results["generator_seed"] = args.seed
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "results.json").write_text(json.dumps(results, indent=1, default=str))
    print(
        json.dumps(
            {k: results[k] for k in ("label_triage", "review_effort", "runtime_seconds")},
            indent=1,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
