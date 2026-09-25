"""Audit pipeline stages. Each stage is idempotent: it clears its own outputs for the audit
before writing, and persists artifacts so later stages (and re-runs) do not recompute."""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from typing import Any

import cv2
import numpy as np
from sqlalchemy import delete, insert, select

from datacourt import ledger
from datacourt.config import get_settings
from datacourt.db import models as m
from datacourt.db.session import new_session
from datacourt.enums import CaseStatus, EvidenceKind, Severity, Stance, Witness
from datacourt.ml import attributes as attrs_mod
from datacourt.ml import baseline as bl
from datacourt.ml import coverage as cov
from datacourt.ml import dna as dna_mod
from datacourt.ml import duplicates as dup
from datacourt.ml import governance as gov
from datacourt.ml import influence as infl
from datacourt.ml import jury
from datacourt.ml import labels as lab
from datacourt.ml import leakage as lk
from datacourt.ml import privacy as priv
from datacourt.ml import quality as qual
from datacourt.ml import shortcuts as sc
from datacourt.ml.embeddings import backend_id, load_backend
from datacourt.ml.knn import knn
from datacourt.pipeline.config import backend_thresholds
from datacourt.pipeline.context import AuditContext
from datacourt.pipeline.facts import collect_facts, enrich_with_governance


class StageSkipped(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _bulk(s, model, rows: list[dict], chunk: int = 2000) -> None:
    for i in range(0, len(rows), chunk):
        if rows[i : i + chunk]:
            s.execute(insert(model), rows[i : i + chunk])


def _clear(s, model, audit_id: uuid.UUID) -> None:
    s.execute(delete(model).where(model.audit_run_id == audit_id))


def _fit_eval_masks(ctx: AuditContext) -> tuple[np.ndarray, np.ndarray]:
    fit = ctx.samples.mask("train", "unsplit")
    ev = ctx.samples.mask("val", "test")
    return fit, ev


# ---------------------------------------------------------------------------
def stage_ingesting(ctx: AuditContext) -> dict:
    with new_session() as s:
        v = s.get(m.DatasetVersion, ctx.version_id)
        assert v is not None
        return {
            "samples": ctx.samples.n,
            "classes": ctx.samples.n_classes,
            "manifest_sha256": v.manifest_sha256,
            "source_sha256": v.source_sha256,
        }


def stage_validating(ctx: AuditContext) -> dict:
    with new_session() as s:
        v = s.get(m.DatasetVersion, ctx.version_id)
        assert v is not None
        stats, layout = v.stats or {}, v.layout or {}
    warnings = list(layout.get("warnings", []))
    if ctx.samples.n_classes < 2:
        warnings.append("Fewer than two classes: label forensics and model evidence will be limited.")
    files = stats.get("files", {})
    if int(files.get("rejected", 0)):
        warnings.append(
            f"{files['rejected']} file(s) were rejected during ingestion (unreadable, unsupported or unsafe)."
        )
    splits = sorted(set(ctx.samples.split))
    if splits == ["unsplit"]:
        warnings.append("No train/val/test folders detected; model evidence uses cross-validation only.")
    elif "train" not in splits:
        warnings.append("No training split detected.")
    ctx.warnings.extend(warnings)
    return {
        "layout": layout.get("kind"),
        "splits": splits,
        "warnings": warnings,
        "rejected_files": int(files.get("rejected", 0)),
        "ignored_files": int(files.get("ignored", 0)),
    }


def stage_profiling(ctx: AuditContext) -> dict:
    pcfg = ctx.config["privacy"]
    do_privacy = bool(pcfg["enabled"])
    max_side = int(ctx.config["profiling"]["decode_max_side"])

    def work(i: int, rgb: np.ndarray) -> tuple[dict, list, np.ndarray]:
        a = attrs_mod.compute_attributes(rgb, int(ctx.samples.width[i]), int(ctx.samples.height[i]))
        p = priv.scan(rgb, pcfg) if do_privacy else []
        return a, p, dup.structure_thumb(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY))

    results = ctx.map_images(work, list(range(ctx.samples.n)), max_side=max_side)
    attrs = [r[0] for r in results]
    privacy = {i: r[1] for i, r in enumerate(results) if r[1]}
    ctx.save_json("attributes", attrs)
    ctx.save_npz("structure", thumbs=np.stack([r[2] for r in results]))
    ctx.save_json("privacy", privacy)
    with new_session() as s:
        _clear(s, m.SampleQualityMetrics, ctx.audit_id)
        _bulk(
            s,
            m.SampleQualityMetrics,
            [
                {
                    "id": uuid.uuid4(),
                    "audit_run_id": ctx.audit_id,
                    "sample_id": ctx.samples.ids[i],
                    "metrics": a,
                }
                for i, a in enumerate(attrs)
            ],
        )
        s.commit()
    summary = {
        k: round(float(np.median([a[k] for a in attrs])), 3)
        for k in ("brightness", "contrast", "blur_laplacian_var", "saturation")
    }
    return {
        "attribute_version": attrs_mod.VERSION,
        "medians": summary,
        "grayscale_fraction": round(float(np.mean([a["is_grayscale"] for a in attrs])), 4),
        "privacy_flagged": len(privacy),
        "privacy_scan": "enabled" if do_privacy else "disabled",
    }


def stage_quality(ctx: AuditContext) -> dict:
    attrs = ctx.load_json("attributes")
    findings = qual.evaluate_quality(attrs, ctx.samples.meta, ctx.config["quality"])
    privacy = {int(k): v for k, v in ctx.load_json("privacy").items()}
    with new_session() as s:
        _clear(s, m.QualityFinding, ctx.audit_id)
        _clear(s, m.PrivacyFinding, ctx.audit_id)
        _bulk(
            s,
            m.QualityFinding,
            [
                {
                    "id": uuid.uuid4(),
                    "audit_run_id": ctx.audit_id,
                    "sample_id": ctx.samples.ids[f.sample_index],
                    "finding_type": f.finding_type,
                    "severity": f.severity,
                    "measured_value": f.measured_value,
                    "threshold": f.threshold,
                    "comparator": f.comparator,
                    "rule_version": qual.RULE_VERSION,
                    "deterministic": f.deterministic,
                    "description": f.description,
                }
                for f in findings
            ],
        )
        _bulk(
            s,
            m.PrivacyFinding,
            [
                {
                    "id": uuid.uuid4(),
                    "audit_run_id": ctx.audit_id,
                    "sample_id": ctx.samples.ids[i],
                    "kind": p["kind"],
                    "count": p["count"],
                    "score": p["score"],
                    "boxes": p["boxes"],
                }
                for i, ps in privacy.items()
                for p in ps
            ],
        )
        s.commit()
    by_type = Counter(f.finding_type for f in findings)
    by_sev = Counter(str(f.severity) for f in findings)
    affected = len({f.sample_index for f in findings if f.severity in (Severity.MEDIUM, Severity.HIGH)})
    return {
        "rule_version": qual.RULE_VERSION,
        "findings": len(findings),
        "by_type": dict(by_type),
        "by_severity": dict(by_sev),
        "samples_medium_plus": affected,
        "privacy_findings": sum(len(v) for v in privacy.values()),
    }


