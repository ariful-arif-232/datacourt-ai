"""Audit orchestration: ordered stages, progress, resumability, completion bookkeeping."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select

from datacourt import jobs, ledger
from datacourt.db import models as m
from datacourt.db.session import new_session
from datacourt.enums import JobType, RunStatus, Stage, StepStatus
from datacourt.logging_setup import log
from datacourt.pipeline import stages as st
from datacourt.pipeline.context import AuditContext, load_samples

logger = logging.getLogger("datacourt.pipeline")

# (stage, function, relative cost weight). Order differs slightly from the stage enum:
# training dynamics and influence run before label forensics so labels can use them.
PIPELINE: list[tuple[Stage, Callable[[AuditContext], dict], float]] = [
    (Stage.INGESTING, st.stage_ingesting, 0.5),
    (Stage.VALIDATING, st.stage_validating, 0.5),
    (Stage.PROFILING, st.stage_profiling, 12),
    (Stage.QUALITY_ANALYSIS, st.stage_quality, 2),
    (Stage.EMBEDDING, st.stage_embedding, 20),
    (Stage.DUPLICATE_ANALYSIS, st.stage_duplicates, 5),
    (Stage.LEAKAGE_ANALYSIS, st.stage_leakage, 3),
    (Stage.BASELINE_TRAINING, st.stage_baseline, 10),
    (Stage.TRAINING_DYNAMICS, st.stage_dynamics, 2),
    (Stage.INFLUENCE_ANALYSIS, st.stage_influence, 6),
    (Stage.LABEL_FORENSICS, st.stage_labels, 4),
    (Stage.SHORTCUT_ANALYSIS, st.stage_shortcuts, 6),
    (Stage.COVERAGE_ANALYSIS, st.stage_coverage, 8),
    (Stage.COURT_CASE_GENERATION, st.stage_cases, 4),
    (Stage.DEBT_CALCULATION, st.stage_debt, 1),
    (Stage.PREFLIGHT, st.stage_preflight, 1),
]
TOTAL_WEIGHT = sum(w for _, _, w in PIPELINE)
STAGE_ORDER = [s for s, _, _ in PIPELINE] + [Stage.COMPLETE]


def ensure_steps(s, audit_id: uuid.UUID) -> None:
    existing = set(
        s.scalars(select(m.AuditPipelineStep.stage).where(m.AuditPipelineStep.audit_run_id == audit_id))
    )
    for order, stage in enumerate(STAGE_ORDER):
        if str(stage) not in existing:
            s.add(
                m.AuditPipelineStep(
                    audit_run_id=audit_id, stage=str(stage), order=order, status=StepStatus.PENDING
                )
            )


def run_audit_job(job_ctx) -> dict:
    audit_id = uuid.UUID(job_ctx.payload["audit_run_id"])
    with new_session() as s:
        audit = s.get(m.AuditRun, audit_id)
        if audit is None:
            raise jobs.PermanentJobError("audit run not found")
        if audit.status == RunStatus.COMPLETED:
            return {"skipped": "already complete"}
        version = s.get(m.DatasetVersion, audit.dataset_version_id)
        assert version is not None
        ensure_steps(s, audit_id)
        audit.status = RunStatus.RUNNING
        audit.started_at = audit.started_at or datetime.now(UTC)
        audit.error = None
        s.commit()
        ctx = AuditContext(
            audit_id=audit.id,
            org_id=audit.org_id,
            version_id=version.id,
            dataset_id=version.dataset_id,
            profile=str(audit.profile),
            config=audit.config,
            samples=load_samples(version.id),
            warnings=list(audit.warnings or []),
        )
    if ctx.samples.n == 0:
        raise jobs.PermanentJobError("dataset version has no valid samples")

    done_weight = 0.0
    for stage, fn, weight in PIPELINE:
        job_ctx.check_cancelled()
        with new_session() as s:
            step = s.scalar(
                select(m.AuditPipelineStep).where(
                    m.AuditPipelineStep.audit_run_id == audit_id, m.AuditPipelineStep.stage == str(stage)
                )
            )
            assert step is not None
            if step.status in (StepStatus.COMPLETED, StepStatus.SKIPPED):
                done_weight += weight
                continue
            step.status = StepStatus.RUNNING
            step.started_at = datetime.now(UTC)
            step.attempts += 1
            step.error = None
            a = s.get(m.AuditRun, audit_id)
            assert a is not None
            a.current_stage = str(stage)
            a.progress = done_weight / TOTAL_WEIGHT
            s.commit()

        base = done_weight / TOTAL_WEIGHT
        span = weight / TOTAL_WEIGHT

        def report(frac: float, _base: float = base, _span: float = span, _stage: str = str(stage)) -> None:
            job_ctx.report(_base + _span * frac, _stage)

        ctx.report = report
        job_ctx.report(base, str(stage), force=True)
        warn_before = len(ctx.warnings)
        t0 = time.monotonic()
        status, output, error = StepStatus.COMPLETED, {}, None
        try:
            output = fn(ctx) or {}
        except st.StageSkipped as skip:
            status, output = StepStatus.SKIPPED, {"reason": skip.reason}
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:400]}"
            with new_session() as s:
                step = s.scalar(
                    select(m.AuditPipelineStep).where(
                        m.AuditPipelineStep.audit_run_id == audit_id, m.AuditPipelineStep.stage == str(stage)
                    )
                )
                assert step is not None
                step.status = StepStatus.FAILED
                step.error = error
                step.finished_at = datetime.now(UTC)
                step.duration_ms = int((time.monotonic() - t0) * 1000)
                s.commit()
            raise
        duration = int((time.monotonic() - t0) * 1000)
        with new_session() as s:
            step = s.scalar(
                select(m.AuditPipelineStep).where(
                    m.AuditPipelineStep.audit_run_id == audit_id, m.AuditPipelineStep.stage == str(stage)
                )
            )
            assert step is not None
            step.status = status
            step.output = output
            step.warnings = ctx.warnings[warn_before:]
            step.finished_at = datetime.now(UTC)
            step.duration_ms = duration
            a = s.get(m.AuditRun, audit_id)
            assert a is not None
            a.warnings = ctx.warnings
            s.commit()
        log(
            logger, logging.INFO, "stage finished", stage=str(stage), status=str(status), duration_ms=duration
        )
        done_weight += weight

    return finalize_audit(audit_id)


def finalize_audit(audit_id: uuid.UUID) -> dict:
    from datacourt.ml import (
        attributes,
        baseline,
        coverage,
        dna,
        duplicates,
        governance,
        influence,
        jury,
        labels,
        leakage,
        privacy,
        quality,
        shortcuts,
    )

    with new_session() as s:
        audit = s.get(m.AuditRun, audit_id)
        assert audit is not None
        steps = s.scalars(
            select(m.AuditPipelineStep).where(m.AuditPipelineStep.audit_run_id == audit_id)
        ).all()
        outputs = {st_.stage: st_.output for st_ in steps}
        for st_ in steps:
            if st_.stage == str(Stage.COMPLETE):
                st_.status = StepStatus.COMPLETED
                st_.started_at = st_.finished_at = datetime.now(UTC)
                st_.duration_ms = 0
        algos = {
            "attributes": attributes.VERSION,
            "quality": quality.RULE_VERSION,
            "duplicates": duplicates.VERSION,
            "leakage": leakage.VERSION,
            "baseline": baseline.VERSION,
            "dynamics": influence.DYNAMICS_VERSION,
            "influence": influence.INFLUENCE_VERSION,
            "labels": labels.LABEL_VERSION,
            "rare_or_wrong": labels.RARE_VERSION,
            "shortcuts": shortcuts.VERSION,
            "coverage": coverage.VERSION,
            "jury": jury.VERSION,
            "debt": governance.DEBT_VERSION,
            "preflight": governance.PREFLIGHT_VERSION,
            "dna": dna.VERSION,
            "privacy": privacy.VERSION,
            "config": audit.config.get("config_version"),
        }
        audit.algorithm_versions = algos
        audit.summary = {
            "cases": outputs.get("COURT_CASE_GENERATION", {}).get("cases", 0),
            "verdicts": outputs.get("COURT_CASE_GENERATION", {}).get("verdicts", {}),
            "quality_findings": outputs.get("QUALITY_ANALYSIS", {}).get("findings", 0),
            "duplicate_families": outputs.get("DUPLICATE_ANALYSIS", {}).get("families", 0),
            "leakage": outputs.get("LEAKAGE_ANALYSIS", {}).get("by_risk", {}),
            "label_actions": outputs.get("LABEL_FORENSICS", {}).get("actions", {}),
            "shortcut_findings": outputs.get("SHORTCUT_ANALYSIS", {}).get("findings", 0),
            "coverage_gaps": outputs.get("COVERAGE_ANALYSIS", {}).get("gaps_by_priority", {}),
            "debt": outputs.get("DEBT_CALCULATION", {}).get("overall"),
            "preflight": outputs.get("PREFLIGHT", {}).get("status"),
            "baseline": outputs.get("BASELINE_TRAINING", {}),
            "dna_fingerprint": outputs.get("DEBT_CALCULATION", {}).get("dna_fingerprint"),
        }
        audit.status = RunStatus.COMPLETED
        audit.progress = 1.0
        audit.current_stage = str(Stage.COMPLETE)
        audit.finished_at = datetime.now(UTC)
        ledger.record(
            s,
            org_id=audit.org_id,
            event_type="audit.completed",
            entity_type="audit_run",
            entity_id=audit.id,
            dataset_version_id=audit.dataset_version_id,
            actor_id=audit.created_by,
            payload={
                "profile": str(audit.profile),
                "config_hash": audit.config_hash,
                "algorithm_versions": algos,
                "embedding_model": audit.embedding_model,
                "summary": {k: v for k, v in audit.summary.items() if k != "baseline"},
            },
        )
        # Compare against the previous version of the dataset, if any.
        version = s.get(m.DatasetVersion, audit.dataset_version_id)
        assert version is not None
        prev = s.scalar(
            select(m.DatasetVersion)
            .where(
                m.DatasetVersion.dataset_id == version.dataset_id,
                m.DatasetVersion.version_number < version.version_number,
            )
            .order_by(m.DatasetVersion.version_number.desc())
            .limit(1)
        )
        if prev is not None:
            jobs.enqueue(
                s,
                JobType.VERSION_DIFF,
                {"from_version_id": str(prev.id), "to_version_id": str(version.id)},
                org_id=audit.org_id,
                priority=120,
                idempotency_key=f"diff:{prev.id}:{version.id}:{audit.id}",
            )
        s.commit()
        return {
            "audit_run_id": str(audit_id),
            "summary": {k: v for k, v in audit.summary.items() if k != "baseline"},
        }
