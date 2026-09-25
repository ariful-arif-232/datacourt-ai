from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from datacourt import ledger
from datacourt.api.deps import current_principal, current_user, not_demo
from datacourt.api.views import iso
from datacourt.db import models as m
from datacourt.db.session import get_db
from datacourt.enums import Role, RunStatus
from datacourt.ml.governance import CONTRACT_METRICS, DEFAULT_CONTRACT, OPS, validate_rules
from datacourt.pipeline.config import DEFAULT_CONFIG, ConfigError, resolve_config
from datacourt.security import hash_token, new_token, sha256_json
from datacourt.services import bootstrap, purge
from datacourt.tenancy import Principal, get_project, require_org

router = APIRouter(tags=["workspaces"])


class OrgIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


@router.post("/orgs")
def create_org(
    body: OrgIn,
    p: Principal = Depends(not_demo),
    user: m.User = Depends(current_user),
    s: Session = Depends(get_db),
) -> dict:
    org = bootstrap.create_org(s, body.name, user)
    ledger.security_event(s, "org.created", org_id=org.id, actor_id=user.id)
    s.commit()
    return {"id": str(org.id), "name": org.name, "slug": org.slug, "role": "owner"}


@router.get("/orgs/{org_id}")
def get_org(
    org_id: uuid.UUID, p: Principal = Depends(current_principal), s: Session = Depends(get_db)
) -> dict:
    role = require_org(s, p, org_id)
    org = s.get(m.Organization, org_id)
    assert org is not None
    members = s.execute(
        select(m.User, m.OrganizationMember.role)
        .join(m.OrganizationMember, m.OrganizationMember.user_id == m.User.id)
        .where(m.OrganizationMember.org_id == org_id)
        .order_by(m.User.email)
    ).all()
    return {
        "id": str(org.id),
        "name": org.name,
        "slug": org.slug,
        "role": str(role),
        "settings": org.settings,
        "retention_days": org.retention_days,
        "is_demo": org.is_demo,
        "members": [
            {
                "id": str(u.id),
                "email": u.email if role in (Role.OWNER, Role.ADMIN) else None,
                "name": u.name,
                "role": str(r),
            }
            for u, r in members
            if not u.is_demo
        ],
    }


