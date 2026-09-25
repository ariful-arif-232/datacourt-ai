"""End-to-end: workspace → upload → audit → findings → case → decision → budget → what-if → export → report → CI gate."""

from __future__ import annotations

import pytest
from sqlalchemy import update

from datacourt.benchmark.generator import generate
from datacourt.db import models as m
from datacourt.db.session import new_session
from datacourt.enums import RunStatus
from datacourt.worker import Worker
from tests.conftest import register

API = "/api/v1"


def drain() -> None:
    with new_session() as s:
        s.execute(update(m.Job).where(m.Job.status == RunStatus.QUEUED).values(run_after=m.utcnow()))
        s.commit()
    Worker("test-worker").drain()


@pytest.fixture(scope="module")
def dataset_zip() -> tuple[bytes, dict]:
    data, gt = generate(seed=3, per_class_train=36, per_class_eval=10, minority_train=18)
    return data, gt.__dict__


def _upload(client, project_id: str, name: str, data: bytes, audit: str = "deep") -> str:
    d = client.post(
        f"{API}/projects/{project_id}/datasets",
        json={"name": name, "provenance": {"source": "synthetic", "license": "CC0"}},
    )
    assert d.status_code == 200, d.text
    v = client.post(
        f"{API}/datasets/{d.json()['id']}/versions",
        json={"filename": f"{name}.zip", "size_bytes": len(data), "auto_audit": audit},
    )
    assert v.status_code == 200, v.text
    up = v.json()["upload"]
    assert up["method"] == "PUT"
    r = client.put(
        up["url"].replace("http://testserver", ""), content=data, headers={"content-type": "application/zip"}
    )
    assert r.status_code == 200, r.text
    vid = v.json()["version"]["id"]
    r = client.post(f"{API}/versions/{vid}/finalize-upload")
    assert r.status_code == 200, r.text
    return vid