def stage_embedding(ctx: AuditContext) -> dict:
    settings = get_settings()
    backend, warns = load_backend(settings.embedding_backend, settings.model_cache_dir)
    ctx.warnings.extend(warns)
    bs = int(ctx.config["embedding"]["batch_size"])
    side = 256 if backend.name == "dc-descriptor" else 320
    n = ctx.samples.n
    out = np.zeros((n, backend.dim), dtype=np.float32)
    for s0 in range(0, n, bs):
        idx = list(range(s0, min(n, s0 + bs)))
        imgs = ctx.map_images(lambda i, rgb: rgb, idx, max_side=side, chunk=bs)
        out[s0 : s0 + len(idx)] = backend.embed(imgs)
        if ctx.report:
            ctx.report(0.9 * min(1.0, (s0 + len(idx)) / max(1, n)))
    ctx.save_npz("embeddings", emb=out)
    k = int(ctx.config["knn"]["k"])
    sims, nn = knn(out, out, k, exclude_self=True)
    ctx.save_npz("knn", sims=sims, idx=nn)
    with new_session() as s:
        _clear(s, m.EmbeddingMetadata, ctx.audit_id)
        s.add(
            m.EmbeddingMetadata(
                audit_run_id=ctx.audit_id,
                dataset_version_id=ctx.version_id,
                model_name=backend.name,
                model_version=backend.version,
                preprocessing_version=backend.preprocessing_version,
                dim=backend.dim,
                count=n,
                object_key=ctx._key("embeddings.npz"),
            )
        )
        a = s.get(m.AuditRun, ctx.audit_id)
        assert a is not None
        a.embedding_model = backend_id(backend)
        existing = s.scalar(
            select(m.SystemModelVersion).where(
                m.SystemModelVersion.name == backend.name, m.SystemModelVersion.version == backend.version
            )
        )
        if existing is None:
            s.add(
                m.SystemModelVersion(
                    name=backend.name,
                    version=backend.version,
                    source=backend.source,
                    dim=backend.dim,
                    preprocessing_version=backend.preprocessing_version,
                )
            )
        s.commit()
    return {
        "backend": backend.name,
        "backend_version": backend.version,
        "dim": backend.dim,
        "count": n,
        "knn_k": k,
        "median_nn_similarity": round(float(np.median(sims[:, 0])) if n > 1 else 0.0, 4),
    }


def _backend_name(ctx: AuditContext) -> str:
    with new_session() as s:
        e = s.scalar(select(m.EmbeddingMetadata).where(m.EmbeddingMetadata.audit_run_id == ctx.audit_id))
        return e.model_name if e else "dc-descriptor"


def stage_duplicates(ctx: AuditContext) -> dict:
    emb = ctx.load_npz("embeddings")["emb"]
    kn = ctx.load_npz("knn")
    attrs = ctx.load_json("attributes")
    th = backend_thresholds(ctx.config, _backend_name(ctx))
    S = ctx.samples
    edges, fams = dup.find_duplicates(
        sha=S.sha,
        ph=S.phash,
        ph_flip=S.phash_flip,
        width=S.width,
        height=S.height,
        brightness=np.array([a["brightness"] for a in attrs]),
        saturation=np.array([a["saturation"] for a in attrs]),
        emb=emb,
        struct=ctx.load_npz("structure")["thumbs"],
        knn_sims=kn["sims"],
        knn_idx=kn["idx"],
        cfg=th,
        verifier=dup.KeypointVerifier(lambda i: cv2.cvtColor(ctx.image(i, 256), cv2.COLOR_RGB2GRAY)),
        pixel=[mm["attributes"].get("pixel_sha256") for mm in S.meta],
    )
    labels = [S.class_names[c] for c in S.labels]
    membership: dict[int, dict] = {}
    with new_session() as s:
        _clear(s, m.SimilarityEdge, ctx.audit_id)
        s.execute(
            delete(m.DuplicateFamilyMember).where(
                m.DuplicateFamilyMember.family_id.in_(
                    select(m.DuplicateFamily.id).where(m.DuplicateFamily.audit_run_id == ctx.audit_id)
                )
            )
        )
        s.execute(delete(m.LeakageFinding).where(m.LeakageFinding.audit_run_id == ctx.audit_id))
        _clear(s, m.DuplicateFamily, ctx.audit_id)
        s.flush()
        fam_rows, mem_rows, edge_rows = [], [], []
        for fi, f in enumerate(fams):
            fid = uuid.uuid4()
            fam_labels = sorted({labels[i] for i in f.members})
            fam_splits = sorted({S.split[i] for i in f.members})
            fam_rows.append(
                {
                    "id": fid,
                    "audit_run_id": ctx.audit_id,
                    "family_number": fi + 1,
                    "root_sample_id": S.ids[f.root],
                    "size": len(f.members),
                    "kind": f.kind,
                    "splits": fam_splits,
                    "labels": fam_labels,
                    "crosses_splits": len(set(fam_splits) - {"unsplit"}) >= 2,
                    "label_conflict": len(fam_labels) > 1,
                    "max_similarity": round(max(e.cosine for e in f.edges), 5),
                }
            )
            for i in f.members:
                pe = f.parent_edge.get(i)
                mem_rows.append(
                    {
                        "id": uuid.uuid4(),
                        "family_id": fid,
                        "sample_id": S.ids[i],
                        "parent_sample_id": S.ids[f.parent[i]] if f.parent.get(i) is not None else None,
                        "relation": str(pe.relation) if pe else None,
                        "similarity": round(pe.cosine, 5) if pe else None,
                        "phash_distance": pe.phash_distance if pe else None,
                        "depth": f.depth.get(i, 0),
                        "evidence": pe.evidence if pe else {"role": "source-like (highest resolution)"},
                    }
                )
                membership[i] = {
                    "family_index": fi,
                    "number": fi + 1,
                    "size": len(f.members),
                    "kind": f.kind,
                    "is_root": i == f.root,
                    "relation": str(pe.relation) if pe else None,
                    "label_conflict": len(fam_labels) > 1,
                    "labels": fam_labels,
                    "splits": fam_splits,
                    "members": list(f.members),
                }
            for e in f.edges:
                edge_rows.append(
                    {
                        "id": uuid.uuid4(),
                        "audit_run_id": ctx.audit_id,
                        "sample_a_id": S.ids[e.a],
                        "sample_b_id": S.ids[e.b],
                        "cosine": round(e.cosine, 5),
                        "phash_distance": e.phash_distance,
                        "relation": e.relation,
                        "family_id": fid,
                    }
                )
        _bulk(s, m.DuplicateFamily, fam_rows)
        _bulk(s, m.DuplicateFamilyMember, mem_rows)
        _bulk(s, m.SimilarityEdge, edge_rows)
        s.commit()
    ctx.save_json("dup_membership", {str(k): v for k, v in membership.items()})
    ctx.save_json(
        "dup_families",
        [
            {
                "members": f.members,
                "root": f.root,
                "kind": f.kind,
                "edges": [[e.a, e.b, str(e.relation), e.cosine] for e in f.edges],
            }
            for f in fams
        ],
    )
    rel = Counter(str(e.relation) for e in edges)
    return {
        "algorithm": dup.VERSION,
        "thresholds": th,
        "edges": len(edges),
        "families": len(fams),
        "samples_in_families": len(membership),
        "redundant_samples": sum(len(f.members) - 1 for f in fams),
        "relations": dict(rel),
        "exact_families": sum(1 for f in fams if f.kind == "exact"),
        "label_conflict_families": sum(1 for r in fam_rows if r["label_conflict"]),
    }


def _load_families(ctx: AuditContext) -> list[dup.Family]:
    raw = ctx.load_json("dup_families")
    out = []
    for f in raw:
        edges = [dup.Edge(a, b, dup.Relation(r), c, 0, {}) for a, b, r, c in f["edges"]]
        out.append(
            dup.Family(
                members=f["members"],
                root=f["root"],
                parent={},
                parent_edge={},
                depth={},
                edges=edges,
                kind=f["kind"],
            )
        )
    return out


