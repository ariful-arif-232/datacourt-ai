"""Evidence ledger: append-only, hash-chained per organization.

entry_hash = sha256(prev_hash || event_type || entity || payload_sha256 || created_at)
Tampering with any stored payload breaks verification from that entry onward.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from datacourt.db.models import EvidenceLedgerEntry, SecurityAuditLog, utcnow
from datacourt.security import sha256_json

GENESIS = "0" * 64


def _entry_hash(
    prev_hash: str, event_type: str, entity_type: str, entity_id: str, payload_sha: str, ts: str
) -> str:
    h = hashlib.sha256()
    for part in (prev_hash, event_type, entity_type, entity_id, payload_sha, ts):
        h.update(part.encode())
        h.update(b"\x1f")
    return h.hexdigest()


def record(
    session: Session,
    *,
    org_id: uuid.UUID,
    event_type: str,
    entity_type: str,
    entity_id: uuid.UUID | str,
    payload: dict[str, Any],
    actor_id: uuid.UUID | None = None,
    dataset_version_id: uuid.UUID | None = None,
) -> EvidenceLedgerEntry:
    # Serialize appends per org so the chain stays linear under concurrency.
    session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"ledger:{org_id}"})
    prev = session.scalar(
        select(EvidenceLedgerEntry.entry_hash)
        .where(EvidenceLedgerEntry.org_id == org_id)
        .order_by(EvidenceLedgerEntry.seq.desc())
        .limit(1)
    )
    prev_hash = prev or GENESIS
    ts = utcnow()
    payload_sha = sha256_json(payload)
    entry = EvidenceLedgerEntry(
        org_id=org_id,
        dataset_version_id=dataset_version_id,
        entity_type=entity_type,
        entity_id=str(entity_id),
        event_type=event_type,
        actor_id=actor_id,
        payload=payload,
        payload_sha256=payload_sha,
        prev_hash=prev_hash,
        entry_hash=_entry_hash(
            prev_hash, event_type, entity_type, str(entity_id), payload_sha, ts.isoformat()
        ),
        created_at=ts,
    )
    session.add(entry)
    session.flush()
    return entry


def verify_chain(session: Session, org_id: uuid.UUID) -> dict[str, Any]:
    prev_hash = GENESIS
    checked = 0
    for e in session.scalars(
        select(EvidenceLedgerEntry)
        .where(EvidenceLedgerEntry.org_id == org_id)
        .order_by(EvidenceLedgerEntry.seq)
    ):
        payload_sha = sha256_json(e.payload)
        expected = _entry_hash(
            prev_hash, e.event_type, e.entity_type, e.entity_id, payload_sha, e.created_at.isoformat()
        )
        if e.prev_hash != prev_hash or payload_sha != e.payload_sha256 or expected != e.entry_hash:
            return {"valid": False, "checked": checked, "broken_at": e.seq}
        prev_hash = e.entry_hash
        checked += 1
    return {"valid": True, "checked": checked, "head": prev_hash}


def security_event(
    session: Session,
    action: str,
    *,
    org_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
    target_type: str | None = None,
    target_id: uuid.UUID | str | None = None,
    meta: dict | None = None,
) -> None:
    session.add(
        SecurityAuditLog(
            org_id=org_id,
            actor_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id else None,
            meta=meta or {},
        )
    )
