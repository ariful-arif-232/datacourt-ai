"""DataCourt Audit Report (a training-readiness report, not a compliance certification)."""

from __future__ import annotations

import json
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session

from datacourt import jobs, ledger
from datacourt.db import models as m
from datacourt.db.session import new_session
from datacourt.enums import RunStatus
from datacourt.ml import governance as gov
from datacourt.pipeline.facts import collect_facts
from datacourt.services.review import agreement_stats
from datacourt.storage import get_store, report_key

_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=select_autoescape(["html"])
)

LIMITATIONS = [
    "Findings are evidence for human review, not ground truth. Model predictions can be wrong, and a disagreement between the "
    "baseline model and a label does not prove the label is wrong.",
    "The baseline model is a linear head on frozen embeddings; its metrics describe this proxy model, not your production model.",
    "Visual similarity does not establish that two images show the same subject; only byte-identical files are certain duplicates.",
    "Influence values (TracIn) are first-order approximations for the linear head.",
    "Shortcut findings are statistical associations and perturbation tests, not causal proof.",
    "What-if results are experimental results on this evaluation setup, not guaranteed production improvements.",
    "Coverage recommendations suggest what to collect; they do not assess real-world class frequency.",
    "This report is a technical audit aid. It is not a legal, regulatory or compliance certification.",
]


def build_report_data(s: Session, audit: m.AuditRun) -> dict[str, Any]:
    version = s.get(m.DatasetVersion, audit.dataset_version_id)
    assert version is not None
    dataset = s.get(m.Dataset, version.dataset_id)
    project = s.get(m.Project, dataset.project_id) if dataset else None
    facts = collect_facts(s, audit)
    steps = {
        x.stage: x
        for x in s.scalars(select(m.AuditPipelineStep).where(m.AuditPipelineStep.audit_run_id == audit.id))
    }
    debt = s.scalar(
        select(m.DatasetDebtSnapshot)
        .where(m.DatasetDebtSnapshot.audit_run_id == audit.id)
        .order_by(m.DatasetDebtSnapshot.created_at.desc())
        .limit(1)
    )
    live_debt = gov.compute_debt(facts)
    pf = s.scalar(
        select(m.PreflightResult)
        .where(m.PreflightResult.audit_run_id == audit.id)
        .order_by(m.PreflightResult.created_at.desc())
        .limit(1)
    )
    dna = s.scalar(select(m.DatasetDnaProfile).where(m.DatasetDnaProfile.audit_run_id == audit.id))
    classes = {
        c.id: c.name
        for c in s.scalars(select(m.DatasetClass).where(m.DatasetClass.dataset_version_id == version.id))
    }
    decisions = s.execute(
        select(m.HumanDecision.action, m.HumanDecision.is_adjudication)
        .join(m.CourtCase, m.CourtCase.id == m.HumanDecision.case_id)
        .where(m.CourtCase.audit_run_id == audit.id, m.HumanDecision.undone_at.is_(None))
    ).all()
    shortcuts = s.scalars(
        select(m.ShortcutFinding)
        .where(m.ShortcutFinding.audit_run_id == audit.id)
        .order_by(m.ShortcutFinding.strength.desc())
    ).all()
    gaps = s.scalars(
        select(m.CoverageGap)
        .where(m.CoverageGap.audit_run_id == audit.id)
        .order_by(m.CoverageGap.priority_score.desc())
        .limit(10)
    ).all()
    leaks = s.scalars(select(m.LeakageFinding).where(m.LeakageFinding.audit_run_id == audit.id)).all()
    whatifs = s.scalars(
        select(m.WhatIfRun)
        .where(m.WhatIfRun.audit_run_id == audit.id, m.WhatIfRun.status == RunStatus.COMPLETED)
        .order_by(m.WhatIfRun.created_at.desc())
        .limit(5)
    ).all()
    contract = s.scalar(
        select(m.ContractEvaluation)
        .where(m.ContractEvaluation.audit_run_id == audit.id)
        .order_by(m.ContractEvaluation.created_at.desc())
        .limit(1)
    )
    embed = s.scalar(select(m.EmbeddingMetadata).where(m.EmbeddingMetadata.audit_run_id == audit.id))
    return {
        "title": "DataCourt Audit Report",
        "subtitle": "Training Readiness Report",
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "dataset": {
            "name": dataset.name if dataset else "",
            "project": project.name if project else "",
            "version": version.version_number,
            "origin": version.origin,
            "source_filename": version.source_filename,
            "source_sha256": version.source_sha256,
            "manifest_sha256": version.manifest_sha256,
            "uploaded": version.created_at.strftime("%Y-%m-%d"),
            "provenance": dataset.provenance if dataset else {},
        },
        "audit": {
            "id": str(audit.id),
            "profile": str(audit.profile),
            "config_hash": audit.config_hash,
            "config_version": audit.config.get("config_version"),
            "algorithm_versions": audit.algorithm_versions,
            "embedding": {
                "model": embed.model_name,
                "version": embed.model_version,
                "dim": embed.dim,
                "preprocessing": embed.preprocessing_version,
            }
            if embed
            else None,
            "finished": audit.finished_at.strftime("%Y-%m-%d %H:%M UTC") if audit.finished_at else None,
            "warnings": audit.warnings,
        },
        "facts": facts,
        "stats": version.stats,
        "quality": (steps.get("QUALITY_ANALYSIS").output if steps.get("QUALITY_ANALYSIS") else {}),
        "duplicates": (steps.get("DUPLICATE_ANALYSIS").output if steps.get("DUPLICATE_ANALYSIS") else {}),
        "leakage": {
            "by_risk": dict(Counter(str(x.risk) for x in leaks)),
            "by_kind": dict(Counter(str(x.kind) for x in leaks)),
            "integrity": (steps.get("LEAKAGE_ANALYSIS").output or {}).get("integrity")
            if steps.get("LEAKAGE_ANALYSIS")
            else {},
        },
        "labels": (steps.get("LABEL_FORENSICS").output if steps.get("LABEL_FORENSICS") else {}),
        "baseline": (steps.get("BASELINE_TRAINING").output if steps.get("BASELINE_TRAINING") else {}),
        "shortcuts": [
            {
                "cue": x.cue,
                "value": x.cue_value,
                "class": classes.get(x.class_id),
                "strength": x.strength,
                "label": x.strength_label,
                "consequence": x.consequence,
            }
            for x in shortcuts[:8]
        ],
        "gaps": [
            {
                "title": g.title,
                "priority": g.priority,
                "quantity": g.suggested_quantity,
                "description": g.description,
            }
            for g in gaps
        ],
        "debt": debt.dimensions if debt else None,
        "debt_live": live_debt,
        "preflight": {
            "status": str(pf.status),
            "checks": pf.checks,
            "override": pf.override_status,
            "override_reason": pf.override_reason,
        }
        if pf
        else None,
        "contract": {"passed": contract.passed, "results": contract.results} if contract else None,
        "review": {
            "decisions": dict(Counter(str(a) for a, _ in decisions)),
            "adjudications": sum(1 for _, adj in decisions if adj),
            "agreement": agreement_stats(s, audit.id),
        },
        "what_if": [{"name": w.name, "summary": w.summary} for w in whatifs],
        "dna": {"fingerprint": dna.fingerprint, "strip": dna.profile.get("strip")} if dna else None,
        "limitations": LIMITATIONS,
        "methodology": [
            (
                "Evidence",
                "Measured image attributes, SHA-256/perceptual hashes, structural correlation, embeddings and nearest neighbours.",
            ),
            (
                "Model evidence",
                "Cross-validated linear baseline on frozen embeddings; temperature-scaled probabilities with reported calibration error.",
            ),
            (
                "Training dynamics",
                "Per-sample confidence, variability and forgetting across epochs (dataset cartography).",
            ),
            ("Influence", "TracIn over the baseline head's SGD checkpoints."),
            ("Verdicts", "Deterministic, versioned jury rules over reason codes; no LLM involvement."),
            (
                "Debt & preflight",
                "Transparent formulas and rules over audited facts; see formula text per dimension.",
            ),
        ],
    }