def stage_leakage(ctx: AuditContext) -> dict:
    S = ctx.samples
    fams = _load_families(ctx)
    emb = ctx.load_npz("embeddings")["emb"]
    kn = ctx.load_npz("knn")
    th = backend_thresholds(ctx.config, _backend_name(ctx))
    labels = [S.class_names[c] for c in S.labels]
    leaks = lk.family_leaks(fams, S.split, labels)
    in_family = {i for f in fams for i in f.members}
    outside = [i for i in range(S.n) if i not in in_family]
    q = (
        float(np.quantile(kn["sims"][outside, 0], ctx.config["leakage"]["similar_subject_quantile"]))
        if outside
        else 1.0
    )
    subj_threshold = max(th["similar_subject_cosine"], q)
    subj = lk.similar_subject_clusters(
        kn["sims"],
        kn["idx"],
        S.split,
        in_family,
        subj_threshold,
        1.0001,
        ctx.config["leakage"]["max_similar_subject_clusters"],
    )
    integrity = lk.split_integrity(S.split, labels, emb, fams, S.sha, th)
    rank = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    membership: dict[int, dict] = {}
    with new_session() as s:
        _clear(s, m.LeakageFinding, ctx.audit_id)
        _clear(s, m.SplitIntegrityMetrics, ctx.audit_id)
        fam_ids = dict(
            s.execute(
                select(m.DuplicateFamily.family_number, m.DuplicateFamily.id).where(
                    m.DuplicateFamily.audit_run_id == ctx.audit_id
                )
            ).all()
        )
        rows = []
        for leak in leaks + subj:
            rows.append(
                {
                    "id": uuid.uuid4(),
                    "audit_run_id": ctx.audit_id,
                    "family_id": fam_ids.get(leak.family_index + 1)
                    if leak.family_index is not None
                    else None,
                    "kind": leak.kind,
                    "risk": leak.risk,
                    "splits_crossed": leak.splits,
                    "sample_ids": [str(S.ids[i]) for i in leak.members],
                    "max_similarity": leak.max_similarity,
                    "explanation": leak.explanation,
                    "evidence": leak.evidence,
                }
            )
            for i in leak.members:
                cur = membership.get(i)
                if cur is None or rank[str(leak.risk)] > rank[cur["risk"]]:
                    membership[i] = {"risk": str(leak.risk), "kind": str(leak.kind), "splits": leak.splits}
        _bulk(s, m.LeakageFinding, rows)
        s.add(m.SplitIntegrityMetrics(audit_run_id=ctx.audit_id, metrics=integrity))
        s.commit()
    ctx.save_json("leak_membership", {str(k): v for k, v in membership.items()})
    return {
        "algorithm": lk.VERSION,
        "family_leaks": len(leaks),
        "similar_subject_clusters": len(subj),
        "similar_subject_threshold": round(subj_threshold, 4),
        "by_risk": dict(Counter(str(x.risk) for x in leaks + subj)),
        "integrity": integrity,
    }


def stage_baseline(ctx: AuditContext) -> dict:
    S = ctx.samples
    fit, ev = _fit_eval_masks(ctx)
    C = S.n_classes
    if C < 2 or len(set(S.labels[fit].tolist())) < 2:
        raise StageSkipped("need at least two classes in the training data")
    emb = ctx.load_npz("embeddings")["emb"].astype(np.float64)
    bc = ctx.config["baseline"]
    tcfg = bl.TrainConfig(
        epochs=bc["epochs"],
        batch_size=bc["batch_size"],
        lr=bc["lr"],
        momentum=bc["momentum"],
        weight_decay=bc["weight_decay"],
        class_balanced=bc["class_balanced"],
        seed=ctx.config["seed"],
        checkpoint_every=bc["checkpoint_every"],
    )
    folds = bc["folds_deep"] if ctx.deep else bc["folds_fast"]
    fit_idx = np.nonzero(fit)[0]
    ev_idx = np.nonzero(ev)[0]
    y_fit = S.labels[fit_idx]
    lr_C, balanced = float(bc["C"]), bool(bc["class_balanced"])
    # Evidence and metrics come from the converged (seed-free) logistic head.
    oof_logits, fold_id = bl.out_of_fold_logits_lr(
        emb[fit_idx], y_fit, C, C=lr_C, class_balanced=balanced, k=folds, seed=ctx.config["seed"]
    )
    if ctx.report:
        ctx.report(0.5)
    T = bl.fit_temperature(oof_logits, y_fit)
    oof_raw = bl.softmax(oof_logits)
    oof_probs = bl.softmax(oof_logits / T)
    head = bl.fit_logreg(emb[fit_idx], y_fit, C, C=lr_C, class_balanced=balanced)
    if ctx.report:
        ctx.report(0.8)
    # The SGD run is kept for its trajectory: training dynamics and TracIn checkpoints.
    full = bl.train_softmax(emb[fit_idx], y_fit, C, tcfg, record_dynamics=True, record_checkpoints=True)
    probs = np.zeros((S.n, C))
    probs[fit_idx] = oof_probs
    if len(ev_idx):
        probs[ev_idx] = bl.softmax(head.logits(emb[ev_idx]) / T)
    is_oof = fit.copy()
    ctx.save_npz("preds", probs=probs, is_oof=is_oof, temperature=np.array([T]))
    ckW = np.stack([c[0] for c in full.checkpoints]) if full.checkpoints else np.zeros((0, emb.shape[1], C))
    ckb = np.stack([c[1] for c in full.checkpoints]) if full.checkpoints else np.zeros((0, C))
    cklr = np.array([c[2] for c in full.checkpoints])
    ctx.save_npz(
        "model",
        W=head.W,
        b=head.b,
        mean=head.mean,
        std=head.std,
        T=np.array([T]),
        sgd_mean=full.mean,
        sgd_std=full.std,
        ckW=ckW,
        ckb=ckb,
        cklr=cklr,
        fit_idx=fit_idx,
        ev_idx=ev_idx,
    )
    ctx.save_npz("dynamics_raw", p=full.epoch_p_given, c=full.epoch_correct)

    cv_metrics = bl.classification_metrics(y_fit, oof_probs, S.class_names)
    cv_metrics["ece_uncalibrated"] = round(bl.expected_calibration_error(oof_raw, y_fit), 5)
    cv_metrics["temperature"] = round(T, 4)
    eval_metrics: dict[str, Any] = {}
    for sp in ("val", "test"):
        idx = np.nonzero(np.array(S.split) == sp)[0]
        if len(idx):
            eval_metrics[sp] = bl.classification_metrics(S.labels[idx], probs[idx], S.class_names)
    if len(ev_idx):
        eval_metrics["all_eval"] = bl.classification_metrics(S.labels[ev_idx], probs[ev_idx], S.class_names)

    rows = bl.prediction_rows(probs, S.labels)
    # Failure events: misclassified evaluation samples (or out-of-fold misclassifications without an eval split).
    fail_pool = ev_idx if len(ev_idx) else fit_idx
    failures = [int(i) for i in fail_pool if rows["pred"][i] != S.labels[i]]
    failures = sorted(failures, key=lambda i: -rows["p_pred"][i])[: ctx.config["influence"]["max_failures"]]
    ctx.save_json("failures", failures)
    k_links = int(ctx.config["influence"]["top_k_links"])
    dup_mem = {int(k): v for k, v in ctx.load_json("dup_membership").items()}

    with new_session() as s:
        s.execute(
            delete(m.FailureDataLink).where(
                m.FailureDataLink.failure_event_id.in_(
                    select(m.FailureEvent.id).where(m.FailureEvent.audit_run_id == ctx.audit_id)
                )
            )
        )
        _clear(s, m.FailureEvent, ctx.audit_id)
        _clear(s, m.ModelPrediction, ctx.audit_id)
        _clear(s, m.BaselineModelRun, ctx.audit_id)
        s.flush()
        cv_run = m.BaselineModelRun(
            audit_run_id=ctx.audit_id,
            kind="cross_validation",
            seed=tcfg.seed,
            config={
                "solver": "L-BFGS (converged, seed-free)",
                "C": lr_C,
                "class_balanced": balanced,
                "folds": folds,
                "fold_seed": ctx.config["seed"],
                "algorithm": bl.VERSION,
                "features": _backend_name(ctx),
            },
            metrics=cv_metrics,
        )
        full_run = m.BaselineModelRun(
            audit_run_id=ctx.audit_id,
            kind="full",
            seed=tcfg.seed,
            config={
                "solver": "L-BFGS (converged, seed-free)",
                "C": lr_C,
                "iterations": head.iterations,
                "converged": head.converged,
                "algorithm": bl.VERSION,
                "dynamics_run": {**tcfg.as_dict(), "loss_curve": full.loss_curve},
            },
            metrics={"eval": eval_metrics, "eval_mode": "holdout" if len(ev_idx) else "cross_validation"},
            model_object_key=ctx._key("model.npz"),
        )
        s.add_all([cv_run, full_run])
        s.flush()
        pred_rows = []
        for i in range(S.n):
            order = rows["order"][i][:3]
            pred_rows.append(
                {
                    "id": uuid.uuid4(),
                    "audit_run_id": ctx.audit_id,
                    "baseline_run_id": cv_run.id if fit[i] else full_run.id,
                    "sample_id": S.ids[i],
                    "predicted_class_id": S.class_ids[int(rows["pred"][i])],
                    "prob_given": round(float(rows["p_given"][i]), 5),
                    "prob_predicted": round(float(rows["p_pred"][i]), 5),
                    "margin": round(float(rows["margin"][i]), 5),
                    "entropy": round(float(rows["entropy"][i]), 5),
                    "top_probs": [[S.class_names[int(c)], round(float(probs[i, c]), 4)] for c in order],
                    "is_out_of_sample": True,
                    "correct": bool(rows["pred"][i] == S.labels[i]),
                }
            )
        _bulk(s, m.ModelPrediction, pred_rows)
        fe_rows, link_rows = [], []
        if failures:
            nn_s, nn_i = knn(emb[failures].astype(np.float32), emb[fit_idx].astype(np.float32), k_links + 1)
            for r, i in enumerate(failures):
                fid = uuid.uuid4()
                fe_rows.append(
                    {
                        "id": fid,
                        "audit_run_id": ctx.audit_id,
                        "sample_id": S.ids[i],
                        "split": S.split[i],
                        "actual_class_id": S.class_ids[int(S.labels[i])],
                        "predicted_class_id": S.class_ids[int(rows["pred"][i])],
                        "confidence": round(float(rows["p_pred"][i]), 5),
                    }
                )
                rank = 0
                for c in range(nn_i.shape[1]):
                    j = int(fit_idx[nn_i[r, c]]) if nn_i[r, c] >= 0 else -1
                    if j < 0 or j == i or rank >= k_links:
                        continue
                    link_rows.append(
                        {
                            "id": uuid.uuid4(),
                            "failure_event_id": fid,
                            "train_sample_id": S.ids[j],
                            "link_type": "nearest_neighbor",
                            "score": round(float(nn_s[r, c]), 5),
                            "rank": rank,
                        }
                    )
                    rank += 1
                if i in dup_mem:
                    for rnk, j in enumerate(
                        [x for x in dup_mem[i]["members"] if x != i and fit[x]][:k_links]
                    ):
                        link_rows.append(
                            {
                                "id": uuid.uuid4(),
                                "failure_event_id": fid,
                                "train_sample_id": S.ids[j],
                                "link_type": "duplicate",
                                "score": 1.0,
                                "rank": rnk,
                            }
                        )
        _bulk(s, m.FailureEvent, fe_rows)
        _bulk(s, m.FailureDataLink, link_rows)
        s.commit()
    return {
        "algorithm": bl.VERSION,
        "folds": folds,
        "temperature": round(T, 4),
        "cross_validation": {
            k: cv_metrics[k] for k in ("accuracy", "macro_f1", "balanced_accuracy", "ece", "ece_uncalibrated")
        },
        "eval": {
            sp: {k: v[k] for k in ("n", "accuracy", "macro_f1", "balanced_accuracy", "ece")}
            for sp, v in eval_metrics.items()
        },
        "failures": len(failures),
        "eval_mode": "holdout" if len(ev_idx) else "cross_validation",
    }


