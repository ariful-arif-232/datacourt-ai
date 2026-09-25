"""Model-to-Data Blame Map: the evidence chain from an evaluation failure to training data.

Internally this is "likely contributing training evidence". Every link type is labeled by
how it was measured (nearest neighbour in embedding space, TracIn estimate, duplicate
relation), and nothing here asserts causality.
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from datacourt.db import models as m


def blame_map(s: Session, failure: m.FailureEvent, sample_view) -> dict[str, Any]:
    audit_id = failure.audit_run_id
    classes = {
        c.id: c.name
        for c in s.scalars(
            select(m.DatasetClass)
            .join(m.Sample, m.Sample.class_id == m.DatasetClass.id)
            .where(m.Sample.id == failure.sample_id)
        )
    }
    sample = s.get(m.Sample, failure.sample_id)
    assert sample is not None
    all_classes = {
        c.id: c.name
        for c in s.scalars(
            select(m.DatasetClass).where(m.DatasetClass.dataset_version_id == sample.dataset_version_id)
        )
    }
    classes.update(all_classes)
    pred = s.scalar(
        select(m.ModelPrediction).where(
            m.ModelPrediction.audit_run_id == audit_id, m.ModelPrediction.sample_id == failure.sample_id
        )
    )
    links = s.scalars(
        select(m.FailureDataLink)
        .where(m.FailureDataLink.failure_event_id == failure.id)
        .order_by(m.FailureDataLink.link_type, m.FailureDataLink.rank)
    ).all()
    train_ids = list({lk.train_sample_id for lk in links})
    samples = (
        {x.id: x for x in s.scalars(select(m.Sample).where(m.Sample.id.in_(train_ids)))} if train_ids else {}
    )
    label_f = (
        {
            x.sample_id: x
            for x in s.scalars(
                select(m.LabelFinding).where(
                    m.LabelFinding.audit_run_id == audit_id, m.LabelFinding.sample_id.in_(train_ids)
                )
            )
        }
        if train_ids
        else {}
    )
    cases = {
        x.sample_id: x
        for x in s.scalars(
            select(m.CourtCase).where(
                m.CourtCase.audit_run_id == audit_id,
                m.CourtCase.sample_id.in_(train_ids + [failure.sample_id]),
            )
        )
    }
    shortcut_members: dict[str, list[str]] = defaultdict(list)
    for sc in s.scalars(select(m.ShortcutFinding).where(m.ShortcutFinding.audit_run_id == audit_id)):
        if sc.strength_label in ("moderate", "strong"):
            for sid in sc.affected_sample_ids:
                shortcut_members[sid].append(f"{sc.cue}={sc.cue_value}")
    fam_members = s.execute(
        select(m.DuplicateFamilyMember.family_id, m.DuplicateFamilyMember.sample_id)
        .join(m.DuplicateFamily, m.DuplicateFamily.id == m.DuplicateFamilyMember.family_id)
        .where(m.DuplicateFamily.audit_run_id == audit_id)
    ).all()
    fam_of = {sid: fid for fid, sid in fam_members}
    leak = [
        lk
        for lk in s.scalars(select(m.LeakageFinding).where(m.LeakageFinding.audit_run_id == audit_id))
        if str(failure.sample_id) in lk.sample_ids
    ]

    def item(lk: m.FailureDataLink) -> dict:
        x = samples[lk.train_sample_id]
        lf = label_f.get(x.id)
        case = cases.get(x.id)
        return {
            "sample": sample_view(x),
            "link_type": str(lk.link_type),
            "score": lk.score,
            "rank": lk.rank,
            "label": x.label,
            "matches_failure_prediction": x.class_id == failure.predicted_class_id,
            "matches_failure_truth": x.class_id == failure.actual_class_id,
            "label_suspicion": lf.suspicion_score if lf else None,
            "label_action": str(lf.recommended_action) if lf else None,
            "case": {"id": str(case.id), "number": case.case_number, "verdict": str(case.verdict)}
            if case
            else None,
            "shortcut_cues": shortcut_members.get(str(x.id), []),
            "duplicate_family": str(fam_of[x.id]) if x.id in fam_of else None,
        }

    groups: dict[str, list[dict]] = defaultdict(list)
    for lk in links:
        if lk.train_sample_id in samples:
            groups[str(lk.link_type)].append(item(lk))
    nn = groups.get("nearest_neighbor", [])
    harmful = groups.get("harmful_influence", [])
    contributing = {i["sample"]["id"]: i for i in nn + harmful}
    conflicting = [i for i in contributing.values() if i["matches_failure_prediction"]]
    suspicious = [
        i
        for i in contributing.values()
        if i["label_action"] in ("REVIEW", "LOW_PRIORITY_REVIEW")
        or (i["case"] and i["case"]["verdict"] in ("POSSIBLE_RELABEL", "STRONG_REVIEW"))
    ]
    shortcut_heavy = [i for i in contributing.values() if i["shortcut_cues"]]
    dup_families = {i["duplicate_family"] for i in contributing.values() if i["duplicate_family"]}
    chain = [
        {
            "step": "failure",
            "title": f"{str(failure.split).upper()} sample predicted '{classes.get(failure.predicted_class_id)}' "
            f"but labelled '{classes.get(failure.actual_class_id)}'",
            "kind": "model_prediction",
            "count": 1,
        },
        {
            "step": "nearest_training",
            "title": f"{len(nn)} nearest training examples; "
            f"{sum(1 for i in nn if i['matches_failure_prediction'])} carry the predicted label",
            "kind": "measured",
            "count": len(nn),
        },
    ]
    if harmful:
        chain.append(
            {
                "step": "influence",
                "title": f"{len(harmful)} training samples with the largest harmful TracIn estimate",
                "kind": "model_estimate",
                "count": len(harmful),
            }
        )
    chain.append(
        {
            "step": "suspicious_labels",
            "title": f"{len(suspicious)} of the contributing samples have suspicious labels",
            "kind": "heuristic",
            "count": len(suspicious),
        }
    )
    chain.append(
        {
            "step": "duplicates",
            "title": f"{len(dup_families)} duplicate families among contributing samples"
            + (f"; the failure itself is in {len(leak)} leakage finding(s)" if leak else ""),
            "kind": "measured",
            "count": len(dup_families) + len(leak),
        }
    )
    chain.append(
        {
            "step": "shortcuts",
            "title": f"{len(shortcut_heavy)} contributing samples share a flagged shortcut cue",
            "kind": "heuristic",
            "count": len(shortcut_heavy),
        }
    )
    proposed = []
    for i in suspicious:
        if i["case"]:
            proposed.append(
                {
                    "action": "review_case",
                    "case_id": i["case"]["id"],
                    "case_number": i["case"]["number"],
                    "sample_id": i["sample"]["id"],
                    "why": "suspicious label among contributing training samples",
                }
            )
    for i in harmful[:5]:
        if not i["case"]:
            proposed.append(
                {
                    "action": "inspect_sample",
                    "sample_id": i["sample"]["id"],
                    "why": "high harmful influence estimate on this failure",
                }
            )
    return {
        "failure": {
            "id": str(failure.id),
            "split": failure.split,
            "sample": sample_view(sample),
            "actual": classes.get(failure.actual_class_id),
            "predicted": classes.get(failure.predicted_class_id),
            "confidence": failure.confidence,
            "top_probs": pred.top_probs if pred else [],
            "case": {
                "id": str(cases[failure.sample_id].id),
                "number": cases[failure.sample_id].case_number,
                "verdict": str(cases[failure.sample_id].verdict),
            }
            if failure.sample_id in cases
            else None,
            "leakage": [
                {"risk": str(lk.risk), "kind": str(lk.kind), "splits": lk.splits_crossed} for lk in leak
            ],
        },
        "chain": chain,
        "links": dict(groups),
        "summary": {
            "nearest_label_distribution": dict(Counter(i["label"] for i in nn)),
            "conflicting_labels": len(conflicting),
            "suspicious_labels": len(suspicious),
            "shortcut_samples": len(shortcut_heavy),
            "duplicate_families": len(dup_families),
            "influence_available": bool(harmful) or bool(groups.get("helpful_influence")),
        },
        "proposed_actions": proposed,
        "disclaimer": "Links are likely contributing evidence (similarity, TracIn estimates, duplicate relations). "
        "They are not proof of causation; use Failure Replay to test proposed changes experimentally.",
    }


def failure_candidates(s: Session, audit_id: uuid.UUID) -> list[m.FailureEvent]:
    return list(
        s.scalars(
            select(m.FailureEvent)
            .where(m.FailureEvent.audit_run_id == audit_id)
            .order_by(m.FailureEvent.confidence.desc())
        )
    )