def test_full_flow(client, dataset_zip):
    data, gt = dataset_zip
    me = register(client, "owner@example.com", "Owner Person")
    org_id = me["organizations"][0]["id"]
    proj = client.post(f"{API}/orgs/{org_id}/projects", json={"name": "Shapes"}).json()
    vid = _upload(client, proj["id"], "shapes", data)
    drain()

    detail = client.get(f"{API}/versions/{vid}").json()
    assert detail["status"] == "ready"
    assert detail["stats"]["sample_count"] > 150
    assert detail["stats"]["files"].get("rejected") == 1  # the deliberately corrupt file
    audit = detail["audits"][0]
    assert audit["status"] == "completed", audit

    prog = client.get(f"{API}/audits/{audit['id']}/progress").json()
    assert prog["progress"] == 1.0
    assert all(st["status"] in ("completed", "skipped") for st in prog["steps"])

    ov = client.get(f"{API}/versions/{vid}/overview").json()
    assert ov["debt"]["overall"] in ("LOW", "MODERATE", "HIGH", "CRITICAL")
    assert ov["facts"]["duplicate_families"] > 0

    leak = client.get(f"{API}/versions/{vid}/leakage").json()
    assert any(f["risk"] in ("high", "critical") for f in leak["findings"])
    dups = client.get(f"{API}/versions/{vid}/duplicates").json()
    fam = dups["families"][0]
    lin = client.get(f"{API}/families/{fam['id']}/lineage").json()
    assert len(lin["nodes"]) == fam["size"]
    assert client.get(f"{API}/versions/{vid}/labels").json()["total"] > 0
    assert client.get(f"{API}/versions/{vid}/shortcuts").status_code == 200
    cov = client.get(f"{API}/versions/{vid}/coverage").json()
    assert cov["points"] and cov["clusters"]
    assert client.get(f"{API}/versions/{vid}/cartography").json()["available"] is True
    assert client.get(f"{API}/versions/{vid}/influence").json()["available"] is True
    assert "drift" in client.get(f"{API}/versions/{vid}/dna").json()

    # Signed thumbnail URLs work; tampered ones do not.
    sample_list = client.get(f"{API}/versions/{vid}/samples", params={"limit": 5}).json()
    thumb = sample_list["items"][0]["thumb_url"]
    assert client.get(thumb).status_code == 200
    assert client.get(thumb[:-3] + "abc").status_code == 403

    cases = client.get(f"{API}/versions/{vid}/cases", params={"sort": "priority"}).json()
    assert cases["total"] > 0
    first = cases["items"][0]
    case = client.get(f"{API}/cases/{first['id']}").json()
    assert case["evidence"] and case["verdict_record"]["rule_trace"]
    stances = {e["stance"] for e in case["evidence"]}
    assert stances & {"prosecution", "defense"}
    expl = client.post(f"{API}/cases/{first['id']}/explain").json()
    assert expl["source"] == "template" and expl["content"]["prosecutor"]

    # Human decision, disagreement and adjudication.
    relabel_case = next(
        c for c in cases["items"] if c["verdict"] in ("POSSIBLE_RELABEL", "STRONG_REVIEW", "REVIEW")
    )
    rc = client.get(f"{API}/cases/{relabel_case['id']}").json()
    target = next(c for c in rc["classes"] if c["name"] != rc["sample"]["label"])
    r = client.post(
        f"{API}/cases/{relabel_case['id']}/decision",
        json={"action": "relabel", "target_class_id": target["id"], "note": "clearly another shape"},
    )
    assert r.status_code == 200 and r.json()["case_status"] == "decided"
    bad = client.post(f"{API}/cases/{relabel_case['id']}/decision", json={"action": "relabel"})
    assert bad.status_code == 422

    # Second reviewer disagrees → disputed → owner adjudicates.
    with new_session() as s:
        from datacourt.enums import Role
        from datacourt.services.bootstrap import create_user

        u2 = create_user(s, "reviewer2@example.com", "another-long-password", "Reviewer Two")
        s.add(m.OrganizationMember(org_id=__import__("uuid").UUID(org_id), user_id=u2.id, role=Role.REVIEWER))
        s.commit()
    from fastapi.testclient import TestClient

    from datacourt.main import create_app

    with TestClient(create_app()) as c2:
        c2.headers.update({"x-datacourt-csrf": "1"})
        assert (
            c2.post(
                f"{API}/auth/login",
                json={"email": "reviewer2@example.com", "password": "another-long-password"},
            ).status_code
            == 200
        )
        r2 = c2.post(
            f"{API}/cases/{relabel_case['id']}/decision", json={"action": "keep", "note": "looks fine to me"}
        )
        assert r2.json()["case_status"] == "disputed"
        assert (
            c2.post(
                f"{API}/cases/{relabel_case['id']}/decision", json={"action": "keep", "adjudicate": True}
            ).status_code
            == 403
        )
    adj = client.post(
        f"{API}/cases/{relabel_case['id']}/decision",
        json={
            "action": "relabel",
            "target_class_id": target["id"],
            "adjudicate": True,
            "note": "adjudicated",
        },
    )
    assert adj.json()["case_status"] == "resolved"
    agree = client.get(f"{API}/audits/{audit['id']}/agreement").json()
    assert agree["comparisons"] >= 1 and "cohens_kappa" in agree

    # Remove decision + undo.
    remove_case = next(c for c in cases["items"] if c["id"] != relabel_case["id"])
    d = client.post(f"{API}/cases/{remove_case['id']}/decision", json={"action": "remove"}).json()
    assert client.post(f"{API}/decisions/{d['decision_id']}/undo").json()["case_status"] == "open"
    client.post(f"{API}/cases/{remove_case['id']}/decision", json={"action": "remove", "note": "unusable"})

    # Review budget.
    b = client.post(
        f"{API}/review-budget", json={"dataset_version_id": vid, "max_items": 10, "objective": "leakage"}
    ).json()
    assert 0 < b["selected_count"] <= 10 and b["session_id"]
    assert b["selected"][0]["reason"]
    bm = client.post(
        f"{API}/review-budget",
        json={"dataset_version_id": vid, "max_minutes": 5, "objective": "balanced", "create_session": False},
    ).json()
    assert bm["estimated_minutes"] <= 5

    # Counterfactuals and blame map.
    cf = client.get(f"{API}/cases/{first['id']}/counterfactuals").json()
    assert "qualitative" in cf
    fails = client.get(f"{API}/versions/{vid}/failures").json()["items"]
    if fails:
        bmap = client.get(f"{API}/failures/{fails[0]['id']}/blame-map").json()
        assert bmap["chain"][0]["step"] == "failure" and "disclaimer" in bmap

    # What-if experiment.
    w = client.post(
        f"{API}/what-if",
        json={
            "dataset_version_id": vid,
            "name": "jury + leakage",
            "actions": [
                {"type": "apply_jury_suggestions"},
                {"type": "move_leakage_out_of_eval"},
                {"type": "preserve_rare"},
            ],
            "seeds": 2,
        },
    )
    assert w.status_code == 200, w.text
    assert (
        client.post(
            f"{API}/what-if", json={"dataset_version_id": vid, "actions": [{"type": "nope"}]}
        ).status_code
        == 422
    )
    drain()
    wr = client.get(f"{API}/what-if/{w.json()['id']}").json()
    assert wr["status"] == "completed", wr
    assert "preserved_holdout" in wr["summary"]["deltas"]
    assert wr["summary"]["label"].startswith("Experimental result")
    after = next(r for r in wr["results"] if r["variant"] == "after" and r["eval_set"] == "preserved_holdout")
    assert after["n"] > 0 and after["metrics"]["per_class"] and "macro_f1" in after["metrics"]

    # Preflight, contract + CI gate with an API token.
    pf = client.get(f"{API}/versions/{vid}/preflight").json()
    assert pf["live"]["status"] in ("READY", "READY_WITH_WARNINGS", "BLOCKED")
    put = client.put(
        f"{API}/projects/{proj['id']}/contract",
        json={"rules": [{"metric": "min_images_per_class", "op": ">=", "value": 5}]},
    )
    assert put.status_code == 200
    tok = client.post(
        f"{API}/orgs/{org_id}/tokens", json={"name": "ci", "scopes": ["contracts:read"]}
    ).json()["token"]
    from fastapi.testclient import TestClient as TC

    with TC(create_app()) as anon:
        ci = anon.get(
            f"{API}/ci/contract",
            params={"dataset_version_id": vid},
            headers={"authorization": f"Bearer {tok}"},
        )
        assert ci.status_code == 200 and "passed" in ci.json()
        assert (
            anon.post(
                f"{API}/reports", json={"dataset_version_id": vid}, headers={"authorization": f"Bearer {tok}"}
            ).status_code
            == 403
        )

    # Report.
    rep = client.post(f"{API}/reports", json={"dataset_version_id": vid}).json()
    drain()
    rep = client.get(f"{API}/reports/{rep['id']}").json()
    assert rep["status"] == "completed"
    html = client.get(rep["html_url"])
    assert html.status_code == 200 and "DataCourt Audit Report" in html.text and "not a legal" in html.text

    # Export → new version (v2) → version comparison.
    ex = client.post(f"{API}/exports", json={"dataset_version_id": vid, "auto_audit": "fast"}).json()
    drain()
    exports = client.get(f"{API}/versions/{vid}/exports").json()
    assert exports[0]["status"] == "completed", exports
    assert exports[0]["summary"]["relabelled"] >= 1 and exports[0]["summary"]["removed"] >= 1
    assert client.get(f"{API}/exports/{ex['id']}/download").json()["url"]
    v2 = exports[0]["new_version_id"]
    drain()
    cmp_ = client.get(f"{API}/versions/{v2}/compare").json()
    assert cmp_["available"] and cmp_["labels_changed"]["count"] >= 1
    assert cmp_["to"]["samples"] == cmp_["from"]["samples"] - exports[0]["summary"]["removed"]

    # Deleting a reviewer's account keeps their decisions in this workspace (pseudonymised).
    with TestClient(create_app()) as c3:
        c3.headers.update({"x-datacourt-csrf": "1"})
        c3.post(
            f"{API}/auth/login", json={"email": "reviewer2@example.com", "password": "another-long-password"}
        )
        gone = c3.request("DELETE", f"{API}/auth/account", json={"confirm_email": "reviewer2@example.com"})
        assert gone.status_code == 200 and gone.json()["pseudonymised"] is True
        relogin = c3.post(
            f"{API}/auth/login", json={"email": "reviewer2@example.com", "password": "another-long-password"}
        )
        assert relogin.status_code == 401
    detail = client.get(f"{API}/cases/{relabel_case['id']}").json()
    assert detail["status"] == "resolved"
    assert any(d["reviewer"] == "Deleted user" for d in detail["decisions"])
    assert all(mb["name"] != "Reviewer Two" for mb in client.get(f"{API}/orgs/{org_id}").json()["members"])

    # Ledger integrity.
    led = client.get(f"{API}/orgs/{org_id}/ledger/verify").json()
    assert led["valid"] and led["checked"] > 5


