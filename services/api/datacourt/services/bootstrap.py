"""Workspace creation, account deletion and the public demo workspace."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from datacourt.db import models as m
from datacourt.enums import Role, VersionStatus
from datacourt.security import hash_password, new_token

DEMO_SLUG = "datacourt-demo"


def slugify(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "workspace"
    return f"{base}-{uuid.uuid4().hex[:6]}"


def create_user(s: Session, email: str, password: str, name: str, *, demo: bool = False) -> m.User:
    u = m.User(
        email=email.strip().lower(),
        name=name.strip()[:120],
        password_hash=hash_password(password),
        is_demo=demo,
    )
    s.add(u)
    s.flush()
    return u


def create_org(s: Session, name: str, owner: m.User, *, demo: bool = False) -> m.Organization:
    org = m.Organization(
        name=name.strip()[:120] or "Workspace",
        slug=DEMO_SLUG if demo else slugify(name),
        is_demo=demo,
        settings={"privacy_scan": True},
    )
    s.add(org)
    s.flush()
    s.add(m.OrganizationMember(org_id=org.id, user_id=owner.id, role=Role.OWNER))
    return org


def create_session(s: Session, user: m.User, ttl_hours: int, user_agent: str | None) -> str:
    token = new_token()
    from datacourt.security import hash_token

    s.add(
        m.AuthSession(
            user_id=user.id,
            token_hash=hash_token(token),
            expires_at=datetime.now(UTC) + timedelta(hours=ttl_hours),
            user_agent=(user_agent or "")[:200],
        )
    )
    user.last_login_at = datetime.now(UTC)
    return token


def create_version(
    s: Session,
    dataset: m.Dataset,
    *,
    filename: str,
    created_by: uuid.UUID | None,
    notes: str = "",
    origin: str = "upload",
) -> m.DatasetVersion:
    n = (
        s.scalar(
            select(func.max(m.DatasetVersion.version_number)).where(m.DatasetVersion.dataset_id == dataset.id)
        )
        or 0
    ) + 1
    prev = s.scalar(
        select(m.DatasetVersion.id)
        .where(m.DatasetVersion.dataset_id == dataset.id)
        .order_by(m.DatasetVersion.version_number.desc())
        .limit(1)
    )
    v = m.DatasetVersion(
        org_id=dataset.org_id,
        dataset_id=dataset.id,
        version_number=n,
        parent_version_id=prev,
        origin=origin,
        status=VersionStatus.AWAITING_UPLOAD,
        source_filename=filename[:255],
        created_by=created_by,
        notes=notes[:4000],
    )
    s.add(v)
    s.flush()
    return v


def delete_org_data(s: Session, org: m.Organization) -> uuid.UUID:
    """Deletes the org row (cascades) and schedules the purge of its storage namespace.

    Returns the purge job id; call `purge.run_inline` with it after committing.
    """
    from datacourt.services import purge

    job = purge.schedule(s, [f"orgs/{org.id}"], org_id=org.id, reason="org.deleted")
    s.delete(org)
    return job.id


def delete_account(s: Session, user: m.User) -> dict:
    """Deletes the user; organizations where they are the only owner are deleted with all data."""
    deleted_orgs = 0
    purge_jobs: list[uuid.UUID] = []
    for mem in s.scalars(select(m.OrganizationMember).where(m.OrganizationMember.user_id == user.id)).all():
        if mem.role != Role.OWNER:
            continue
        other_owners = s.scalar(
            select(func.count())
            .select_from(m.OrganizationMember)
            .where(
                m.OrganizationMember.org_id == mem.org_id,
                m.OrganizationMember.role == Role.OWNER,
                m.OrganizationMember.user_id != user.id,
            )
        )
        if not other_owners:
            org = s.get(m.Organization, mem.org_id)
            if org is not None and not org.is_demo:
                purge_jobs.append(delete_org_data(s, org))
                deleted_orgs += 1
    s.flush()
    # Review decisions and notes in workspaces that still exist belong to those workspaces' audit
    # trails; deleting them would silently reopen cases. Keep the row but strip personal data.
    authored = s.scalar(
        select(func.count()).select_from(m.HumanDecision).where(m.HumanDecision.reviewer_id == user.id)
    ) or s.scalar(select(func.count()).select_from(m.ReviewerNote).where(m.ReviewerNote.user_id == user.id))
    if authored:
        s.execute(delete(m.OrganizationMember).where(m.OrganizationMember.user_id == user.id))
        s.execute(delete(m.AuthSession).where(m.AuthSession.user_id == user.id))
        user.email = f"deleted-{uuid.uuid4().hex}@deleted.invalid"
        user.name = "Deleted user"
        user.password_hash = hash_password(new_token())
        user.is_active = False
        return {"deleted_organizations": deleted_orgs, "purge_jobs": purge_jobs, "pseudonymised": True}
    s.delete(user)
    return {"deleted_organizations": deleted_orgs, "purge_jobs": purge_jobs, "pseudonymised": False}


def demo_org(s: Session) -> m.Organization | None:
    return s.scalar(select(m.Organization).where(m.Organization.slug == DEMO_SLUG))


def join_demo(s: Session) -> m.User:
    """Creates an ephemeral read-only viewer in the demo workspace."""
    org = demo_org(s)
    if org is None:
        raise LookupError("demo workspace is not seeded")
    user = create_user(
        s, f"demo-{uuid.uuid4().hex[:10]}@demo.datacourt.local", new_token(), "Demo visitor", demo=True
    )
    s.add(m.OrganizationMember(org_id=org.id, user_id=user.id, role=Role.VIEWER))
    return user