class OrgSettingsIn(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    privacy_scan: bool | None = None
    # 0 clears the policy (keep archives indefinitely); omitted leaves it unchanged.
    retention_days: int | None = Field(default=None, ge=0, le=3650)


@router.patch("/orgs/{org_id}")
def update_org(
    org_id: uuid.UUID, body: OrgSettingsIn, p: Principal = Depends(not_demo), s: Session = Depends(get_db)
) -> dict:
    require_org(s, p, org_id, Role.ADMIN)
    org = s.get(m.Organization, org_id)
    assert org is not None
    if body.name:
        org.name = body.name
    if body.privacy_scan is not None:
        org.settings = {**org.settings, "privacy_scan": body.privacy_scan}
    if body.retention_days is not None:
        org.retention_days = body.retention_days or None
    ledger.security_event(
        s, "org.settings_updated", org_id=org_id, actor_id=p.user_id, meta=body.model_dump(exclude_none=True)
    )
    s.commit()
    return {"ok": True}


class MemberIn(BaseModel):
    email: EmailStr
    role: Role


@router.post("/orgs/{org_id}/members")
def add_member(
    org_id: uuid.UUID, body: MemberIn, p: Principal = Depends(not_demo), s: Session = Depends(get_db)
) -> dict:
    role = require_org(s, p, org_id, Role.ADMIN)
    if body.role == Role.OWNER and role != Role.OWNER:
        raise HTTPException(status_code=403, detail="only owners can add owners")
    user = s.scalar(select(m.User).where(m.User.email == body.email.strip().lower()))
    if user is None:
        raise HTTPException(
            status_code=404, detail="no registered user with that email; ask them to create an account first"
        )
    existing = s.scalar(
        select(m.OrganizationMember).where(
            m.OrganizationMember.org_id == org_id, m.OrganizationMember.user_id == user.id
        )
    )
    if existing:
        existing.role = body.role
    else:
        s.add(m.OrganizationMember(org_id=org_id, user_id=user.id, role=body.role))
    ledger.security_event(
        s,
        "org.member_set",
        org_id=org_id,
        actor_id=p.user_id,
        target_type="user",
        target_id=user.id,
        meta={"role": str(body.role)},
    )
    s.commit()
    return {"ok": True}


@router.delete("/orgs/{org_id}/members/{user_id}")
def remove_member(
    org_id: uuid.UUID, user_id: uuid.UUID, p: Principal = Depends(not_demo), s: Session = Depends(get_db)
) -> dict:
    require_org(s, p, org_id, Role.ADMIN)
    mem = s.scalar(
        select(m.OrganizationMember).where(
            m.OrganizationMember.org_id == org_id, m.OrganizationMember.user_id == user_id
        )
    )
    if mem is None:
        raise HTTPException(status_code=404, detail="not found")
    if mem.role == Role.OWNER:
        owners = s.scalar(
            select(func.count())
            .select_from(m.OrganizationMember)
            .where(m.OrganizationMember.org_id == org_id, m.OrganizationMember.role == Role.OWNER)
        )
        if owners <= 1:
            raise HTTPException(status_code=409, detail="cannot remove the last owner")
    s.delete(mem)
    ledger.security_event(
        s, "org.member_removed", org_id=org_id, actor_id=p.user_id, target_type="user", target_id=user_id
    )
    s.commit()
    return {"ok": True}


class DeleteOrgIn(BaseModel):
    confirm_name: str


@router.delete("/orgs/{org_id}")
def delete_org(
    org_id: uuid.UUID, body: DeleteOrgIn, p: Principal = Depends(not_demo), s: Session = Depends(get_db)
) -> dict:
    require_org(s, p, org_id, Role.OWNER)
    org = s.get(m.Organization, org_id)
    assert org is not None
    if body.confirm_name != org.name:
        raise HTTPException(status_code=400, detail="confirmation name does not match")
    ledger.security_event(s, "org.deleted", actor_id=p.user_id, meta={"org_id": str(org_id)})
    job_id = bootstrap.delete_org_data(s, org)
    s.commit()
    return purge.run_inline([job_id])


# ---- projects ---------------------------------------------------------------


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4000)


@router.get("/orgs/{org_id}/projects")
def list_projects(
    org_id: uuid.UUID, p: Principal = Depends(current_principal), s: Session = Depends(get_db)
) -> list[dict]:
    require_org(s, p, org_id)
    projects = s.scalars(
        select(m.Project).where(m.Project.org_id == org_id).order_by(m.Project.created_at.desc())
    ).all()
    counts = dict(
        s.execute(
            select(m.Dataset.project_id, func.count())
            .where(m.Dataset.org_id == org_id)
            .group_by(m.Dataset.project_id)
        ).all()
    )
    return [
        {
            "id": str(x.id),
            "name": x.name,
            "description": x.description,
            "task_type": x.task_type,
            "datasets": counts.get(x.id, 0),
            "created_at": iso(x.created_at),
        }
        for x in projects
    ]


@router.post("/orgs/{org_id}/projects")
def create_project(
    org_id: uuid.UUID, body: ProjectIn, p: Principal = Depends(not_demo), s: Session = Depends(get_db)
) -> dict:
    require_org(s, p, org_id, Role.ADMIN)
    if s.scalar(select(m.Project.id).where(m.Project.org_id == org_id, m.Project.name == body.name.strip())):
        raise HTTPException(status_code=409, detail="a project with this name already exists")
    proj = m.Project(
        org_id=org_id, name=body.name.strip(), description=body.description, created_by=p.user_id
    )
    s.add(proj)
    s.commit()
    return {"id": str(proj.id), "name": proj.name}


