"""Tenant-aware access checks.

Every resource lookup goes through here: the object is loaded, its owning organization
resolved, and the caller's membership/role verified. Missing objects and objects the
caller cannot see produce the same 404, so IDs cannot be probed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from datacourt.db import models as m
from datacourt.enums import ROLE_RANK, Role, RunStatus


@dataclass
class Principal:
    user: m.User | None
    token: m.ApiToken | None = None

    @property
    def user_id(self) -> uuid.UUID | None:
        return self.user.id if self.user else None


def not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="not found")


def membership(s: Session, p: Principal, org_id: uuid.UUID) -> Role | None:
    if p.token is not None:
        if p.token.org_id != org_id:
            return None
        return Role.ADMIN if "audits:write" in (p.token.scopes or []) else Role.VIEWER
    if p.user is None:
        return None
    role = s.scalar(
        select(m.OrganizationMember.role).where(
            m.OrganizationMember.org_id == org_id, m.OrganizationMember.user_id == p.user.id
        )
    )
    return Role(role) if role else None


def require_org(s: Session, p: Principal, org_id: uuid.UUID, min_role: Role = Role.VIEWER) -> Role:
    role = membership(s, p, org_id)
    if role is None:
        raise not_found()
    if ROLE_RANK[role] < ROLE_RANK[min_role]:
        raise HTTPException(status_code=403, detail=f"requires {min_role} role")
    return role


def get_project(s: Session, p: Principal, project_id: uuid.UUID, min_role: Role = Role.VIEWER) -> m.Project:
    obj = s.get(m.Project, project_id)
    if obj is None:
        raise not_found()
    require_org(s, p, obj.org_id, min_role)
    return obj


def get_dataset(s: Session, p: Principal, dataset_id: uuid.UUID, min_role: Role = Role.VIEWER) -> m.Dataset:
    obj = s.get(m.Dataset, dataset_id)
    if obj is None:
        raise not_found()
    require_org(s, p, obj.org_id, min_role)
    return obj


def get_version(
    s: Session, p: Principal, version_id: uuid.UUID, min_role: Role = Role.VIEWER
) -> m.DatasetVersion:
    obj = s.get(m.DatasetVersion, version_id)
    if obj is None:
        raise not_found()
    require_org(s, p, obj.org_id, min_role)
    return obj


def get_audit(s: Session, p: Principal, audit_id: uuid.UUID, min_role: Role = Role.VIEWER) -> m.AuditRun:
    obj = s.get(m.AuditRun, audit_id)
    if obj is None:
        raise not_found()
    require_org(s, p, obj.org_id, min_role)
    return obj


def get_case(s: Session, p: Principal, case_id: uuid.UUID, min_role: Role = Role.VIEWER) -> m.CourtCase:
    obj = s.get(m.CourtCase, case_id)
    if obj is None:
        raise not_found()
    require_org(s, p, obj.org_id, min_role)
    return obj


def get_sample(s: Session, p: Principal, sample_id: uuid.UUID) -> tuple[m.Sample, m.DatasetVersion]:
    obj = s.get(m.Sample, sample_id)
    if obj is None:
        raise not_found()
    version = get_version(s, p, obj.dataset_version_id)
    return obj, version


def get_owned(s: Session, p: Principal, model, obj_id: uuid.UUID, min_role: Role = Role.VIEWER):
    """For models with a direct org_id column."""
    obj = s.get(model, obj_id)
    if obj is None:
        raise not_found()
    require_org(s, p, obj.org_id, min_role)
    return obj


def resolve_audit(
    s: Session, version: m.DatasetVersion, audit_id: uuid.UUID | None = None, require_complete: bool = True
) -> m.AuditRun:
    """The requested audit (must belong to the version) or the latest completed one."""
    if audit_id is not None:
        a = s.get(m.AuditRun, audit_id)
        if a is None or a.dataset_version_id != version.id:
            raise not_found()
        if require_complete and a.status != RunStatus.COMPLETED:
            raise HTTPException(status_code=409, detail="audit is not complete")
        return a
    q = select(m.AuditRun).where(m.AuditRun.dataset_version_id == version.id)
    if require_complete:
        q = q.where(m.AuditRun.status == RunStatus.COMPLETED)
    a = s.scalar(q.order_by(m.AuditRun.created_at.desc()).limit(1))
    if a is None:
        raise HTTPException(status_code=409, detail="no completed audit for this dataset version yet")
    return a