def run_report_job(ctx) -> dict:
    report_id = uuid.UUID(ctx.payload["report_id"])
    with new_session() as s:
        rep = s.get(m.AuditReport, report_id)
        if rep is None:
            raise jobs.PermanentJobError("report not found")
        audit = s.get(m.AuditRun, rep.audit_run_id)
        if audit is None or audit.status != RunStatus.COMPLETED:
            raise jobs.PermanentJobError("audit is not complete")
        rep.status = RunStatus.RUNNING
        s.commit()
        data = build_report_data(s, audit)
        html = _env.get_template("report.html").render(
            r=data, dumps=lambda o: json.dumps(o, indent=1, default=str)
        )
        store = get_store()
        hkey = report_key(rep.org_id, rep.id, "datacourt-audit-report.html")
        jkey = report_key(rep.org_id, rep.id, "datacourt-audit-report.json")
        store.put_bytes(hkey, html.encode(), "text/html; charset=utf-8")
        store.put_bytes(jkey, json.dumps(data, indent=1, default=str).encode(), "application/json")
        rep.html_object_key, rep.json_object_key = hkey, jkey
        rep.status = RunStatus.COMPLETED
        ledger.record(
            s,
            org_id=rep.org_id,
            event_type="report.generated",
            entity_type="audit_report",
            entity_id=rep.id,
            actor_id=rep.created_by,
            dataset_version_id=audit.dataset_version_id,
            payload={"audit_run_id": str(audit.id), "config_hash": audit.config_hash},
        )
        s.commit()
    return {"report_id": str(report_id)}
