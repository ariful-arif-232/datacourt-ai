"""Response serializers. Object keys are never returned: full images use short-lived signed URLs,
thumbnails a CDN-cacheable capability URL (see `storage.media_url`)."""

from __future__ import annotations

from typing import Any

from datacourt.db import models as m
from datacourt.storage import browser_copy_key_for, get_store, media_url


def iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def sample_view(x: m.Sample, *, full: bool = False) -> dict[str, Any]:
    out = {
        "id": str(x.id),
        "idx": x.idx,
        "path": x.relative_path,
        "name": x.relative_path.rsplit("/", 1)[-1],
        "label": x.label,
        "class_id": str(x.class_id),
        "split": str(x.split),
        "width": x.width,
        "height": x.height,
        "format": x.format,
        "thumb_url": media_url(x.thumb_key) if x.thumb_key else None,
    }
    if full:
        store = get_store()
        # Formats browsers cannot display (HEIC/HEIF) are viewed through their WebP copy; the
        # original is always downloadable under its own name.
        viewable = (x.attributes or {}).get("browser_copy")
        out.update(
            {
                "image_url": store.signed_get_url(browser_copy_key_for(x.storage_key, x.sha256))
                if viewable
                else store.signed_get_url(x.storage_key),
                "original_url": store.signed_get_url(x.storage_key, filename=out["name"]),
                "sha256": x.sha256,
                "phash": x.phash,
                "mode": x.mode,
                "byte_size": x.byte_size,
                "attributes": x.attributes,
                "provenance": x.provenance,
            }
        )
    return out


def version_view(v: m.DatasetVersion) -> dict[str, Any]:
    return {
        "id": str(v.id),
        "dataset_id": str(v.dataset_id),
        "version_number": v.version_number,
        "parent_version_id": str(v.parent_version_id) if v.parent_version_id else None,
        "origin": v.origin,
        "status": str(v.status),
        "source_filename": v.source_filename,
        "source_sha256": v.source_sha256,
        "source_bytes": v.source_bytes,
        "manifest_sha256": v.manifest_sha256,
        "layout": v.layout,
        "stats": v.stats,
        "notes": v.notes,
        "error": v.error,
        "created_at": iso(v.created_at),
        "ingested_at": iso(v.ingested_at),
    }


def audit_view(a: m.AuditRun, steps: list[m.AuditPipelineStep] | None = None) -> dict[str, Any]:
    out = {
        "id": str(a.id),
        "dataset_version_id": str(a.dataset_version_id),
        "profile": str(a.profile),
        "status": str(a.status),
        "progress": round(a.progress, 4),
        "current_stage": a.current_stage,
        "config_hash": a.config_hash,
        "embedding_model": a.embedding_model,
        "algorithm_versions": a.algorithm_versions,
        "warnings": a.warnings,
        "summary": a.summary,
        "error": a.error,
        "created_at": iso(a.created_at),
        "started_at": iso(a.started_at),
        "finished_at": iso(a.finished_at),
    }
    if steps is not None:
        out["steps"] = [
            {
                "stage": st.stage,
                "order": st.order,
                "status": str(st.status),
                "attempts": st.attempts,
                "started_at": iso(st.started_at),
                "finished_at": iso(st.finished_at),
                "duration_ms": st.duration_ms,
                "warnings": st.warnings,
                "error": st.error,
                "output": st.output,
            }
            for st in sorted(steps, key=lambda x: x.order)
        ]
    return out


def case_summary(c: m.CourtCase, sample: m.Sample | None = None) -> dict[str, Any]:
    out = {
        "id": str(c.id),
        "case_number": c.case_number,
        "verdict": str(c.verdict),
        "priority": round(c.priority_score, 4),
        "impact": round(c.impact_score, 4),
        "uncertainty": str(c.uncertainty),
        "reason_codes": c.reason_codes,
        "categories": c.categories,
        "primary_concern": c.primary_concern,
        "status": str(c.status),
        "est_review_minutes": c.est_review_minutes,
        "family_id": str(c.family_id) if c.family_id else None,
        "created_at": iso(c.created_at),
        "updated_at": iso(c.updated_at),
    }
    if sample is not None:
        out["sample"] = sample_view(sample)
    return out
