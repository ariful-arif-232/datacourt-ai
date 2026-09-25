from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from datacourt.api.deps import current_principal, not_demo
from datacourt.api.views import case_summary, iso
from datacourt.db import models as m
from datacourt.db.session import get_db
from datacourt.enums import Role
from datacourt.services import budget
from datacourt.tenancy import Principal, get_owned, get_version, resolve_audit

router = APIRouter(tags=["review"])


class BudgetIn(BaseModel):
    dataset_version_id: uuid.UUID
    max_items: int | None = Field(default=None, ge=1, le=2000)
    max_minutes: float | None = Field(default=None, gt=0, le=6000)
    objective: str = Field(default="balanced")
    create_session: bool = True
    name: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def _one_budget(self) -> BudgetIn:
        if self.max_items is None and self.max_minutes is None:
            raise ValueError("set max_items or max_minutes")
        if self.objective not in budget.OBJECTIVES:
            raise ValueError(f"objective must be one of {sorted(budget.OBJECTIVES)}")
        return self


def _case_rows(s: Session, audit_id: uuid.UUID) -> list[dict]:
    rows = s.execute(
        select(m.CourtCase, m.Sample.label, m.Sample.split)
        .join(m.Sample, m.Sample.id == m.CourtCase.sample_id)
        .where(m.CourtCase.audit_run_id == audit_id)
    ).all()
    return [
        {
            "id": str(c.id),
            "case_number": c.case_number,
            "verdict": str(c.verdict),
            "priority": c.priority_score,
            "impact": c.impact_score,
            "strength": c.strength_score,
            "uncertainty": str(c.uncertainty),
            "categories": c.categories or [],
            "status": str(c.status),
            "family_id": str(c.family_id) if c.family_id else None,
            "label": label,
            "split": str(split),
            "est_minutes": c.est_review_minutes,
        }
        for c, label, split in rows
    ]


@router.post("/review-budget")
def run_budget(
    body: BudgetIn, p: Principal = Depends(current_principal), s: Session = Depends(get_db)
) -> dict:
    v = get_version(s, p, body.dataset_version_id)
    a = resolve_audit(s, v, None)
    result = budget.optimize(
        _case_rows(s, a.id), max_items=body.max_items, max_minutes=body.max_minutes, objective=body.objective
    )
    session_id = None
    demo = p.user is not None and p.user.is_demo
    if body.create_session and not demo and result["selected"]:
        from datacourt.tenancy import require_org

        require_org(s, p, v.org_id, Role.REVIEWER)
        label = (
            body.name
            or (f"Budget: {body.max_items} cases" if body.max_items else f"Budget: {body.max_minutes:g} min")
            + f" · {body.objective}"
        )
        rs = m.ReviewSession(
            org_id=v.org_id,
            dataset_version_id=v.id,
            audit_run_id=a.id,
            name=label,
            mode="budget",
            params=body.model_dump(mode="json"),
            created_by=p.user_id,
        )
        s.add(rs)
        s.flush()
        for c in result["selected"]:
            s.add(
                m.ReviewQueueItem(
                    session_id=rs.id,
                    case_id=uuid.UUID(c["id"]),
                    rank=c["rank"],
                    reason=c["reason"],
                    est_minutes=c["est_minutes"],
                    value_score=c["marginal_value"],
                )
            )
        session_id = rs.id
    s.add(
        m.ReviewBudgetRun(
            dataset_version_id=v.id,
            audit_run_id=a.id,
            session_id=session_id,
            params=body.model_dump(mode="json"),
            result={k: val for k, val in result.items() if k != "selected"}
            | {"selected_ids": [c["id"] for c in result["selected"]]},
            created_by=p.user_id,
        )
    )
    s.commit()
    return {
        **result,
        "session_id": str(session_id) if session_id else None,
        "total_open_findings": result["open_cases"],
    }


@router.get("/versions/{version_id}/review-sessions")
def list_sessions(
    version_id: uuid.UUID, p: Principal = Depends(current_principal), s: Session = Depends(get_db)
) -> list[dict]:
    v = get_version(s, p, version_id)
    out = []
    for rs in s.scalars(
        select(m.ReviewSession)
        .where(m.ReviewSession.dataset_version_id == v.id)
        .order_by(m.ReviewSession.created_at.desc())
    ):
        items = s.execute(
            select(m.CourtCase.status)
            .join(m.ReviewQueueItem, m.ReviewQueueItem.case_id == m.CourtCase.id)
            .where(m.ReviewQueueItem.session_id == rs.id)
        ).all()
        done = sum(1 for (st,) in items if str(st) != "open")
        out.append(
            {
                "id": str(rs.id),
                "name": rs.name,
                "mode": rs.mode,
                "status": rs.status,
                "items": len(items),
                "reviewed": done,
                "created_at": iso(rs.created_at),
                "params": rs.params,
            }
        )
    return out


class SessionIn(BaseModel):
    dataset_version_id: uuid.UUID
    name: str = Field(min_length=1, max_length=160)
    case_ids: list[uuid.UUID] = Field(min_length=1, max_length=2000)


@router.post("/review-sessions")
def create_session(body: SessionIn, p: Principal = Depends(not_demo), s: Session = Depends(get_db)) -> dict:
    v = get_version(s, p, body.dataset_version_id, Role.REVIEWER)
    a = resolve_audit(s, v, None)
    cases = {
        c.id: c
        for c in s.scalars(
            select(m.CourtCase).where(m.CourtCase.audit_run_id == a.id, m.CourtCase.id.in_(body.case_ids))
        )
    }
    if len(cases) != len(set(body.case_ids)):
        raise HTTPException(
            status_code=422, detail="some cases do not belong to this dataset version's audit"
        )
    rs = m.ReviewSession(
        org_id=v.org_id,
        dataset_version_id=v.id,
        audit_run_id=a.id,
        name=body.name,
        mode="manual",
        created_by=p.user_id,
    )
    s.add(rs)
    s.flush()
    for rank, cid in enumerate(body.case_ids, start=1):
        c = cases[cid]
        s.add(
            m.ReviewQueueItem(
                session_id=rs.id,
                case_id=cid,
                rank=rank,
                reason="selected manually",
                est_minutes=c.est_review_minutes,
                value_score=c.priority_score,
            )
        )
    s.commit()
    return {"id": str(rs.id)}


@router.get("/review-sessions/{session_id}")
def get_session(
    session_id: uuid.UUID, p: Principal = Depends(current_principal), s: Session = Depends(get_db)
) -> dict:
    rs = get_owned(s, p, m.ReviewSession, session_id)
    rows = s.execute(
        select(m.ReviewQueueItem, m.CourtCase, m.Sample)
        .join(m.CourtCase, m.CourtCase.id == m.ReviewQueueItem.case_id)
        .join(m.Sample, m.Sample.id == m.CourtCase.sample_id)
        .where(m.ReviewQueueItem.session_id == rs.id)
        .order_by(m.ReviewQueueItem.rank)
    ).all()
    return {
        "id": str(rs.id),
        "name": rs.name,
        "mode": rs.mode,
        "params": rs.params,
        "dataset_version_id": str(rs.dataset_version_id),
        "created_at": iso(rs.created_at),
        "items": [
            {
                "rank": q.rank,
                "reason": q.reason,
                "est_minutes": q.est_minutes,
                "value": q.value_score,
                **case_summary(c, x),
            }
            for q, c, x in rows
        ],
    }
