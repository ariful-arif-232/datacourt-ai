"""Human review: decisions, undo, reviewer consensus, adjudication and agreement metrics."""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from datacourt import ledger
from datacourt.db import models as m
from datacourt.enums import CaseStatus, DecisionAction

DESTRUCTIVE = {DecisionAction.RELABEL, DecisionAction.REMOVE}


class ReviewError(ValueError):
    pass


def active_decisions(s: Session, case_id: uuid.UUID) -> list[m.HumanDecision]:
    """Latest non-undone decision per reviewer (adjudications included)."""
    rows = s.scalars(
        select(m.HumanDecision)
        .where(m.HumanDecision.case_id == case_id, m.HumanDecision.undone_at.is_(None))
        .order_by(m.HumanDecision.created_at)
    ).all()
    latest: dict[uuid.UUID, m.HumanDecision] = {}
    for r in rows:
        latest[r.reviewer_id] = r
    return sorted(latest.values(), key=lambda r: r.created_at)


def _decision_key(d: m.HumanDecision) -> tuple:
    return (str(d.action), str(d.target_class_id) if d.action == DecisionAction.RELABEL else None)


def final_decision(s: Session, case_id: uuid.UUID) -> m.HumanDecision | None:
    """The decision that export should apply: latest adjudication, else a consensus, else None."""
    acts = active_decisions(s, case_id)
    adj = [d for d in acts if d.is_adjudication]
    if adj:
        return adj[-1]
    decisive = [d for d in acts if d.action not in (DecisionAction.UNSURE, DecisionAction.ESCALATE)]
    if not decisive:
        return None
    keys = {_decision_key(d) for d in decisive}
    return decisive[-1] if len(keys) == 1 else None


def recompute_status(s: Session, case: m.CourtCase) -> CaseStatus:
    acts = active_decisions(s, case.id)
    if not acts:
        status = CaseStatus.OPEN
    elif any(d.is_adjudication for d in acts):
        status = CaseStatus.RESOLVED
    else:
        decisive = [d for d in acts if d.action not in (DecisionAction.UNSURE, DecisionAction.ESCALATE)]
        keys = {_decision_key(d) for d in decisive}
        if len(keys) > 1 or any(d.action == DecisionAction.ESCALATE for d in acts):
            status = CaseStatus.DISPUTED
        elif decisive:
            status = CaseStatus.DECIDED
        else:
            status = CaseStatus.OPEN
    case.status = status
    case.updated_at = datetime.now(UTC)
    return status


def evidence_snapshot(s: Session, case: m.CourtCase) -> dict[str, Any]:
    verdict = s.scalar(
        select(m.CaseVerdict)
        .where(m.CaseVerdict.case_id == case.id)
        .order_by(m.CaseVerdict.created_at.desc())
        .limit(1)
    )
    sample = s.get(m.Sample, case.sample_id)
    assert sample is not None
    return {
        "case_number": case.case_number,
        "verdict": str(case.verdict),
        "reason_codes": case.reason_codes,
        "jury_version": verdict.jury_version if verdict else None,
        "config_hash": verdict.config_hash if verdict else None,
        "sample_sha256": sample.sha256,
        "label_at_decision": sample.label,
        "split": str(sample.split),
        "priority": case.priority_score,
    }


def record_decision(
    s: Session,
    case: m.CourtCase,
    reviewer_id: uuid.UUID,
    action: DecisionAction,
    *,
    target_class_id: uuid.UUID | None,
    note: str,
    adjudicate: bool,
) -> m.HumanDecision:
    sample = s.get(m.Sample, case.sample_id)
    assert sample is not None
    if action == DecisionAction.RELABEL:
        if target_class_id is None:
            raise ReviewError("relabel requires a target class")
        cls = s.get(m.DatasetClass, target_class_id)
        if cls is None or cls.dataset_version_id != case.dataset_version_id:
            raise ReviewError("target class does not belong to this dataset version")
        if cls.id == sample.class_id:
            raise ReviewError("target class equals the current label")
    elif target_class_id is not None:
        raise ReviewError("target class is only valid for relabel")
    if _export_locked(s, case):
        raise ReviewError(
            "this case was already applied in an export; start a new review on the exported version"
        )
    prev = s.scalar(
        select(m.HumanDecision)
        .where(
            m.HumanDecision.case_id == case.id,
            m.HumanDecision.reviewer_id == reviewer_id,
            m.HumanDecision.undone_at.is_(None),
        )
        .order_by(m.HumanDecision.created_at.desc())
        .limit(1)
    )
    d = m.HumanDecision(
        case_id=case.id,
        dataset_version_id=case.dataset_version_id,
        reviewer_id=reviewer_id,
        action=action,
        target_class_id=target_class_id,
        note=note[:4000],
        evidence_snapshot=evidence_snapshot(s, case),
        previous_decision_id=prev.id if prev else None,
        is_adjudication=adjudicate,
    )
    s.add(d)
    s.flush()
    status = recompute_status(s, case)
    ledger.record(
        s,
        org_id=case.org_id,
        event_type="case.adjudicated" if adjudicate else "case.decision",
        entity_type="court_case",
        entity_id=case.id,
        actor_id=reviewer_id,
        dataset_version_id=case.dataset_version_id,
        payload={
            "decision_id": str(d.id),
            "action": str(action),
            "target_class_id": str(target_class_id) if target_class_id else None,
            "case_status": str(status),
            "evidence": d.evidence_snapshot,
            "note_present": bool(note),
        },
    )
    return d