@router.get("/projects/{project_id}")
def get_project_detail(
    project_id: uuid.UUID, p: Principal = Depends(current_principal), s: Session = Depends(get_db)
) -> dict:
    proj = get_project(s, p, project_id)
    datasets = s.scalars(
        select(m.Dataset).where(m.Dataset.project_id == project_id).order_by(m.Dataset.created_at.desc())
    ).all()
    out = []
    for d in datasets:
        latest = s.scalar(
            select(m.DatasetVersion)
            .where(m.DatasetVersion.dataset_id == d.id)
            .order_by(m.DatasetVersion.version_number.desc())
            .limit(1)
        )
        audit = None
        if latest:
            audit = s.scalar(
                select(m.AuditRun)
                .where(m.AuditRun.dataset_version_id == latest.id)
                .order_by(m.AuditRun.created_at.desc())
                .limit(1)
            )
        out.append(
            {
                "id": str(d.id),
                "name": d.name,
                "description": d.description,
                "created_at": iso(d.created_at),
                "latest_version": {
                    "id": str(latest.id),
                    "version_number": latest.version_number,
                    "status": str(latest.status),
                    "samples": (latest.stats or {}).get("sample_count"),
                }
                if latest
                else None,
                "latest_audit": {
                    "id": str(audit.id),
                    "status": str(audit.status),
                    "progress": audit.progress,
                    "debt": (audit.summary or {}).get("debt"),
                    "preflight": (audit.summary or {}).get("preflight"),
                    "cases": (audit.summary or {}).get("cases"),
                }
                if audit
                else None,
            }
        )
    contract = s.scalar(
        select(m.DataContract)
        .where(m.DataContract.project_id == project_id, m.DataContract.is_active.is_(True))
        .order_by(m.DataContract.version.desc())
        .limit(1)
    )
    return {
        "id": str(proj.id),
        "org_id": str(proj.org_id),
        "name": proj.name,
        "description": proj.description,
        "task_type": proj.task_type,
        "datasets": out,
        "contract": _contract_view(contract) if contract else None,
    }


# ---- audit configuration -------------------------------------------------------


@router.get("/projects/{project_id}/audit-config")
def get_audit_config(
    project_id: uuid.UUID, p: Principal = Depends(current_principal), s: Session = Depends(get_db)
) -> dict:
    get_project(s, p, project_id)
    cfg = s.scalar(
        select(m.AuditConfig)
        .where(m.AuditConfig.project_id == project_id)
        .order_by(m.AuditConfig.version.desc())
        .limit(1)
    )
    resolved, h = resolve_config(cfg.config if cfg else {})
    return {
        "version": cfg.version if cfg else 0,
        "overrides": cfg.config if cfg else {},
        "resolved": resolved,
        "config_hash": h,
        "defaults": DEFAULT_CONFIG,
    }


class ConfigIn(BaseModel):
    overrides: dict


@router.put("/projects/{project_id}/audit-config")
def put_audit_config(
    project_id: uuid.UUID, body: ConfigIn, p: Principal = Depends(not_demo), s: Session = Depends(get_db)
) -> dict:
    get_project(s, p, project_id, Role.ADMIN)
    try:
        _, h = resolve_config(body.overrides)
    except ConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    n = (
        s.scalar(select(func.max(m.AuditConfig.version)).where(m.AuditConfig.project_id == project_id)) or 0
    ) + 1
    s.add(
        m.AuditConfig(
            project_id=project_id,
            version=n,
            config=body.overrides,
            config_hash=sha256_json(body.overrides),
            created_by=p.user_id,
        )
    )
    s.commit()
    return {"version": n, "config_hash": h}


# ---- data contracts ---------------------------------------------------------------


def _contract_view(c: m.DataContract) -> dict:
    return {
        "id": str(c.id),
        "version": c.version,
        "name": c.name,
        "rules": c.rules,
        "created_at": iso(c.created_at),
    }


class ContractIn(BaseModel):
    name: str = Field(default="Default contract", max_length=120)
    rules: list[dict]


