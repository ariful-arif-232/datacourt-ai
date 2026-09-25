"""Signed object access, CI data-contract gate, health."""

from __future__ import annotations

import mimetypes
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from datacourt.api.deps import current_principal, rate_limit
from datacourt.config import get_settings
from datacourt.db import models as m
from datacourt.db.session import get_db
from datacourt.ml import governance as gov
from datacourt.pipeline.facts import collect_facts, enrich_with_governance
from datacourt.security import verify_signed_payload
from datacourt.storage import LocalObjectStore, ObjectNotFound, TooLarge, get_store
from datacourt.tenancy import Principal, get_version, resolve_audit

router = APIRouter(tags=["system"])


@router.get("/health")
def health(s: Session = Depends(get_db)) -> JSONResponse:
    settings = get_settings()
    body: dict = {
        "ok": True,
        "configured": True,
        "version": "0.1.0",
        "storage": settings.storage_backend,
        "execution": settings.execution_backend,
        "embedding_backend": settings.embedding_backend,
        "gemini": bool(settings.gemini_api_key),
    }
    try:
        s.execute(text("select 1"))
        body["database"] = "ok"
    except Exception:  # noqa: BLE001 - report, don't crash, when the database is unreachable
        body.update(ok=False, database="unreachable")
        return JSONResponse(body, status_code=503)
    return JSONResponse(body)


@router.get("/algorithms")
def algorithms() -> dict:
    """Public registry of versioned algorithms and embedding backends (no tenant data)."""
    from datacourt.registry import ALGORITHMS, EMBEDDING_BACKENDS

    settings = get_settings()
    return {
        "algorithms": ALGORITHMS,
        "embedding_backends": EMBEDDING_BACKENDS,
        "configured_embedding_backend": settings.embedding_backend,
        "llm": {
            "provider": "gemini" if settings.gemini_api_key else None,
            "model": settings.gemini_model if settings.gemini_api_key else None,
            "role": "Plain-language explanations only; never scores, verdicts or evidence.",
        },
    }


@router.get("/jobs/{job_id}")
def job_status(
    job_id: uuid.UUID, p: Principal = Depends(current_principal), s: Session = Depends(get_db)
) -> dict:
    """Execution status of a background job: queued, where it runs, retries, failure reason."""
    from datacourt import execution
    from datacourt.tenancy import not_found, require_org

    job = s.get(m.Job, job_id)
    if job is None or job.org_id is None:
        raise not_found()
    require_org(s, p, job.org_id)
    if str(job.status) in ("queued", "running"):
        execution.maybe_redispatch()
        s.refresh(job)
    return execution.status_for(job) or {}