def undo_decision(s: Session, decision: m.HumanDecision, user_id: uuid.UUID) -> None:
    case = s.get(m.CourtCase, decision.case_id)
    assert case is not None
    if decision.undone_at is not None:
        raise ReviewError("decision already undone")
    if _export_locked(s, case):
        raise ReviewError("decision already applied in an export and can no longer be undone")
    decision.undone_at = datetime.now(UTC)
    decision.undone_by = user_id
    s.flush()  # the status query below must see the undo
    status = recompute_status(s, case)
    ledger.record(
        s,
        org_id=case.org_id,
        event_type="case.decision_undone",
        entity_type="court_case",
        entity_id=case.id,
        actor_id=user_id,
        dataset_version_id=case.dataset_version_id,
        payload={"decision_id": str(decision.id), "case_status": str(status)},
    )


def _export_locked(s: Session, case: m.CourtCase) -> bool:
    locked: Sequence[dict | None] = s.scalars(
        select(m.Export.summary).where(
            m.Export.dataset_version_id == case.dataset_version_id, m.Export.status == "completed"
        )
    ).all()
    return any(str(case.id) in (sm or {}).get("applied_case_ids", []) for sm in locked)


def agreement_stats(s: Session, audit_id: uuid.UUID) -> dict[str, Any]:
    """Pairwise percent agreement and Cohen's kappa over cases reviewed by 2+ reviewers."""
    rows = s.execute(
        select(
            m.HumanDecision.case_id,
            m.HumanDecision.reviewer_id,
            m.HumanDecision.action,
            m.HumanDecision.target_class_id,
            m.HumanDecision.created_at,
            m.HumanDecision.is_adjudication,
        )
        .join(m.CourtCase, m.CourtCase.id == m.HumanDecision.case_id)
        .where(m.CourtCase.audit_run_id == audit_id, m.HumanDecision.undone_at.is_(None))
        .order_by(m.HumanDecision.created_at)
    ).all()
    per_case: dict[uuid.UUID, dict[uuid.UUID, str]] = defaultdict(dict)
    reviewers: set[uuid.UUID] = set()
    for r in rows:
        if r.is_adjudication:
            continue
        label = f"{r.action}:{r.target_class_id}" if str(r.action) == "relabel" else str(r.action)
        per_case[r.case_id][r.reviewer_id] = label
        reviewers.add(r.reviewer_id)
    pairs: list[tuple[str, str]] = []
    for votes in per_case.values():
        v = list(votes.values())
        for a in range(len(v)):
            for b in range(a + 1, len(v)):
                pairs.append((v[a], v[b]))
    result: dict[str, Any] = {
        "reviewers": len(reviewers),
        "cases_with_multiple_reviews": sum(1 for v in per_case.values() if len(v) > 1),
        "comparisons": len(pairs),
    }
    if pairs:
        agree = sum(1 for a, b in pairs if a == b) / len(pairs)
        cats = sorted({x for p in pairs for x in p})
        pa = {c: sum((a == c) + (b == c) for a, b in pairs) / (2 * len(pairs)) for c in cats}
        pe = sum(v * v for v in pa.values())
        kappa = (agree - pe) / (1 - pe) if pe < 1 else 1.0
        result.update(
            {
                "percent_agreement": round(agree, 4),
                "cohens_kappa": round(kappa, 4),
                "kappa_note": "Pooled Cohen's kappa over all reviewer pairs on the same case.",
            }
        )
    return result
