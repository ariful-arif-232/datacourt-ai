import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import update

from datacourt import jobs, ledger
from datacourt.db import models as m
from datacourt.db.session import new_session
from datacourt.enums import JobType, RunStatus


def _clear_queue():
    with new_session() as s:
        s.execute(
            update(m.Job)
            .where(m.Job.status.in_([RunStatus.QUEUED, RunStatus.RUNNING]))
            .values(status=RunStatus.CANCELLED)
        )
        s.commit()


def test_claim_is_exclusive_and_idempotent_enqueue():
    _clear_queue()
    with new_session() as s:
        j1 = jobs.enqueue(s, JobType.REPORT, {"report_id": str(uuid.uuid4())}, idempotency_key="k-1")
        j2 = jobs.enqueue(s, JobType.REPORT, {"report_id": "ignored"}, idempotency_key="k-1")
        s.commit()
        assert j1.id == j2.id
    with new_session() as a, new_session() as b:
        c1 = jobs.claim(a, "w1")
        c2 = jobs.claim(b, "w2")
    assert c1 is not None and c1.id == j1.id
    assert c2 is None


def test_retry_backoff_then_permanent_failure():
    _clear_queue()
    with new_session() as s:
        j = jobs.enqueue(s, JobType.REPORT, {"report_id": str(uuid.uuid4())}, max_attempts=2)
        s.commit()
        jid = j.id
    with new_session() as s:
        jobs.claim(s, "w")
        assert jobs.fail(s, jid, RuntimeError("boom")) == "queued"
    with new_session() as s:
        s.execute(update(m.Job).where(m.Job.id == jid).values(run_after=datetime.now(UTC)))
        s.commit()
        jobs.claim(s, "w")
        assert jobs.fail(s, jid, RuntimeError("boom")) == "failed"
    with new_session() as s:
        assert s.get(m.Job, jid).error_class == "internal"


def test_permanent_errors_do_not_retry():
    _clear_queue()
    with new_session() as s:
        j = jobs.enqueue(s, JobType.REPORT, {"report_id": str(uuid.uuid4())}, max_attempts=5)
        s.commit()
        jobs.claim(s, "w")
        assert jobs.fail(s, j.id, jobs.PermanentJobError("bad input")) == "failed"
        assert s.get(m.Job, j.id).error_class == "permanent"


def test_reaper_requeues_stale_jobs():
    _clear_queue()
    with new_session() as s:
        j = jobs.enqueue(s, JobType.REPORT, {"report_id": str(uuid.uuid4())})
        s.commit()
        jobs.claim(s, "dead-worker")
        s.execute(
            update(m.Job)
            .where(m.Job.id == j.id)
            .values(heartbeat_at=datetime.now(UTC) - timedelta(minutes=30))
        )
        s.commit()
        assert jobs.reap_stale(s, 60) == 1
        s.refresh(j)
        assert j.status == RunStatus.QUEUED and j.error_class == "worker_lost"


def test_ledger_chain_detects_tampering():
    with new_session() as s:
        org = m.Organization(name="L", slug=f"l-{uuid.uuid4().hex[:6]}")
        s.add(org)
        s.flush()
        for i in range(3):
            ledger.record(
                s, org_id=org.id, event_type="test.event", entity_type="x", entity_id=str(i), payload={"i": i}
            )
        s.commit()
        assert ledger.verify_chain(s, org.id) == {
            "valid": True,
            "checked": 3,
            "head": ledger.verify_chain(s, org.id)["head"],
        }
        e = s.query(m.EvidenceLedgerEntry).filter_by(org_id=org.id, entity_id="1").one()
        e.payload = {"i": 999}
        s.commit()
        res = ledger.verify_chain(s, org.id)
        assert res["valid"] is False and res["broken_at"] == e.seq


def test_retention_purges_only_expired_archives():
    from datacourt.enums import VersionStatus
    from datacourt.services import bootstrap
    from datacourt.services.retention import sweep
    from datacourt.storage import get_store, source_zip_key

    store = get_store()
    old = datetime.now(UTC) - timedelta(days=40)
    with new_session() as s:
        user = bootstrap.create_user(
            s, f"ret-{uuid.uuid4().hex[:6]}@example.com", "correct-horse-battery", "R"
        )
        org = bootstrap.create_org(s, "Retention Org", user)
        project = m.Project(org_id=org.id, name="p")
        s.add(project)
        s.flush()
        ds = m.Dataset(org_id=org.id, project_id=project.id, name="d")
        s.add(ds)
        s.flush()
        versions = []
        for age in (old, datetime.now(UTC)):
            v = bootstrap.create_version(s, ds, filename="a.zip", created_by=user.id)
            s.flush()
            key = source_zip_key(org.id, ds.id, v.id)
            store.put_bytes(key, b"PK\x03\x04zip", "application/zip")
            v.source_object_key, v.status, v.created_at = key, VersionStatus.READY, age
            versions.append((v.id, key))
        s.commit()
        org_id = org.id

    with new_session() as s:  # no policy -> nothing purged
        sweep(s)
        s.commit()
    assert all(store.exists(k) for _, k in versions)

    with new_session() as s:
        s.get(m.Organization, org_id).retention_days = 30
        s.commit()
        purged = sweep(s)
        s.commit()
    assert purged["source_archives"] >= 1
    (old_id, old_key), (new_id, new_key) = versions
    assert not store.exists(old_key) and store.exists(new_key)
    with new_session() as s:
        assert s.get(m.DatasetVersion, old_id).source_object_key is None
        assert s.get(m.DatasetVersion, old_id).status == VersionStatus.READY  # the version itself is kept
        assert s.get(m.DatasetVersion, new_id).source_object_key == new_key