def test_tenant_isolation(client, dataset_zip):
    a = register(client, "alice@example.com", "Alice")
    org_a = a["organizations"][0]["id"]
    proj = client.post(f"{API}/orgs/{org_a}/projects", json={"name": "Private"}).json()
    ds = client.post(f"{API}/projects/{proj['id']}/datasets", json={"name": "secret"}).json()
    client.post(f"{API}/auth/logout")
    b = register(client, "mallory@example.com", "Mallory")
    org_b = b["organizations"][0]["id"]
    assert client.get(f"{API}/projects/{proj['id']}").status_code == 404
    assert client.get(f"{API}/datasets/{ds['id']}").status_code == 404
    assert client.get(f"{API}/orgs/{org_a}/projects").status_code == 404
    assert client.post(f"{API}/projects/{proj['id']}/datasets", json={"name": "x"}).status_code == 404
    assert client.get(f"{API}/orgs/{org_b}/projects").status_code == 200


def test_auth_protections(client):
    assert client.get(f"{API}/auth/me").status_code == 401
    register(client, "csrf@example.com", "Csrf")
    r = client.post(f"{API}/orgs", json={"name": "no header"}, headers={"x-datacourt-csrf": ""})
    assert r.status_code == 403
    r = client.post(f"{API}/orgs", json={"name": "evil origin"}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    client.post(f"{API}/auth/logout")
    assert (
        client.post(
            f"{API}/auth/login", json={"email": "csrf@example.com", "password": "wrong-password"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            f"{API}/auth/register", json={"email": "short@example.com", "password": "short", "name": "S"}
        ).status_code
        == 422
    )