def stage_dynamics(ctx: AuditContext) -> dict:
    if not ctx.deep:
        raise StageSkipped("training dynamics run in the DEEP profile")
    if not ctx.has_artifact("dynamics_raw"):
        raise StageSkipped("baseline model was not trained")
    raw = ctx.load_npz("dynamics_raw")
    model = ctx.load_npz("model")
    fit_idx = model["fit_idx"]
    res = bl.TrainResult(
        W=model["W"],
        b=model["b"],
        mean=model["mean"],
        std=model["std"],
        epoch_p_given=raw["p"],
        epoch_correct=raw["c"],
    )
    table = infl.dynamics_table(res, ctx.config["dynamics"])
    ctx.save_npz(
        "dynamics",
        idx=fit_idx,
        confidence=table["confidence"],
        variability=table["variability"],
        correctness=table["correctness"],
        forgetting=table["forgetting"],
        category=table["category"],
    )
    with new_session() as s:
        _clear(s, m.TrainingDynamics, ctx.audit_id)
        _bulk(
            s,
            m.TrainingDynamics,
            [
                {
                    "id": uuid.uuid4(),
                    "audit_run_id": ctx.audit_id,
                    "sample_id": ctx.samples.ids[int(i)],
                    "confidence": round(float(table["confidence"][r]), 5),
                    "variability": round(float(table["variability"][r]), 5),
                    "correctness": round(float(table["correctness"][r]), 5),
                    "forgetting_events": int(table["forgetting"][r]),
                    "first_learned_epoch": int(table["first_learned"][r])
                    if table["first_learned"][r] >= 0
                    else None,
                    "trajectory": [round(float(x), 4) for x in table["trajectory"][r]],
                    "category": str(table["category"][r]),
                }
                for r, i in enumerate(fit_idx)
            ],
        )
        s.commit()
    return {
        "algorithm": infl.DYNAMICS_VERSION,
        "epochs": int(raw["p"].shape[0]),
        "categories": dict(Counter(table["category"].tolist())),
    }


