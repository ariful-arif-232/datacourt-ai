"""Evaluation contamination check (`contamination-v2`).

Compares a dataset version against another version the same organization owns (for
example an internal benchmark). Never reaches outside the user's authorized data.
Exact overlap via SHA-256 or identical decoded pixels (the same picture in another format, such
as a HEIC and a lossless PNG of it; v2); near overlap via embedding kNN (same backend only),
verified with the same detail-layer structural correlation used for duplicate families.
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import select

from datacourt import algorithms, jobs
from datacourt.db import models as m
from datacourt.db.session import new_session
from datacourt.enums import RunStatus
from datacourt.ml.duplicates import structural_similarity
from datacourt.ml.knn import knn
from datacourt.pipeline.config import backend_thresholds
from datacourt.pipeline.context import AuditContext, load_samples
from datacourt.services.versions import latest_completed_audit

VERSION = algorithms.CONTAMINATION


def run_contamination_job(ctx) -> dict:
    check_id = uuid.UUID(ctx.payload["check_id"])
    with new_session() as s:
        chk = s.get(m.ContaminationCheck, check_id)
        if chk is None:
            raise jobs.PermanentJobError("check not found")
        chk.status = RunStatus.RUNNING
        s.commit()
        src_v, ref_v = (
            s.get(m.DatasetVersion, chk.source_version_id),
            s.get(m.DatasetVersion, chk.reference_version_id),
        )
        assert src_v is not None and ref_v is not None
        if src_v.org_id != ref_v.org_id:
            raise jobs.PermanentJobError("versions belong to different organizations")
        a_src, a_ref = latest_completed_audit(s, src_v.id), latest_completed_audit(s, ref_v.id)
        e_src = (
            s.scalar(select(m.EmbeddingMetadata).where(m.EmbeddingMetadata.audit_run_id == a_src.id))
            if a_src
            else None
        )
        e_ref = (
            s.scalar(select(m.EmbeddingMetadata).where(m.EmbeddingMetadata.audit_run_id == a_ref.id))
            if a_ref
            else None
        )
    S, R = load_samples(src_v.id), load_samples(ref_v.id)
    ref_sha: dict[str, int] = {}
    for j, h in enumerate(R.sha):
        ref_sha.setdefault(h, j)
    ref_pixels: dict[str, int] = {}
    for j, mm in enumerate(R.meta):
        if px := mm["attributes"].get("pixel_sha256"):
            ref_pixels.setdefault(px, j)
    exact: list[tuple[int, int]] = []
    for i, h in enumerate(S.sha):
        px = S.meta[i]["attributes"].get("pixel_sha256")
        j = ref_sha.get(h, ref_pixels.get(px) if px else None)
        if j is not None:
            exact.append((i, j))
    result: dict = {
        "algorithm": VERSION,
        "source_samples": S.n,
        "reference_samples": R.n,
        "exact_overlap": len(exact),
        "exact_by_split": dict(Counter(S.split[i] for i, _ in exact)),
        "exact_examples": [{"source": S.paths[i], "reference": R.paths[j]} for i, j in exact[:25]],
    }
    near: list[dict] = []
    if a_src and a_ref and e_src and e_ref and e_src.model_name == e_ref.model_name:
        cs = AuditContext(
            a_src.id, a_src.org_id, src_v.id, src_v.dataset_id, str(a_src.profile), a_src.config, S
        )
        cr = AuditContext(
            a_ref.id, a_ref.org_id, ref_v.id, ref_v.dataset_id, str(a_ref.profile), a_ref.config, R
        )
        th = backend_thresholds(a_src.config, e_src.model_name)
        es, er = cs.load_npz("embeddings")["emb"], cr.load_npz("embeddings")["emb"]
        ts, tr = cs.load_npz("structure")["thumbs"], cr.load_npz("structure")["thumbs"]
        sims, idx = knn(es, er, 3)
        exact_set = {i for i, _ in exact}
        for i in range(S.n):
            if i in exact_set:
                continue
            for c in range(idx.shape[1]):
                j = int(idx[i, c])
                if j < 0 or sims[i, c] < th["candidate_min_cosine"]:
                    continue
                st = structural_similarity(ts[i], tr[j])
                if max(st["detail_ncc"], st["detail_ncc_mirror"]) >= th["struct_min_detail_ncc"]:
                    near.append(
                        {
                            "source": S.paths[i],
                            "reference": R.paths[j],
                            "cosine": round(float(sims[i, c]), 4),
                            "split": S.split[i],
                            **st,
                        }
                    )
                    break
            if i % 500 == 0:
                ctx.report(0.9 * i / max(1, S.n), "CONTAMINATION")
        result["near_duplicate_method"] = (
            f"{e_src.model_name} kNN + detail-layer NCC ≥ {th['struct_min_detail_ncc']}"
        )
    else:
        result["near_duplicate_method"] = (
            "unavailable (both versions need a completed audit with the same embedding backend)"
        )
    result["near_overlap"] = len(near)
    result["near_examples"] = near[:50]
    result["near_by_split"] = dict(Counter(x["split"] for x in near))
    total = len(exact) + len(near)
    result["contaminated_fraction"] = round(total / max(1, S.n), 5)
    result["verdict"] = (
        "clean" if total == 0 else "contaminated" if total / max(1, S.n) > 0.01 else "minor_overlap"
    )
    with new_session() as s:
        chk = s.get(m.ContaminationCheck, check_id)
        assert chk is not None
        chk.status = RunStatus.COMPLETED
        chk.result = {**result, "finished_at": datetime.now(UTC).isoformat()}
        s.commit()
    return {"check_id": str(check_id), "overlap": total}
