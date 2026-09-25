"""Version Intelligence: what changed between two dataset versions (`version-diff-v1`)."""

from __future__ import annotations

import uuid
from collections import Counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from datacourt import algorithms, jobs
from datacourt.db import models as m
from datacourt.db.session import new_session
from datacourt.enums import RunStatus
from datacourt.ml import dna as dna_mod

VERSION = algorithms.VERSION_DIFF


def latest_completed_audit(s: Session, version_id: uuid.UUID) -> m.AuditRun | None:
    return s.scalar(
        select(m.AuditRun)
        .where(m.AuditRun.dataset_version_id == version_id, m.AuditRun.status == RunStatus.COMPLETED)
        .order_by(m.AuditRun.finished_at.desc())
        .limit(1)
    )


def _sample_map(s: Session, version_id: uuid.UUID) -> dict[str, list[tuple[str, str, str]]]:
    out: dict[str, list[tuple[str, str, str]]] = {}
    for sha, label, split, path in s.execute(
        select(m.Sample.sha256, m.Sample.label, m.Sample.split, m.Sample.relative_path).where(
            m.Sample.dataset_version_id == version_id
        )
    ):
        out.setdefault(sha, []).append((label, str(split), path))
    return out


def _leak_signatures(s: Session, audit: m.AuditRun | None) -> set[frozenset]:
    if audit is None:
        return set()
    sha = dict(
        s.execute(
            select(m.Sample.id, m.Sample.sha256).where(
                m.Sample.dataset_version_id == audit.dataset_version_id
            )
        ).all()
    )
    return {
        frozenset(sha[uuid.UUID(x)] for x in lk.sample_ids)
        for lk in s.scalars(select(m.LeakageFinding).where(m.LeakageFinding.audit_run_id == audit.id))
        if str(lk.risk) != "low"
    }


def _label_issue_shas(s: Session, audit: m.AuditRun | None) -> set[str]:
    if audit is None:
        return set()
    return set(
        s.scalars(
            select(m.Sample.sha256)
            .join(m.LabelFinding, m.LabelFinding.sample_id == m.Sample.id)
            .where(m.LabelFinding.audit_run_id == audit.id, m.LabelFinding.recommended_action == "REVIEW")
        )
    )


def _dup_signatures(s: Session, audit: m.AuditRun | None) -> set[frozenset]:
    if audit is None:
        return set()
    sha = dict(
        s.execute(
            select(m.Sample.id, m.Sample.sha256).where(
                m.Sample.dataset_version_id == audit.dataset_version_id
            )
        ).all()
    )
    fams: dict[uuid.UUID, set[str]] = {}
    for fid, sid in s.execute(
        select(m.DuplicateFamilyMember.family_id, m.DuplicateFamilyMember.sample_id)
        .join(m.DuplicateFamily)
        .where(m.DuplicateFamily.audit_run_id == audit.id)
    ):
        fams.setdefault(fid, set()).add(sha[sid])
    return {frozenset(v) for v in fams.values()}


def _shortcuts(s: Session, audit: m.AuditRun | None) -> set[tuple]:
    if audit is None:
        return set()
    names = {
        c.id: c.name
        for c in s.scalars(
            select(m.DatasetClass).where(m.DatasetClass.dataset_version_id == audit.dataset_version_id)
        )
    }
    return {
        (r.cue, r.cue_value, names.get(r.class_id))
        for r in s.scalars(select(m.ShortcutFinding).where(m.ShortcutFinding.audit_run_id == audit.id))
        if r.strength_label in ("moderate", "strong")
    }