def stage_influence(ctx: AuditContext) -> dict:
    if not ctx.deep:
        raise StageSkipped("influence estimation runs in the DEEP profile")
    if not ctx.has_artifact("model"):
        raise StageSkipped("baseline model was not trained")
    S = ctx.samples
    model = ctx.load_npz("model")
    emb = ctx.load_npz("embeddings")["emb"].astype(np.float64)
    fit_idx, ev_idx = model["fit_idx"], model["ev_idx"]
    res = bl.TrainResult(W=model["W"], b=model["b"], mean=model["sgd_mean"], std=model["sgd_std"])
    res.checkpoints = [
        (model["ckW"][c], model["ckb"][c], float(model["cklr"][c])) for c in range(len(model["cklr"]))
    ]
    failures = ctx.load_json("failures")
    target_idx = ev_idx if len(ev_idx) else fit_idx
    X_fit, y_fit = emb[fit_idx], S.labels[fit_idx]
    self_inf, inf_eval = infl.tracin(res, X_fit, y_fit, emb[target_idx], S.labels[target_idx])
    pos_in_target = {int(t): r for r, t in enumerate(target_idx)}
    fail_cols = [pos_in_target[f] for f in failures if f in pos_in_target]
    inf_fail = inf_eval[:, fail_cols] if fail_cols else np.zeros((len(fit_idx), 0), np.float32)
    if not len(ev_idx):
        # Without an eval split the targets are training samples; drop self-pairs.
        for c, f in enumerate([failures[k] for k in range(len(fail_cols))]):
            r = np.nonzero(fit_idx == f)[0]
            if len(r):
                inf_fail[r[0], c] = 0.0
    # Counterfactual basis: G[c, i, :] = Σ_e r_e,c (x_e·x_i + 1), with P[c, i, :] the checkpoint probabilities.
    Xt = res.standardize(X_fit)
    Xe = res.standardize(emb[target_idx])
    G = np.zeros((len(res.checkpoints), len(fit_idx), S.n_classes), dtype=np.float32)
    P = np.zeros_like(G)
    for c, (W, b, _) in enumerate(res.checkpoints):
        Re = bl.softmax(Xe @ W + b)
        Re[np.arange(len(target_idx)), S.labels[target_idx]] -= 1.0
        for s0 in range(0, len(fit_idx), 4096):
            G[c, s0 : s0 + 4096] = ((Xt[s0 : s0 + 4096] @ Xe.T + 1.0) @ Re).astype(np.float32)
        P[c] = bl.softmax(Xt @ W + b).astype(np.float32)
    ctx.save_npz(
        "cf_basis", G=G, P=P, lrs=model["cklr"], scale=np.array([float(Xt.shape[1])]), fit_idx=fit_idx
    )
    self_pct = infl.percentile_rank(self_inf)
    k_links = int(ctx.config["influence"]["top_k_links"])
    harmful = -np.minimum(inf_fail, 0).sum(axis=1) if inf_fail.size else np.zeros(len(fit_idx))
    helpful = np.maximum(inf_eval, 0).sum(axis=1) if inf_eval.size else np.zeros(len(fit_idx))
    harmed_count = np.zeros(len(fit_idx), dtype=int)
    affected: dict[int, set] = defaultdict(set)
    link_rows = []
    with new_session() as s:
        fe = dict(
            s.execute(
                select(m.FailureEvent.sample_id, m.FailureEvent.id).where(
                    m.FailureEvent.audit_run_id == ctx.audit_id
                )
            ).all()
        )
        s.execute(
            delete(m.FailureDataLink).where(
                m.FailureDataLink.failure_event_id.in_(list(fe.values())),
                m.FailureDataLink.link_type.in_(["harmful_influence", "helpful_influence"]),
            )
        )
        for c, f in enumerate(failures[: len(fail_cols)]):
            col = inf_fail[:, c]
            fid = fe.get(S.ids[f])
            if fid is None:
                continue
            for rank, r in enumerate(np.argsort(col)[:k_links]):
                if col[r] >= 0:
                    break
                harmed_count[r] += 1
                affected[int(r)].update({S.class_names[int(S.labels[f])]})
                link_rows.append(
                    {
                        "id": uuid.uuid4(),
                        "failure_event_id": fid,
                        "train_sample_id": S.ids[int(fit_idx[r])],
                        "link_type": "harmful_influence",
                        "score": round(float(col[r]), 6),
                        "rank": rank,
                    }
                )
            for rank, r in enumerate(np.argsort(-col)[: max(3, k_links // 2)]):
                if col[r] <= 0:
                    break
                link_rows.append(
                    {
                        "id": uuid.uuid4(),
                        "failure_event_id": fid,
                        "train_sample_id": S.ids[int(fit_idx[r])],
                        "link_type": "helpful_influence",
                        "score": round(float(col[r]), 6),
                        "rank": rank,
                    }
                )
        _bulk(s, m.FailureDataLink, link_rows)
        _clear(s, m.InfluenceFinding, ctx.audit_id)
        keep = np.nonzero((self_pct >= 0.9) | (harmed_count > 0))[0]
        _bulk(
            s,
            m.InfluenceFinding,
            [
                {
                    "id": uuid.uuid4(),
                    "audit_run_id": ctx.audit_id,
                    "sample_id": S.ids[int(fit_idx[r])],
                    "self_influence": round(float(self_inf[r]), 6),
                    "self_influence_pct": round(float(self_pct[r]), 5),
                    "harmful_influence": round(float(harmful[r]), 6),
                    "helpful_influence": round(float(helpful[r]), 6),
                    "failures_harmed": int(harmed_count[r]),
                    "affected_classes": sorted(affected.get(int(r), set())),
                }
                for r in keep
            ],
        )
        s.commit()
    ctx.save_npz(
        "influence",
        fit_idx=fit_idx,
        self_inf=self_inf,
        self_pct=self_pct,
        harmful=harmful,
        harmed_count=harmed_count,
    )
    return {
        "algorithm": infl.INFLUENCE_VERSION,
        "checkpoints": len(res.checkpoints),
        "failures_analyzed": len(fail_cols),
        "targets": "evaluation split" if len(ev_idx) else "training samples (no eval split)",
        "samples_harming_failures": int((harmed_count > 0).sum()),
        "note": "Exact TracIn for the linear head over its SGD checkpoints; an approximation for the full system.",
    }


def _dynamics_by_sample(ctx: AuditContext) -> dict[int, dict]:
    if not ctx.has_artifact("dynamics"):
        return {}
    d = ctx.load_npz("dynamics")
    return {
        int(i): {
            "category": str(d["category"][r]),
            "confidence": round(float(d["confidence"][r]), 4),
            "variability": round(float(d["variability"][r]), 4),
            "forgetting": int(d["forgetting"][r]),
        }
        for r, i in enumerate(d["idx"])
    }


def _influence_by_sample(ctx: AuditContext) -> dict[int, dict]:
    if not ctx.has_artifact("influence"):
        return {}
    d = ctx.load_npz("influence")
    return {
        int(i): {
            "self_pct": round(float(d["self_pct"][r]), 4),
            "harmful": round(float(d["harmful"][r]), 6),
            "failures_harmed": int(d["harmed_count"][r]),
        }
        for r, i in enumerate(d["fit_idx"])
    }


def _quality_severity(ctx: AuditContext) -> tuple[np.ndarray, dict[int, list[dict]]]:
    sev = np.zeros(ctx.samples.n)
    per: dict[int, list[dict]] = defaultdict(list)
    pos = {sid: i for i, sid in enumerate(ctx.samples.ids)}
    with new_session() as s:
        for r in s.scalars(select(m.QualityFinding).where(m.QualityFinding.audit_run_id == ctx.audit_id)):
            i = pos[r.sample_id]
            sev[i] = max(sev[i], qual.SEVERITY_SCORE[r.severity])
            if r.severity != Severity.INFO:
                per[i].append(
                    {
                        "type": r.finding_type,
                        "severity": str(r.severity),
                        "description": r.description,
                        "value": r.measured_value,
                        "threshold": r.threshold,
                    }
                )
    return sev, per


def _attribute_deviation(ctx: AuditContext, attrs: list[dict]) -> np.ndarray:
    S = ctx.samples
    keys = ["brightness", "contrast", "saturation", "border_brightness"]
    dev = np.zeros(S.n)
    for c in range(S.n_classes):
        idx = np.nonzero(S.labels == c)[0]
        if len(idx) < 8:
            continue
        zs = []
        for k in keys:
            zs.append(np.abs(attrs_mod.robust_z(np.array([float(attrs[i][k]) for i in idx]))))
        zs.append(
            np.abs(attrs_mod.robust_z(np.log(np.array([float(attrs[i]["min_side"]) for i in idx]) + 1)))
        )
        gray = np.array([float(attrs[i]["is_grayscale"]) for i in idx])
        gz = (
            np.where(gray != (gray.mean() > 0.5), 4.0, 0.0)
            if 0 < gray.mean() < 0.15 or 0.85 < gray.mean() < 1
            else np.zeros(len(idx))
        )
        zs.append(gz)
        dev[idx] = np.max(np.stack(zs), axis=0)
    return dev


def stage_labels(ctx: AuditContext) -> dict:
    S = ctx.samples
    if not ctx.has_artifact("preds"):
        raise StageSkipped("baseline model was not trained")
    probs = ctx.load_npz("preds")["probs"]
    emb = ctx.load_npz("embeddings")["emb"]
    kn = ctx.load_npz("knn")
    attrs = ctx.load_json("attributes")
    dup_mem = {int(k): v for k, v in ctx.load_json("dup_membership").items()}
    fit, _ = _fit_eval_masks(ctx)
    rows = bl.prediction_rows(probs, S.labels)
    exclude = [set(dup_mem[i]["members"]) if i in dup_mem else set() for i in range(S.n)]
    same, major, major_share, dists = lab.neighbor_label_stats(
        S.labels, kn["sims"], kn["idx"], exclude, S.n_classes
    )
    ratio, nearest_other, cents = lab.centroid_evidence(emb, S.labels, fit, S.n_classes)
    fit_idx = np.nonzero(fit)[0]
    nb_pred = np.array([max(d, key=d.get) if d else S.labels[i] for i, d in enumerate(dists)])
    reliability = {
        "model": lab.witness_reliability(S.labels[fit_idx], rows["pred"][fit_idx], S.n_classes),
        "neighbor": lab.witness_reliability(S.labels[fit_idx], nb_pred[fit_idx], S.n_classes),
        "centroid": lab.witness_reliability(
            S.labels[fit_idx], (emb[fit_idx] @ cents.T).argmax(1), S.n_classes
        ),
    }
    density = cov.local_density(kn["sims"], ctx.config["coverage"]["density_k"])
    density_pct = infl.percentile_rank(density)
    dyn = _dynamics_by_sample(ctx)
    inf = _influence_by_sample(ctx)
    qsev, _ = _quality_severity(ctx)
    dev = _attribute_deviation(ctx, attrs)
    sig = lab.LabelSignals(
        p_given=rows["p_given"],
        pred=rows["pred"],
        p_pred=rows["p_pred"],
        top12_gap=rows["top12_gap"],
        neighbor_same=same,
        neighbor_major=major,
        neighbor_major_share=major_share,
        neighbor_dist=dists,
        centroid_ratio=ratio,
        nearest_other_class=nearest_other,
        density_pct=density_pct,
        dynamics=[dyn.get(i, {}).get("category") for i in range(S.n)],
        dup_conflict=np.array([bool(dup_mem.get(i, {}).get("label_conflict")) for i in range(S.n)]),
        quality_severity=qsev,
        self_influence_pct=np.array([inf.get(i, {}).get("self_pct", 0.0) for i in range(S.n)])
        if inf
        else None,
        attribute_deviation=dev,
        reliability=reliability,
    )
    lcfg = ctx.config["labels"]
    scores = lab.score_labels(sig, lcfg)
    store_min = lcfg["store_min_suspicion"]
    cn = S.class_names
    label_rows, rare_rows, rare_out = [], [], {}
    for i in range(S.n):
        act = str(scores["action"][i])
        susp = float(scores["suspicion"][i])
        candidate_rw = act != "NO_ACTION" or density_pct[i] <= 0.05
        if susp >= store_min or act != "NO_ACTION":
            evidence = {
                "current_label": cn[S.labels[i]],
                "predicted_label": cn[int(rows["pred"][i])],
                "p_given": round(float(rows["p_given"][i]), 4),
                "p_predicted": round(float(rows["p_pred"][i]), 4),
                "margin": round(float(rows["margin"][i]), 4),
                "neighbor_distribution": {cn[int(k)]: v for k, v in dists[i].items()},
                "neighbor_same_label_share": round(float(same[i]), 4),
                "centroid_ratio": round(float(ratio[i]), 4),
                "nearest_other_class": cn[int(nearest_other[i])],
                "density_percentile": round(float(density_pct[i]), 4),
                "dynamics": dyn.get(i),
                "influence": inf.get(i),
                "quality_severity": round(float(qsev[i]), 2),
                "duplicate_label_conflict": bool(sig.dup_conflict[i]),
                "signals": {
                    "model": round(float(scores["s_model"][i]), 4),
                    "neighbor": round(float(scores["s_neighbor"][i]), 4),
                    "centroid": round(float(scores["s_centroid"][i]), 4),
                    "dynamics": None
                    if np.isnan(scores["s_dynamics"][i])
                    else round(float(scores["s_dynamics"][i]), 4),
                },
                "score_type": "evidence score (weighted mean of normalized signals), not a probability",
                "witness_reliability": {k: round(v, 3) for k, v in reliability.items()},
            }
            label_rows.append(
                {
                    "id": uuid.uuid4(),
                    "audit_run_id": ctx.audit_id,
                    "sample_id": S.ids[i],
                    "suspicion_score": round(susp, 5),
                    "model_disagreement": round(float(scores["s_model"][i]), 5),
                    "neighbor_agreement": round(float(same[i]), 5),
                    "centroid_ratio": round(float(ratio[i]), 5),
                    "predicted_class_id": S.class_ids[int(rows["pred"][i])],
                    "neighbor_majority_class_id": S.class_ids[int(major[i])] if major[i] >= 0 else None,
                    "recommended_action": act,
                    "evidence": evidence,
                }
            )
        if candidate_rw:
            hyp, hs, reasons, valuable = lab.rare_or_wrong(sig, scores, lcfg, i)
            rare_out[i] = {"hypothesis": str(hyp), "scores": hs}
            rare_rows.append(
                {
                    "id": uuid.uuid4(),
                    "audit_run_id": ctx.audit_id,
                    "sample_id": S.ids[i],
                    "hypothesis": hyp,
                    "scores": hs,
                    "reasons": reasons,
                    "valuable_reasons": valuable,
                }
            )
    with new_session() as s:
        _clear(s, m.LabelFinding, ctx.audit_id)
        _clear(s, m.RareWrongFinding, ctx.audit_id)
        _bulk(s, m.LabelFinding, label_rows)
        _bulk(s, m.RareWrongFinding, rare_rows)
        s.commit()
    ctx.save_npz(
        "label_signals",
        suspicion=scores["suspicion"],
        action=scores["action"],
        same=same,
        major=major,
        major_share=major_share,
        ratio=ratio,
        nearest_other=nearest_other,
        density_pct=density_pct,
        pred=rows["pred"],
        p_given=rows["p_given"],
        p_pred=rows["p_pred"],
    )
    ctx.save_json("rare_wrong", {str(k): v for k, v in rare_out.items()})
    ctx.save_json("witness_reliability", reliability)
    return {
        "algorithm": lab.LABEL_VERSION,
        "rare_algorithm": lab.RARE_VERSION,
        "witness_reliability": {k: round(v, 4) for k, v in reliability.items()},
        "effective_weights": {k: round(v, 4) for k, v in scores["weights"].items()},
        "actions": dict(Counter(scores["action"].tolist())),
        "stored_findings": len(label_rows),
        "rare_or_wrong": dict(Counter(v["hypothesis"] for v in rare_out.values())),
    }


def stage_shortcuts(ctx: AuditContext) -> dict:
    S = ctx.samples
    attrs = ctx.load_json("attributes")
    cues = sc.derive_cues(attrs, S.meta)
    fit, ev = _fit_eval_masks(ctx)
    scfg = ctx.config["shortcuts"]
    findings, summary = sc.analyze(cues, S.labels, S.class_names, fit, ev, scfg, ctx.config["seed"])
    tests: dict[str, Any] = {}
    if ctx.deep and ev.sum() >= 10 and ctx.has_artifact("model"):
        tests = _perturbation_tests(ctx, np.nonzero(ev)[0], scfg["perturbation_max_samples"])
        summary["perturbation_tests"] = tests
    membership: dict[int, list] = defaultdict(list)
    with new_session() as s:
        _clear(s, m.ShortcutFinding, ctx.audit_id)
        for fi, f in enumerate(findings):
            evidence = {"per_cue": summary["per_cue"].get(f["cue"])}
            if fi == 0 and tests:
                evidence["perturbation_tests"] = tests
            s.add(
                m.ShortcutFinding(
                    audit_run_id=ctx.audit_id,
                    cue=f["cue"],
                    cue_value=f["cue_value"],
                    class_id=S.class_ids[f["class_index"]],
                    strength=f["strength"],
                    strength_label=f["strength_label"],
                    association=f["association"],
                    affected_sample_ids=[str(S.ids[i]) for i in f["affected"]],
                    consequence=f["consequence"],
                    recommendation=f["recommendation"],
                    evidence=evidence,
                )
            )
            if f["strength_label"] in ("moderate", "strong"):
                for i in f["affected"]:
                    membership[i].append(
                        {
                            "cue_label": f["cue_label"],
                            "value": f["cue_value"],
                            "strength_label": f["strength_label"],
                        }
                    )
        s.commit()
    ctx.save_json("shortcut_membership", {str(k): v for k, v in membership.items()})
    return {
        "algorithm": sc.VERSION,
        "cues_analyzed": sorted(cues),
        "findings": len(findings),
        "strong": sum(1 for f in findings if f["strength_label"] == "strong"),
        "summary": summary,
    }


def _perturbation_tests(ctx: AuditContext, ev_idx: np.ndarray, max_n: int) -> dict:
    settings = get_settings()
    backend, _ = load_backend(settings.embedding_backend, settings.model_cache_dir)
    rng = np.random.default_rng(ctx.config["seed"])
    chosen = np.sort(rng.choice(ev_idx, size=min(len(ev_idx), max_n), replace=False)).tolist()
    model = ctx.load_npz("model")
    res = bl.TrainResult(W=model["W"], b=model["b"], mean=model["mean"], std=model["std"])
    T = float(model["T"][0])
    side = 256 if backend.name == "dc-descriptor" else 320
    borders = ctx.map_images(lambda i, rgb: sc.border_only(rgb), chosen, max_side=side)
    contents = ctx.map_images(lambda i, rgb: sc.content_only(rgb), chosen, max_side=side)
    y = ctx.samples.labels[chosen]
    out = {}
    emb = ctx.load_npz("embeddings")["emb"]
    base_pred = bl.softmax(res.logits(emb[chosen].astype(np.float64)) / T).argmax(1)
    out["original_accuracy"] = round(float((base_pred == y).mean()), 4)
    for name, imgs in (("border_only", borders), ("content_only", contents)):
        e = backend.embed(imgs).astype(np.float64)
        pred = bl.softmax(res.logits(e) / T).argmax(1)
        per_class = {}
        for c in sorted(set(y.tolist())):
            mk = y == c
            per_class[ctx.samples.class_names[c]] = round(float((pred[mk] == c).mean()), 4)
        out[f"{name}_accuracy"] = round(float((pred == y).mean()), 4)
        out[f"{name}_per_class"] = per_class
    out["n"] = len(chosen)
    out["chance"] = round(1.0 / max(1, len(set(ctx.samples.labels.tolist()))), 4)
    out["interpretation"] = (
        "border_only keeps only the outer 20% of each image (object area filled with the mean colour). Accuracy well above "
        "chance means the model can classify from the background/border alone. content_only removes the border."
    )
    return out


def stage_coverage(ctx: AuditContext) -> dict:
    S = ctx.samples
    emb = ctx.load_npz("embeddings")["emb"]
    kn = ctx.load_npz("knn")
    attrs = ctx.load_json("attributes")
    ccfg = dict(ctx.config["coverage"])
    ccfg["tsne_max"] = ccfg["tsne_max_deep"] if ctx.deep else ccfg["tsne_max_fast"]
    res = cov.analyze(
        emb=emb,
        labels=S.labels,
        class_names=S.class_names,
        split=S.split,
        attrs=attrs,
        knn_sims=kn["sims"],
        cfg=ccfg,
        seed=ctx.config["seed"],
    )
    ctx.save_npz("coverage", coords=res["coords"], assign=res["assign"], density=res["density"])
    with new_session() as s:
        _clear(s, m.CoverageGap, ctx.audit_id)
        _clear(s, m.CoverageCluster, ctx.audit_id)
        _bulk(
            s,
            m.CoverageCluster,
            [
                {
                    "id": uuid.uuid4(),
                    "audit_run_id": ctx.audit_id,
                    "cluster_index": c["cluster_index"],
                    "size": c["size"],
                    "centroid_2d": c["centroid_2d"],
                    "class_composition": c["class_composition"],
                    "split_composition": c["split_composition"],
                    "purity": c["purity"],
                    "density": c["density"],
                    "is_sparse": c["is_sparse"],
                    "dominant_class_id": S.class_ids[c["dominant_class_index"]]
                    if c["dominant_class_index"] >= 0
                    else None,
                    "attributes": c["attributes"],
                }
                for c in res["clusters"]
            ],
        )
        _bulk(
            s,
            m.CoverageGap,
            [
                {
                    "id": uuid.uuid4(),
                    "audit_run_id": ctx.audit_id,
                    "kind": g["kind"],
                    "class_id": S.class_ids[g["class_index"]]
                    if g["class_index"] is not None and g["class_index"] >= 0
                    else None,
                    "cluster_index": g["cluster_index"],
                    "condition": {
                        **g["condition"],
                        **(
                            {"other_class": S.class_names[g["condition"]["other_class_index"]]}
                            if "other_class_index" in g["condition"]
                            else {}
                        ),
                    },
                    "title": g["title"],
                    "description": g["description"],
                    "priority_score": g["priority_score"],
                    "priority": g["priority"],
                    "suggested_quantity": g["quantity"],
                    "anchors": [str(S.ids[i]) for i in g["anchors"]],
                    "evidence": g["evidence"],
                }
                for g in res["gaps"]
            ],
        )
        s.commit()
    return {
        "algorithm": cov.VERSION,
        "projection": res["projection_method"],
        "clusters": res["k"],
        "sparse_clusters": sum(1 for c in res["clusters"] if c["is_sparse"]),
        "gaps": dict(Counter(g["kind"] for g in res["gaps"])),
        "gaps_by_priority": dict(Counter(g["priority"] for g in res["gaps"])),
    }


def stage_cases(ctx: AuditContext) -> dict:
    S = ctx.samples
    cn = S.class_names
    jcfg = ctx.config["jury"]
    has_model = ctx.has_artifact("label_signals")
    ls = ctx.load_npz("label_signals") if has_model else None
    rare = (
        {int(k): v for k, v in ctx.load_json("rare_wrong").items()}
        if ctx.has_artifact("rare_wrong", "json")
        else {}
    )
    dup_mem = {int(k): v for k, v in ctx.load_json("dup_membership").items()}
    leak_mem = {int(k): v for k, v in ctx.load_json("leak_membership").items()}
    sc_mem = (
        {int(k): v for k, v in ctx.load_json("shortcut_membership").items()}
        if ctx.has_artifact("shortcut_membership", "json")
        else {}
    )
    privacy = {int(k): v for k, v in ctx.load_json("privacy").items()}
    dyn = _dynamics_by_sample(ctx)
    inf = _influence_by_sample(ctx)
    _, qual_per = _quality_severity(ctx)
    ece = None
    with new_session() as s:
        cv = s.scalar(
            select(m.BaselineModelRun).where(
                m.BaselineModelRun.audit_run_id == ctx.audit_id, m.BaselineModelRun.kind == "cross_validation"
            )
        )
        if cv:
            ece = cv.metrics.get("ece")
        fam_ids = dict(
            s.execute(
                select(m.DuplicateFamily.family_number, m.DuplicateFamily.id).where(
                    m.DuplicateFamily.audit_run_id == ctx.audit_id
                )
            ).all()
        )
    reliability = (
        ctx.load_json("witness_reliability") if ctx.has_artifact("witness_reliability", "json") else {}
    )
    profile_has = {"model"} if has_model else set()
    if dyn:
        profile_has.add("dynamics")
    if inf:
        profile_has.add("influence")
    k = int(ctx.config["knn"]["k"])
    candidates = set()
    if ls is not None:
        candidates |= {i for i in range(S.n) if str(ls["action"][i]) != "NO_ACTION"}
    candidates |= {i for i, v in leak_mem.items() if v["risk"] in ("medium", "high", "critical", "low")}
    candidates |= {i for i, v in dup_mem.items() if not v["is_root"] or v["label_conflict"]}
    candidates |= {i for i, q in qual_per.items() if any(x["severity"] in ("medium", "high") for x in q)}
    candidates |= {
        i
        for i, v in inf.items()
        if v["failures_harmed"] >= jcfg["harms_failures_min"]
        or v["self_pct"] >= jcfg["high_self_influence_pct"]
    }
    decided: list[tuple[int, jury.CaseInputs, jury.Deliberation]] = []
    for i in sorted(candidates):
        ci = jury.CaseInputs(
            label=cn[S.labels[i]],
            split=S.split[i],
            k=k,
            reliability=reliability,
            min_reliability=jcfg["min_witness_reliability"],
        )
        if ls is not None:
            ci.p_given = float(ls["p_given"][i])
            ci.pred_label = cn[int(ls["pred"][i])]
            ci.p_pred = float(ls["p_pred"][i])
            ci.model_ece = ece
            ci.neighbor_same = float(ls["same"][i])
            ci.neighbor_major_label = cn[int(ls["major"][i])] if int(ls["major"][i]) >= 0 else None
            ci.neighbor_major_share = float(ls["major_share"][i])
            ci.centroid_ratio = float(ls["ratio"][i])
            ci.nearest_other_label = cn[int(ls["nearest_other"][i])]
            ci.density_pct = float(ls["density_pct"][i])
            ci.suspicion = float(ls["suspicion"][i])
            ci.label_action = str(ls["action"][i])
        ci.quality = qual_per.get(i, [])
        if i in dup_mem:
            d = dup_mem[i]
            ci.family = {
                k2: d[k2]
                for k2 in (
                    "number",
                    "size",
                    "kind",
                    "is_root",
                    "relation",
                    "label_conflict",
                    "splits",
                    "labels",
                )
            }
        if i in leak_mem:
            ci.leakage = leak_mem[i]
        ci.dynamics = dyn.get(i)
        ci.influence = inf.get(i)
        ci.shortcut = sc_mem.get(i, [])
        if i in rare:
            ci.hypothesis = rare[i]["hypothesis"]
            ci.hypothesis_scores = rare[i]["scores"]
        ci.privacy = [{"kind": p["kind"], "count": p["count"]} for p in privacy.get(i, [])]
        d = jury.deliberate(ci, jcfg, profile_has)
        if (
            d.verdict == jury.Verdict.KEEP
            and (ci.suspicion or 0) < ctx.config["labels"]["low_priority_threshold"]
        ):
            continue
        decided.append((i, ci, d))
    decided.sort(key=lambda t: -t[2].priority)
    decided = decided[: jcfg["max_cases"]]
    cfg_hash = _audit_config_hash(ctx)
    with new_session() as s:
        case_ids = select(m.CourtCase.id).where(m.CourtCase.audit_run_id == ctx.audit_id)
        # Human decisions reference cases; the stage only regenerates when no decisions exist.
        if (
            s.scalar(select(m.HumanDecision.id).where(m.HumanDecision.case_id.in_(case_ids)).limit(1))
            is not None
        ):
            raise StageSkipped("cases already have human decisions; regeneration would discard review work")
        s.execute(delete(m.CaseEvidence).where(m.CaseEvidence.case_id.in_(case_ids)))
        s.execute(delete(m.CaseVerdict).where(m.CaseVerdict.case_id.in_(case_ids)))
        s.execute(delete(m.CaseExplanation).where(m.CaseExplanation.case_id.in_(case_ids)))
        _clear(s, m.CourtCase, ctx.audit_id)
        s.flush()
        case_rows, ev_rows, verdict_rows = [], [], []
        for number, (i, ci, d) in enumerate(decided, start=1):
            cid = uuid.uuid4()
            fam = dup_mem.get(i)
            case_rows.append(
                {
                    "id": cid,
                    "org_id": ctx.org_id,
                    "audit_run_id": ctx.audit_id,
                    "dataset_version_id": ctx.version_id,
                    "case_number": number,
                    "sample_id": S.ids[i],
                    "verdict": d.verdict,
                    "priority_score": d.priority,
                    "impact_score": d.impact,
                    "strength_score": d.scores["evidence_strength"],
                    "uncertainty": d.uncertainty,
                    "reason_codes": d.reason_codes,
                    "categories": d.categories,
                    "primary_concern": d.primary_concern,
                    "family_id": fam_ids.get(fam["number"]) if fam else None,
                    "est_review_minutes": d.est_minutes,
                    "status": CaseStatus.OPEN,
                }
            )
            for e in jury.witnesses(ci):
                ev_rows.append(
                    {
                        "id": uuid.uuid4(),
                        "case_id": cid,
                        "witness": Witness(e.witness),
                        "stance": Stance(e.stance),
                        "evidence_kind": EvidenceKind(e.kind),
                        "code": e.code,
                        "title": e.title[:255],
                        "detail": e.detail,
                        "value": e.value,
                        "weight": round(e.weight, 4),
                    }
                )
            verdict_rows.append(
                {
                    "id": uuid.uuid4(),
                    "case_id": cid,
                    "jury_version": jury.VERSION,
                    "config_hash": cfg_hash,
                    "verdict": d.verdict,
                    "reason_codes": d.reason_codes,
                    "rule_trace": d.rule_trace,
                    "scores": {**d.scores, "target_label": d.target_label},
                }
            )
        _bulk(s, m.CourtCase, case_rows)
        _bulk(s, m.CaseEvidence, ev_rows)
        _bulk(s, m.CaseVerdict, verdict_rows)
        verdicts = Counter(str(d.verdict) for _, _, d in decided)
        ledger.record(
            s,
            org_id=ctx.org_id,
            event_type="audit.verdicts_issued",
            entity_type="audit_run",
            entity_id=ctx.audit_id,
            dataset_version_id=ctx.version_id,
            payload={
                "jury_version": jury.VERSION,
                "config_hash": cfg_hash,
                "cases": len(decided),
                "verdicts": dict(verdicts),
            },
        )
        s.commit()
    return {
        "jury_version": jury.VERSION,
        "cases": len(decided),
        "verdicts": dict(verdicts),
        "candidates_considered": len(candidates),
    }


def _audit_config_hash(ctx: AuditContext) -> str:
    with new_session() as s:
        a = s.get(m.AuditRun, ctx.audit_id)
        assert a is not None
        return a.config_hash


def issue_profile(facts: dict) -> dict:
    n = max(1, facts["sample_count"])
    return {
        "duplicate_rate": round(facts["duplicate_rate"], 5),
        "quality_rate": round(facts["quality_rate"], 5),
        "label_review_rate": round((facts["label_actions"].get("REVIEW", 0)) / n, 5),
        "leakage_findings": facts["leakage_by_risk"],
        "shortcut_max": facts["shortcut_max_strength"],
    }


def stage_debt(ctx: AuditContext) -> dict:
    with new_session() as s:
        a = s.get(m.AuditRun, ctx.audit_id)
        assert a is not None
        facts = collect_facts(s, a)
        debt = gov.compute_debt(facts)
        s.execute(
            delete(m.DatasetDebtSnapshot).where(
                m.DatasetDebtSnapshot.audit_run_id == ctx.audit_id, m.DatasetDebtSnapshot.trigger == "audit"
            )
        )
        s.add(
            m.DatasetDebtSnapshot(
                dataset_version_id=ctx.version_id,
                audit_run_id=ctx.audit_id,
                formula_version=gov.DEBT_VERSION,
                trigger="audit",
                overall=debt["overall"],
                dimensions=debt,
            )
        )
        s.commit()
    attrs = ctx.load_json("attributes")
    emb = ctx.load_npz("embeddings")["emb"]
    profile = dna_mod.build_profile(
        labels=ctx.samples.labels,
        class_names=ctx.samples.class_names,
        split=ctx.samples.split,
        attrs=attrs,
        meta=ctx.samples.meta,
        emb=emb,
        backend=_backend_name(ctx),
        issue_profile={**issue_profile(facts), "debt": debt["overall"]},
    )
    fp = dna_mod.fingerprint(profile)
    with new_session() as s:
        _clear(s, m.DatasetDnaProfile, ctx.audit_id)
        s.add(
            m.DatasetDnaProfile(
                audit_run_id=ctx.audit_id,
                dataset_version_id=ctx.version_id,
                dna_version=dna_mod.VERSION,
                fingerprint=fp,
                profile=profile,
            )
        )
        s.commit()
    return {
        "formula_version": gov.DEBT_VERSION,
        "overall": debt["overall"],
        "levels": {k: v["level"] for k, v in debt["dimensions"].items()},
        "dna_fingerprint": fp,
    }


def stage_preflight(ctx: AuditContext) -> dict:
    with new_session() as s:
        a = s.get(m.AuditRun, ctx.audit_id)
        assert a is not None
        facts = collect_facts(s, a)
        pf = gov.preflight(facts, ctx.config["preflight"])
        _clear(s, m.PreflightResult, ctx.audit_id)
        s.add(
            m.PreflightResult(
                audit_run_id=ctx.audit_id,
                dataset_version_id=ctx.version_id,
                rules_version=pf["rules_version"],
                status=pf["status"],
                checks=pf["checks"],
            )
        )
        debt = s.scalar(
            select(m.DatasetDebtSnapshot)
            .where(m.DatasetDebtSnapshot.audit_run_id == ctx.audit_id)
            .order_by(m.DatasetDebtSnapshot.created_at.desc())
            .limit(1)
        )
        full_facts = enrich_with_governance(facts, str(debt.overall) if debt else None, pf["status"])
        contract_result = None
        dataset = s.get(m.Dataset, ctx.dataset_id)
        assert dataset is not None
        contract = s.scalar(
            select(m.DataContract)
            .where(m.DataContract.project_id == dataset.project_id, m.DataContract.is_active.is_(True))
            .order_by(m.DataContract.version.desc())
            .limit(1)
        )
        if contract is not None:
            contract_result = gov.evaluate_contract(contract.rules, full_facts)
            s.add(
                m.ContractEvaluation(
                    contract_id=contract.id,
                    dataset_version_id=ctx.version_id,
                    audit_run_id=ctx.audit_id,
                    passed=contract_result["passed"],
                    results=contract_result["results"],
                )
            )
        ledger.record(
            s,
            org_id=ctx.org_id,
            event_type="preflight.evaluated",
            entity_type="audit_run",
            entity_id=ctx.audit_id,
            dataset_version_id=ctx.version_id,
            payload={
                "status": pf["status"],
                "rules_version": pf["rules_version"],
                "contract_passed": None if contract_result is None else contract_result["passed"],
            },
        )
        s.commit()
    return {
        "status": pf["status"],
        "blocking": [c["id"] for c in pf["checks"] if c["status"] == "block"],
        "warnings": [c["id"] for c in pf["checks"] if c["status"] == "warn"],
        "contract_passed": None if contract_result is None else contract_result["passed"],
    }