@router.get("/contract-metrics")
def contract_metrics() -> dict:
    return {
        "metrics": {k: v[0] for k, v in CONTRACT_METRICS.items()},
        "operators": list(OPS),
        "default_rules": DEFAULT_CONTRACT,
    }


@router.put("/projects/{project_id}/contract")
def put_contract(
    project_id: uuid.UUID, body: ContractIn, p: Principal = Depends(not_demo), s: Session = Depends(get_db)
) -> dict:
    get_project(s, p, project_id, Role.ADMIN)
    errors = validate_rules(body.rules)
    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors))
    for c in s.scalars(
        select(m.DataContract).where(
            m.DataContract.project_id == project_id, m.DataContract.is_active.is_(True)
        )
    ):
        c.is_active = False
    n = (
        s.scalar(select(func.max(m.DataContract.version)).where(m.DataContract.project_id == project_id)) or 0
    ) + 1
    c = m.DataContract(
        project_id=project_id, version=n, name=body.name, rules=body.rules, created_by=p.user_id
    )
    s.add(c)
    s.commit()
    return _contract_view(c)


# ---- API tokens -------------------------------------------------------------------


class TokenIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    scopes: list[str] = Field(default_factory=lambda: ["contracts:read"])


@router.get("/orgs/{org_id}/tokens")
def list_tokens(
    org_id: uuid.UUID, p: Principal = Depends(current_principal), s: Session = Depends(get_db)
) -> list[dict]:
    require_org(s, p, org_id, Role.ADMIN)
    return [
        {
            "id": str(t.id),
            "name": t.name,
            "prefix": t.prefix,
            "scopes": t.scopes,
            "created_at": iso(t.created_at),
            "last_used_at": iso(t.last_used_at),
            "revoked": t.revoked_at is not None,
        }
        for t in s.scalars(
            select(m.ApiToken).where(m.ApiToken.org_id == org_id).order_by(m.ApiToken.created_at.desc())
        )
    ]


@router.post("/orgs/{org_id}/tokens")
def create_token(
    org_id: uuid.UUID,
    body: TokenIn,
    p: Principal = Depends(not_demo),
    user: m.User = Depends(current_user),
    s: Session = Depends(get_db),
) -> dict:
    require_org(s, p, org_id, Role.ADMIN)
    allowed = {"contracts:read", "audits:read", "audits:write"}
    if not set(body.scopes) <= allowed:
        raise HTTPException(status_code=422, detail=f"scopes must be within {sorted(allowed)}")
    token = new_token("dct_")
    t = m.ApiToken(
        org_id=org_id,
        created_by=user.id,
        name=body.name,
        prefix=token[:10],
        token_hash=hash_token(token),
        scopes=body.scopes,
    )
    s.add(t)
    ledger.security_event(
        s,
        "token.created",
        org_id=org_id,
        actor_id=user.id,
        target_type="api_token",
        target_id=t.id,
        meta={"scopes": body.scopes},
    )
    s.commit()
    return {"id": str(t.id), "token": token, "note": "Store this token now; it cannot be shown again."}


@router.delete("/orgs/{org_id}/tokens/{token_id}")
def revoke_token(
    org_id: uuid.UUID, token_id: uuid.UUID, p: Principal = Depends(not_demo), s: Session = Depends(get_db)
) -> dict:
    require_org(s, p, org_id, Role.ADMIN)
    t = s.get(m.ApiToken, token_id)
    if t is None or t.org_id != org_id:
        raise HTTPException(status_code=404, detail="not found")
    t.revoked_at = datetime.now(UTC)
    ledger.security_event(
        s, "token.revoked", org_id=org_id, actor_id=p.user_id, target_type="api_token", target_id=t.id
    )
    s.commit()
    return {"ok": True}


# ---- ledger, security log, metrics ---------------------------------------------------