@router.get("/media/{token}", include_in_schema=False)
def media(token: str, request: Request) -> Response:
    """Thumbnails by capability URL (`storage.media_url`), cacheable by browsers and the CDN.

    Thumbnails are immutable and content-addressed, and the URL is stable for a whole period, so
    repeat views are served by the CDN without an object-store download. Query strings are refused
    so the URL cannot be varied to bypass the cache.
    """
    no_store = {"Cache-Control": "no-store"}
    if request.url.query:
        raise HTTPException(status_code=400, detail="unexpected query string", headers=no_store)
    payload = verify_signed_payload(token, get_settings().signed_url_secret)
    key = str(payload.get("k", "")) if payload else ""
    if payload is None or payload.get("m") != "MEDIA" or "/thumbs/" not in key:
        raise HTTPException(status_code=403, detail="invalid or expired link", headers=no_store)
    try:
        data = get_store().get_bytes(key)
    except ObjectNotFound as exc:
        raise HTTPException(
            status_code=404, detail="not found", headers={"Cache-Control": "public, max-age=60"}
        ) from exc
    # Never let a cache keep the thumbnail past the capability's expiry, and at most a day, which
    # also bounds how long a deleted dataset's thumbnails can linger in the CDN.
    max_age = max(0, min(86400, int(float(payload["exp"]) - time.time())))
    return Response(
        data,
        media_type=mimetypes.guess_type(key)[0] or "application/octet-stream",
        headers={
            "Cache-Control": f"public, max-age={max_age}, s-maxage={max_age}, immutable",
            "Content-Security-Policy": "default-src 'none'",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/storage/object/{token}", include_in_schema=False)
def get_object(token: str) -> Response:
    payload = verify_signed_payload(token, get_settings().signed_url_secret)
    if payload is None or payload.get("m") != "GET":
        raise HTTPException(status_code=403, detail="invalid or expired link")
    store = get_store()
    if not isinstance(store, LocalObjectStore):
        raise HTTPException(status_code=404, detail="not found")
    key = payload["k"]
    if not store.exists(key):
        raise HTTPException(status_code=404, detail="not found")
    ctype = mimetypes.guess_type(key)[0] or "application/octet-stream"
    headers = {"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"}
    if ctype.startswith("text/html"):
        # Reports are self-contained; never allow them to run scripts or be framed.
        headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'none'"
        )
    if payload.get("fn"):
        return FileResponse(
            store.local_path(key), media_type=ctype, filename=str(payload["fn"]), headers=headers
        )
    return FileResponse(store.local_path(key), media_type=ctype, headers=headers)


@router.put(
    "/storage/upload/{token}", include_in_schema=False, dependencies=[Depends(rate_limit("upload", 30))]
)
async def put_object(token: str, request: Request) -> dict:
    payload = verify_signed_payload(token, get_settings().signed_url_secret)
    if payload is None or payload.get("m") != "PUT":
        raise HTTPException(status_code=403, detail="invalid or expired upload link")
    store = get_store()
    if not isinstance(store, LocalObjectStore):
        raise HTTPException(status_code=404, detail="not found")
    import tempfile

    limit = get_settings().max_upload_bytes
    total = 0
    with tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024) as tmp:
        async for chunk in request.stream():
            total += len(chunk)
            if total > limit:
                raise HTTPException(status_code=413, detail="upload too large")
            tmp.write(chunk)
        tmp.seek(0)
        try:
            store.put_stream(payload["k"], tmp, limit)
        except TooLarge as exc:
            raise HTTPException(status_code=413, detail="upload too large") from exc
    return {"ok": True, "bytes": total}


@router.get("/ci/contract", dependencies=[Depends(rate_limit("ci", 120))])
def ci_contract(
    dataset_version_id: uuid.UUID, p: Principal = Depends(current_principal), s: Session = Depends(get_db)
) -> dict:
    """Machine-readable data-contract result for CI pipelines (use an API token with contracts:read)."""
    if p.token is not None and not (
        {"contracts:read", "audits:read", "audits:write"} & set(p.token.scopes or [])
    ):
        raise HTTPException(status_code=403, detail="token lacks contracts:read")
    v = get_version(s, p, dataset_version_id)
    a = resolve_audit(s, v, None)
    ds = s.get(m.Dataset, v.dataset_id)
    assert ds is not None
    facts = collect_facts(s, a)
    pf = gov.preflight(facts, a.config["preflight"])
    stored = s.scalar(
        select(m.PreflightResult)
        .where(m.PreflightResult.audit_run_id == a.id)
        .order_by(m.PreflightResult.created_at.desc())
        .limit(1)
    )
    effective = stored.override_status if stored and stored.override_status else pf["status"]
    debt = gov.compute_debt(facts)
    contract = s.scalar(
        select(m.DataContract)
        .where(m.DataContract.project_id == ds.project_id, m.DataContract.is_active.is_(True))
        .order_by(m.DataContract.version.desc())
        .limit(1)
    )
    rules = contract.rules if contract else gov.DEFAULT_CONTRACT
    result = gov.evaluate_contract(rules, enrich_with_governance(facts, debt["overall"], effective))
    return {
        "passed": result["passed"] and effective != "BLOCKED",
        "contract": {
            "name": contract.name if contract else "DataCourt default contract",
            "version": contract.version if contract else 0,
            "results": result["results"],
            "passed": result["passed"],
        },
        "preflight": {
            "status": pf["status"],
            "effective_status": effective,
            "blocking": [c["id"] for c in pf["checks"] if c["status"] == "block"],
        },
        "debt": debt["overall"],
        "dataset_version_id": str(v.id),
        "audit_run_id": str(a.id),
        "config_hash": a.config_hash,
    }