def compute_diff(s: Session, from_id: uuid.UUID, to_id: uuid.UUID) -> dict[str, Any]:
    a_v, b_v = s.get(m.DatasetVersion, from_id), s.get(m.DatasetVersion, to_id)
    assert a_v is not None and b_v is not None
    A, B = _sample_map(s, from_id), _sample_map(s, to_id)
    added = sorted(set(B) - set(A))
    removed = sorted(set(A) - set(B))
    common = set(A) & set(B)
    label_changes, split_changes = [], []
    for sha in sorted(common):
        la, lb = {x[0] for x in A[sha]}, {x[0] for x in B[sha]}
        if la != lb:
            label_changes.append({"sha256": sha, "from": sorted(la), "to": sorted(lb), "path": B[sha][0][2]})
        sa, sb = {x[1] for x in A[sha]}, {x[1] for x in B[sha]}
        if sa != sb:
            split_changes.append({"sha256": sha, "from": sorted(sa), "to": sorted(sb), "path": B[sha][0][2]})
    classes_a = {x[0] for v in A.values() for x in v}
    classes_b = {x[0] for v in B.values() for x in v}
    aa, ab = latest_completed_audit(s, from_id), latest_completed_audit(s, to_id)
    leak_a, leak_b = _leak_signatures(s, aa), _leak_signatures(s, ab)
    lab_a, lab_b = _label_issue_shas(s, aa), _label_issue_shas(s, ab)
    dup_a, dup_b = _dup_signatures(s, aa), _dup_signatures(s, ab)
    sc_a, sc_b = _shortcuts(s, aa), _shortcuts(s, ab)

    def debt(audit: m.AuditRun | None) -> dict | None:
        if audit is None:
            return None
        snap = s.scalar(
            select(m.DatasetDebtSnapshot)
            .where(m.DatasetDebtSnapshot.audit_run_id == audit.id)
            .order_by(m.DatasetDebtSnapshot.created_at.desc())
            .limit(1)
        )
        return (
            {
                "overall": str(snap.overall),
                "levels": {k: v["level"] for k, v in snap.dimensions["dimensions"].items()},
            }
            if snap
            else None
        )

    def metrics(audit: m.AuditRun | None) -> dict | None:
        if audit is None:
            return None
        b = (audit.summary or {}).get("baseline", {})
        return {"cross_validation": b.get("cross_validation"), "eval": (b.get("eval") or {}).get("all_eval")}

    drift = None
    if aa is not None and ab is not None:
        da = s.scalar(select(m.DatasetDnaProfile).where(m.DatasetDnaProfile.audit_run_id == aa.id))
        db = s.scalar(select(m.DatasetDnaProfile).where(m.DatasetDnaProfile.audit_run_id == ab.id))
        if da and db:
            drift = dna_mod.compare(
                da.profile, db.profile, label_a=f"v{a_v.version_number}", label_b=f"v{b_v.version_number}"
            )
            drift["fingerprints"] = {"from": da.fingerprint, "to": db.fingerprint}
    count_a, count_b = sum(len(v) for v in A.values()), sum(len(v) for v in B.values())
    class_counts = lambda M: dict(Counter(x[0] for v in M.values() for x in v))  # noqa: E731
    return {
        "algorithm": VERSION,
        "from": {
            "id": str(from_id),
            "version": a_v.version_number,
            "samples": count_a,
            "audit_id": str(aa.id) if aa else None,
        },
        "to": {
            "id": str(to_id),
            "version": b_v.version_number,
            "samples": count_b,
            "audit_id": str(ab.id) if ab else None,
        },
        "files": {
            "added": len(added),
            "removed": len(removed),
            "unchanged": len(common),
            "added_examples": [B[x][0][2] for x in added[:20]],
            "removed_examples": [A[x][0][2] for x in removed[:20]],
        },
        "labels_changed": {"count": len(label_changes), "examples": label_changes[:50]},
        "splits_changed": {"count": len(split_changes), "examples": split_changes[:50]},
        "classes": {
            "added": sorted(classes_b - classes_a),
            "removed": sorted(classes_a - classes_b),
            "counts_from": class_counts(A),
            "counts_to": class_counts(B),
        },
        "duplicates": {"new_families": len(dup_b - dup_a), "resolved_families": len(dup_a - dup_b)},
        "leakage": {
            "fixed": len(leak_a - leak_b),
            "new": len(leak_b - leak_a),
            "persisting": len(leak_a & leak_b),
        },
        "label_issues": {
            "resolved": len(lab_a - lab_b),
            "new": len(lab_b - lab_a),
            "persisting": len(lab_a & lab_b),
        },
        "shortcuts": {
            "new": sorted(list(x) for x in sc_b - sc_a),
            "resolved": sorted(list(x) for x in sc_a - sc_b),
        },
        "debt": {"from": debt(aa), "to": debt(ab)},
        "model_metrics": {
            "from": metrics(aa),
            "to": metrics(ab),
            "note": "Metrics are measured on each version's own evaluation split; if the split changed they are not directly comparable.",
        },
        "drift": drift,
        "audits_available": {"from": aa is not None, "to": ab is not None},
    }


def run_version_diff_job(ctx) -> dict:
    a, b = uuid.UUID(ctx.payload["from_version_id"]), uuid.UUID(ctx.payload["to_version_id"])
    with new_session() as s:
        if s.get(m.DatasetVersion, a) is None or s.get(m.DatasetVersion, b) is None:
            raise jobs.PermanentJobError("version not found")
        diff = compute_diff(s, a, b)
        row = s.scalar(
            select(m.DatasetVersionDiff).where(
                m.DatasetVersionDiff.from_version_id == a, m.DatasetVersionDiff.to_version_id == b
            )
        )
        if row is None:
            s.add(m.DatasetVersionDiff(from_version_id=a, to_version_id=b, diff=diff))
        else:
            row.diff = diff
        s.commit()
    return {"from": str(a), "to": str(b)}