@router.get("/orgs/{org_id}/ledger")
def get_ledger(
    org_id: uuid.UUID,
    dataset_version_id: uuid.UUID | None = None,
    entity_id: str | None = None,
    limit: int = Query(100, le=500),
    before_seq: int | None = None,
    p: Principal = Depends(current_principal),
    s: Session = Depends(get_db),
) -> dict:
    require_org(s, p, org_id)
    q = select(m.EvidenceLedgerEntry).where(m.EvidenceLedgerEntry.org_id == org_id)
    if dataset_version_id:
        q = q.where(m.EvidenceLedgerEntry.dataset_version_id == dataset_version_id)
    if entity_id:
        q = q.where(m.EvidenceLedgerEntry.entity_id == entity_id)
    if before_seq:
        q = q.where(m.EvidenceLedgerEntry.seq < before_seq)
    rows = s.scalars(q.order_by(m.EvidenceLedgerEntry.seq.desc()).limit(limit)).all()
    actors = {
        u.id: u.name
        for u in s.scalars(select(m.User).where(m.User.id.in_({r.actor_id for r in rows if r.actor_id})))
    }
    return {
        "entries": [
            {
                "seq": r.seq,
                "id": str(r.id),
                "event_type": r.event_type,
                "entity_type": r.entity_type,
                "entity_id": r.entity_id,
                "actor": actors.get(r.actor_id),
                "payload": r.payload,
                "payload_sha256": r.payload_sha256,
                "prev_hash": r.prev_hash,
                "entry_hash": r.entry_hash,
                "dataset_version_id": str(r.dataset_version_id) if r.dataset_version_id else None,
                "created_at": iso(r.created_at),
            }
            for r in rows
        ]
    }


@router.get("/orgs/{org_id}/ledger/verify")
def verify_ledger(
    org_id: uuid.UUID, p: Principal = Depends(current_principal), s: Session = Depends(get_db)
) -> dict:
    require_org(s, p, org_id)
    return ledger.verify_chain(s, org_id)


@router.get("/orgs/{org_id}/security-log")
def security_log(
    org_id: uuid.UUID, p: Principal = Depends(current_principal), s: Session = Depends(get_db)
) -> list[dict]:
    require_org(s, p, org_id, Role.ADMIN)
    rows = s.scalars(
        select(m.SecurityAuditLog)
        .where(m.SecurityAuditLog.org_id == org_id)
        .order_by(m.SecurityAuditLog.created_at.desc())
        .limit(200)
    ).all()
    return [
        {
            "action": r.action,
            "target_type": r.target_type,
            "target_id": r.target_id,
            "meta": r.meta,
            "created_at": iso(r.created_at),
        }
        for r in rows
    ]


@router.get("/orgs/{org_id}/metrics")
def org_metrics(
    org_id: uuid.UUID, p: Principal = Depends(current_principal), s: Session = Depends(get_db)
) -> dict:
    """Operational metrics for workspace admins (scoped to this organization only)."""
    require_org(s, p, org_id, Role.ADMIN)
    audits = s.scalars(select(m.AuditRun).where(m.AuditRun.org_id == org_id)).all()
    durations = [
        (a.finished_at - a.started_at).total_seconds() for a in audits if a.finished_at and a.started_at
    ]
    jobs_by_status = dict(
        s.execute(
            select(m.Job.status, func.count()).where(m.Job.org_id == org_id).group_by(m.Job.status)
        ).all()
    )
    failures = s.execute(
        select(m.Job.error_class, func.count())
        .where(m.Job.org_id == org_id, m.Job.status == RunStatus.FAILED)
        .group_by(m.Job.error_class)
    ).all()
    return {
        "audits_run": len(audits),
        "audits_by_status": {str(k): v for k, v in _count(str(a.status) for a in audits).items()},
        "median_audit_seconds": sorted(durations)[len(durations) // 2] if durations else None,
        "jobs_by_status": {str(k): v for k, v in jobs_by_status.items()},
        "queue_length": int(jobs_by_status.get(RunStatus.QUEUED, 0)),
        "failures_by_class": {str(k): v for k, v in failures},
    }


def _count(items) -> dict:
    out: dict = {}
    for i in items:
        out[i] = out.get(i, 0) + 1
    return out
