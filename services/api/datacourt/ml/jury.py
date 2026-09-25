"""Deterministic jury (`jury-v1`).

Witnesses turn measured signals into typed evidence items (measured / heuristic /
model prediction / model estimate), each arguing for the prosecution (the current data
state is wrong or harmful) or the defense (keep it as is). Reason codes are derived with
configurable thresholds, and an ordered rule list maps reason codes to a verdict. Every
rule evaluation is recorded in a trace, so a verdict can be reproduced and audited.
No LLM is involved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from datacourt import algorithms
from datacourt.enums import EvidenceKind as K
from datacourt.enums import Stance as S
from datacourt.enums import Uncertainty, Verdict
from datacourt.enums import Witness as W

VERSION = algorithms.JURY


@dataclass
class CaseInputs:
    label: str
    split: str
    # model
    p_given: float | None = None
    pred_label: str | None = None
    p_pred: float | None = None
    model_ece: float | None = None
    out_of_sample: bool = True
    # neighbors / embedding
    k: int = 0
    neighbor_same: float | None = None
    neighbor_major_label: str | None = None
    neighbor_major_share: float = 0.0
    centroid_ratio: float | None = None
    nearest_other_label: str | None = None
    density_pct: float | None = None
    # quality
    quality: list[dict] = field(default_factory=list)  # {type, severity, description, value, threshold}
    # duplicates / leakage
    family: dict | None = None  # {number, size, kind, is_root, relation, label_conflict, splits, labels}
    leakage: dict | None = None  # {risk, kind, splits}
    # dynamics / influence
    dynamics: dict | None = None  # {category, confidence, variability, forgetting}
    influence: dict | None = None  # {self_pct, harmful, failures_harmed, affected_classes}
    shortcut: list[dict] = field(default_factory=list)  # {cue_label, value, strength_label}
    # composite
    suspicion: float | None = None
    label_action: str | None = None
    hypothesis: str | None = None
    hypothesis_scores: dict | None = None
    privacy: list[dict] = field(default_factory=list)
    reliability: dict = field(default_factory=dict)  # witness -> measured reliability on this dataset
    min_reliability: float = 0.35

    def reliable(self, witness: str) -> bool:
        return self.reliability.get(witness, 1.0) >= self.min_reliability


@dataclass
class Evidence:
    witness: W
    stance: S
    kind: K
    code: str
    title: str
    detail: str
    value: dict[str, Any] = field(default_factory=dict)
    weight: float = 1.0


@dataclass
class Deliberation:
    verdict: Verdict
    reason_codes: list[str]
    rule_trace: list[dict]
    uncertainty: Uncertainty
    scores: dict[str, float]
    priority: float
    impact: float
    primary_concern: str
    categories: list[str]
    target_label: str | None
    est_minutes: float


SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3}


def reason_codes(ci: CaseInputs, cfg: dict) -> list[str]:
    c: list[str] = []
    t = cfg
    if ci.p_given is not None:
        disagree = ci.pred_label is not None and ci.pred_label != ci.label
        if disagree and ci.p_given < t["model_high_disagreement"]:
            c.append("HIGH_MODEL_DISAGREEMENT")
        elif disagree and ci.p_given < t["model_disagreement"]:
            c.append("MODEL_DISAGREEMENT")
        elif ci.p_given >= t["model_agreement"]:
            c.append("MODEL_AGREES")
    if ci.model_ece is not None and ci.model_ece > t["uncalibrated_ece"]:
        c.append("MODEL_POORLY_CALIBRATED")
    if ci.neighbor_same is not None and not ci.reliable("neighbor"):
        c.append("NEIGHBOR_WITNESS_WEAK")
    elif ci.neighbor_same is not None:
        if ci.neighbor_same < t["neighbor_disagreement"] and ci.neighbor_major_share >= 0.5:
            c.append("HIGH_NEIGHBOR_DISAGREEMENT")
        elif ci.neighbor_same >= t["neighbor_agreement"]:
            c.append("NEIGHBORS_AGREE")
    if ci.centroid_ratio is not None and not ci.reliable("centroid"):
        c.append("EMBEDDING_WITNESS_WEAK")
    elif ci.centroid_ratio is not None:
        if ci.centroid_ratio >= t["outside_cluster_ratio"]:
            c.append("OUTSIDE_CLASS_CLUSTER")
        elif ci.centroid_ratio < t["inside_cluster_ratio"]:
            c.append("INSIDE_CLASS_CLUSTER")
    if (
        ci.pred_label
        and ci.pred_label != ci.label
        and ci.pred_label == ci.neighbor_major_label == ci.nearest_other_label
        and ci.reliable("neighbor")
        and ci.reliable("centroid")
    ):
        c.append("CONSISTENT_ALTERNATIVE_LABEL")
    if ci.density_pct is not None and ci.density_pct <= t["low_density_pct"]:
        c.append("LOW_DENSITY_REGION")
    worst = max((SEVERITY_ORDER[q["severity"]] for q in ci.quality), default=0)
    if worst >= 3:
        c.append("SEVERE_QUALITY_ISSUE")
    elif worst == 2:
        c.append("QUALITY_CONCERN")
    else:
        c.append("IMAGE_QUALITY_OK")
    if ci.family:
        if ci.family.get("label_conflict"):
            c.append("DUPLICATE_LABEL_CONFLICT")
        if not ci.family.get("is_root") and ci.family.get("relation") == "exact":
            c.append("EXACT_DUPLICATE_COPY")
        elif not ci.family.get("is_root"):
            c.append("NEAR_DUPLICATE_MEMBER")
    if ci.leakage:
        if ci.leakage["risk"] in ("high", "critical"):
            c.append("CROSS_SPLIT_LEAKAGE_HIGH")
        elif ci.leakage["risk"] == "medium":
            c.append("CROSS_SPLIT_LEAKAGE")
        else:
            c.append("SIMILAR_SUBJECT_ACROSS_SPLITS")
    else:
        c.append("NO_LEAKAGE")
    if ci.dynamics:
        dyn_code = {
            "consistently_hard": "HARD_TO_LEARN",
            "forgotten": "FORGOTTEN_DURING_TRAINING",
            "ambiguous": "AMBIGUOUS_TRAINING",
            "easy": "EASY_TO_LEARN",
            "unstable": "UNSTABLE_TRAINING",
        }.get(str(ci.dynamics.get("category")))
        if dyn_code:
            c.append(dyn_code)
    if ci.influence:
        if ci.influence.get("self_pct", 0) >= t["high_self_influence_pct"]:
            c.append("HIGH_SELF_INFLUENCE")
        if ci.influence.get("failures_harmed", 0) >= t["harms_failures_min"]:
            c.append("HARMS_EVAL_FAILURES")
    if ci.shortcut:
        c.append("SHORTCUT_GROUP_MEMBER")
    hyp = ci.hypothesis
    if hyp == "rare_valid" or ci.label_action == "LIKELY_RARE":
        c.append("RARE_HYPOTHESIS")
    elif hyp == "likely_mislabeled":
        c.append("MISLABEL_HYPOTHESIS")
    elif hyp == "ambiguous":
        c.append("AMBIGUOUS_HYPOTHESIS")
    elif hyp == "domain_shifted":
        c.append("DOMAIN_SHIFT_HYPOTHESIS")
    if ci.privacy:
        c.append("PRIVACY_SENSITIVE_CONTENT")
    if ci.split in ("val", "test"):
        c.append("EVALUATION_SAMPLE")
    return c


def witnesses(ci: CaseInputs) -> list[Evidence]:
    ev: list[Evidence] = []
    if ci.p_given is not None:
        disagree = ci.pred_label != ci.label
        ev.append(
            Evidence(
                W.MODEL,
                S.PROSECUTION
                if disagree and ci.p_given < 0.4
                else S.DEFENSE
                if ci.p_given >= 0.6
                else S.NEUTRAL,
                K.MODEL_PREDICTION,
                "MODEL_PREDICTION",
                f"Baseline model predicts '{ci.pred_label}'"
                + ("" if not disagree else f" instead of '{ci.label}'"),
                (
                    f"{'Out-of-fold' if ci.out_of_sample else 'In-sample'} probability of the given label is {ci.p_given:.2f}; "
                    f"top prediction '{ci.pred_label}' at {ci.p_pred:.2f}. A model prediction is evidence, not ground truth."
                    + (f" Calibration error (ECE) is {ci.model_ece:.3f}." if ci.model_ece is not None else "")
                ),
                {"p_given": ci.p_given, "pred": ci.pred_label, "p_pred": ci.p_pred, "ece": ci.model_ece},
                weight=abs(0.5 - ci.p_given) * 2,
            )
        )
    if ci.neighbor_same is not None:
        against = ci.neighbor_same < 0.4 and ci.neighbor_major_share >= 0.5
        weak = not ci.reliable("neighbor")
        ev.append(
            Evidence(
                W.NEIGHBOR,
                S.NEUTRAL
                if weak
                else S.PROSECUTION
                if against
                else S.DEFENSE
                if ci.neighbor_same >= 0.6
                else S.NEUTRAL,
                K.MEASURED,
                "NEIGHBOR_LABELS",
                f"{ci.neighbor_same:.0%} of nearest neighbours share the label '{ci.label}'",
                (
                    f"Similarity-weighted vote over the {ci.k} nearest samples (duplicates of this sample excluded). "
                    + (
                        f"Dominant other label: '{ci.neighbor_major_label}' ({ci.neighbor_major_share:.0%})."
                        if ci.neighbor_major_label
                        else ""
                    )
                    + (
                        f" This witness is weak on this dataset (reliability {ci.reliability.get('neighbor', 0):.2f}), so it is not counted."
                        if weak
                        else ""
                    )
                ),
                {
                    "same_label_share": round(ci.neighbor_same, 4),
                    "major_other": ci.neighbor_major_label,
                    "major_other_share": round(ci.neighbor_major_share, 4),
                    "k": ci.k,
                    "reliability": ci.reliability.get("neighbor"),
                },
                weight=0.0 if weak else abs(0.5 - ci.neighbor_same) * 2,
            )
        )
    if ci.centroid_ratio is not None:
        outside = ci.centroid_ratio >= 0.55
        weak_c = not ci.reliable("centroid")
        ev.append(
            Evidence(
                W.EMBEDDING,
                S.NEUTRAL
                if weak_c
                else S.PROSECUTION
                if outside
                else S.DEFENSE
                if ci.centroid_ratio < 0.45
                else S.NEUTRAL,
                K.MEASURED,
                "CLASS_CENTROID",
                (
                    f"Closer to the '{ci.nearest_other_label}' class centre than to '{ci.label}'"
                    if outside
                    else f"Sits within the '{ci.label}' class region"
                ),
                (
                    f"Centroid distance ratio {ci.centroid_ratio:.2f} (own / (own + nearest other)); values above 0.5 mean "
                    "the sample is closer to another class in embedding space."
                    + (
                        f" Local density percentile {ci.density_pct:.0%}."
                        if ci.density_pct is not None
                        else ""
                    )
                    + (
                        f" Class centroids separate classes poorly on this dataset (reliability {ci.reliability.get('centroid', 0):.2f}), so this witness is not counted."
                        if weak_c
                        else ""
                    )
                ),
                {
                    "centroid_ratio": round(ci.centroid_ratio, 4),
                    "nearest_other": ci.nearest_other_label,
                    "density_pct": None if ci.density_pct is None else round(ci.density_pct, 4),
                    "reliability": ci.reliability.get("centroid"),
                },
                weight=0.0 if weak_c else abs(0.5 - ci.centroid_ratio) * 2,
            )
        )
    if ci.quality:
        worst = max(ci.quality, key=lambda q: SEVERITY_ORDER[q["severity"]])
        ev.append(
            Evidence(
                W.QUALITY,
                S.PROSECUTION if SEVERITY_ORDER[worst["severity"]] >= 2 else S.NEUTRAL,
                K.MEASURED,
                "QUALITY_FINDINGS",
                f"{len(ci.quality)} potential quality issue(s); worst: {worst['type'].replace('_', ' ')}",
                " ".join(q["description"] for q in ci.quality[:4]),
                {"findings": ci.quality[:6]},
                weight=SEVERITY_ORDER[worst["severity"]] / 3,
            )
        )
    else:
        ev.append(
            Evidence(
                W.QUALITY,
                S.DEFENSE,
                K.MEASURED,
                "QUALITY_OK",
                "Image quality within normal ranges",
                "No quality rule fired for blur, exposure, contrast, resolution, information content or encoding.",
                {},
                weight=0.4,
            )
        )
    if ci.family:
        f = ci.family
        conflict = f.get("label_conflict")
        ev.append(
            Evidence(
                W.DUPLICATE,
                S.PROSECUTION if (conflict or not f.get("is_root")) else S.NEUTRAL,
                K.MEASURED if f.get("relation") == "exact" else K.HEURISTIC,
                "DUPLICATE_FAMILY",
                f"Member of duplicate family #{f['number']} ({f['size']} samples, {f['kind']})",
                (
                    (
                        "This is the source-like (highest resolution) member. "
                        if f.get("is_root")
                        else f"Relation to its parent: {str(f.get('relation', 'near')).replace('_', ' ')}. "
                    )
                    + (
                        f"Family carries conflicting labels: {', '.join(f.get('labels', []))}."
                        if conflict
                        else "Family labels agree."
                    )
                ),
                {
                    k: f[k]
                    for k in ("number", "size", "kind", "relation", "is_root", "label_conflict", "splits")
                    if k in f
                },
                weight=1.0 if conflict else 0.6,
            )
        )
    if ci.leakage:
        lk = ci.leakage
        ev.append(
            Evidence(
                W.SPLIT,
                S.PROSECUTION,
                K.MEASURED if lk["kind"] == "exact_cross_split" else K.HEURISTIC,
                "SPLIT_LEAKAGE",
                f"Possible evaluation leakage across {', '.join(lk['splits'])} ({lk['risk']} risk)",
                (
                    "A visually matching sample exists in another split, so evaluation on it may be inflated. "
                    "Similarity is evidence of overlap, not proof of identical subjects unless the files are byte-identical."
                ),
                lk,
                weight={"low": 0.3, "medium": 0.6, "high": 0.9, "critical": 1.0}[lk["risk"]],
            )
        )
    else:
        ev.append(
            Evidence(
                W.SPLIT,
                S.DEFENSE,
                K.MEASURED,
                "NO_LEAKAGE",
                "No cross-split duplicate found",
                "No exact, transformed or near-duplicate counterpart in another split.",
                {},
                weight=0.3,
            )
        )
    if ci.dynamics:
        d = ci.dynamics
        cat = d["category"]
        ev.append(
            Evidence(
                W.DYNAMICS,
                S.PROSECUTION
                if cat in ("consistently_hard", "forgotten")
                else S.DEFENSE
                if cat == "easy"
                else S.NEUTRAL,
                K.MODEL_ESTIMATE,
                "TRAINING_DYNAMICS",
                f"Training behaviour: {cat.replace('_', ' ')}",
                (
                    f"Across epochs the given label's probability averaged {d['confidence']:.2f} (variability {d['variability']:.2f}); "
                    f"forgotten {d['forgetting']} time(s). Hard-to-learn samples are over-represented among label errors, "
                    "but also include rare valid cases."
                ),
                d,
                weight=0.6,
            )
        )
    if ci.influence:
        inf = ci.influence
        ev.append(
            Evidence(
                W.INFLUENCE,
                S.PROSECUTION if inf.get("failures_harmed", 0) >= 1 else S.NEUTRAL,
                K.MODEL_ESTIMATE,
                "TRACIN_INFLUENCE",
                f"Estimated influence: self-influence percentile {inf['self_pct']:.0%}, pushes {inf.get('failures_harmed', 0)} evaluation failure(s)",
                (
                    "TracIn approximation on the linear head's training checkpoints. High self-influence often marks memorized "
                    "outliers or label errors; harmful influence means training on it increased loss on failed evaluation samples."
                ),
                inf,
                weight=0.5,
            )
        )
    for sc in ci.shortcut[:2]:
        ev.append(
            Evidence(
                W.SHORTCUT,
                S.NEUTRAL,
                K.HEURISTIC,
                "SHORTCUT_CUE",
                f"Part of a {sc['strength_label']} '{sc['cue_label']}' = '{sc['value']}' association",
                "This sample shares a non-content cue strongly associated with its class. It is not wrong by itself, "
                "but the class may be learnable from the cue rather than the object.",
                sc,
                weight=0.3,
            )
        )
    if ci.hypothesis == "rare_valid":
        ev.append(
            Evidence(
                W.EMBEDDING,
                S.DEFENSE,
                K.HEURISTIC,
                "RARE_VALID_HYPOTHESIS",
                "Evidence is consistent with a rare but valid example",
                "Isolated in embedding space without neighbours or a centroid pointing to another class. "
                "Unusual data is not automatically bad data.",
                ci.hypothesis_scores or {},
                weight=0.8,
            )
        )
    for p in ci.privacy[:2]:
        ev.append(
            Evidence(
                W.PRIVACY,
                S.NEUTRAL,
                K.HEURISTIC,
                "PRIVACY_CONTENT",
                f"Potential privacy-sensitive content: {p['kind'].replace('_', ' ')}",
                "Detector-based flag only; no identity recognition is performed.",
                p,
                weight=0.2,
            )
        )
    return ev


def deliberate(ci: CaseInputs, cfg: dict, profile_has: set[str]) -> Deliberation:
    codes = reason_codes(ci, cfg)
    has = set(codes).__contains__
    trace: list[dict] = []
    verdict: Verdict | None = None
    target: str | None = None

    def rule(name: str, cond: bool, v: Verdict) -> None:
        nonlocal verdict
        trace.append({"rule": name, "matched": bool(cond) and verdict is None, "evaluated": verdict is None})
        if verdict is None and cond:
            verdict = v

    model_d = has("HIGH_MODEL_DISAGREEMENT") or has("MODEL_DISAGREEMENT")
    three = [model_d, has("HIGH_NEIGHBOR_DISAGREEMENT"), has("OUTSIDE_CLASS_CLUSTER")]
    rule(
        "R1_leakage_high_or_medium",
        has("CROSS_SPLIT_LEAKAGE_HIGH") or has("CROSS_SPLIT_LEAKAGE"),
        Verdict.LEAKAGE_ACTION_NEEDED,
    )
    rule(
        "R2_redundant_exact_copy",
        has("EXACT_DUPLICATE_COPY") and not has("DUPLICATE_LABEL_CONFLICT"),
        Verdict.POSSIBLE_REMOVE,
    )
    rule("R3_duplicate_label_conflict", has("DUPLICATE_LABEL_CONFLICT"), Verdict.STRONG_REVIEW)
    rule(
        "R4_severe_quality_unlearnable",
        has("SEVERE_QUALITY_ISSUE") and (model_d or has("HARD_TO_LEARN")) and not has("RARE_HYPOTHESIS"),
        Verdict.POSSIBLE_REMOVE,
    )
    consistent_mislabel = (
        has("HIGH_MODEL_DISAGREEMENT")
        and has("HIGH_NEIGHBOR_DISAGREEMENT")
        and has("OUTSIDE_CLASS_CLUSTER")
        and has("CONSISTENT_ALTERNATIVE_LABEL")
        and not has("SEVERE_QUALITY_ISSUE")
    )
    rule("R5_consistent_alternative_label", consistent_mislabel, Verdict.POSSIBLE_RELABEL)
    if verdict == Verdict.POSSIBLE_RELABEL:
        target = ci.pred_label
    rule(
        "R6_rare_hypothesis",
        has("RARE_HYPOTHESIS") and not has("CONSISTENT_ALTERNATIVE_LABEL"),
        Verdict.LIKELY_RARE,
    )
    rule("R7_two_of_three_label_signals", sum(three) >= 2, Verdict.STRONG_REVIEW)
    conflicting = model_d and has("NEIGHBORS_AGREE") and has("INSIDE_CLASS_CLUSTER")
    rule("R8_conflicting_evidence", conflicting, Verdict.UNCERTAIN)
    single = (
        any(three)
        or has("QUALITY_CONCERN")
        or has("SEVERE_QUALITY_ISSUE")
        or has("SIMILAR_SUBJECT_ACROSS_SPLITS")
        or has("HARMS_EVAL_FAILURES")
        or has("AMBIGUOUS_HYPOTHESIS")
        or has("DOMAIN_SHIFT_HYPOTHESIS")
        or has("NEAR_DUPLICATE_MEMBER")
        or has("MISLABEL_HYPOTHESIS")
    )
    rule("R9_single_signal", single, Verdict.REVIEW)
    rule("R10_default_keep", True, Verdict.KEEP)
    assert verdict is not None

    # Uncertainty
    ev = witnesses(ci)
    pro = sum(e.weight for e in ev if e.stance == S.PROSECUTION)
    de = sum(e.weight for e in ev if e.stance == S.DEFENSE)
    deterministic = has("EXACT_DUPLICATE_COPY") or (ci.leakage or {}).get("kind") == "exact_cross_split"
    if deterministic:
        unc = Uncertainty.LOW
    elif pro > 0.5 and de > 0.5 and min(pro, de) / max(pro, de) > 0.6:
        unc = Uncertainty.HIGH
    elif (
        verdict in (Verdict.UNCERTAIN, Verdict.LIKELY_RARE)
        or has("MODEL_POORLY_CALIBRATED")
        or not {"dynamics", "influence"} <= profile_has
    ):
        unc = Uncertainty.MEDIUM
    else:
        unc = Uncertainty.LOW if pro + de > 0 and abs(pro - de) / (pro + de) > 0.5 else Uncertainty.MEDIUM

    severity = {
        Verdict.LEAKAGE_ACTION_NEEDED: 0.9,
        Verdict.POSSIBLE_RELABEL: 0.85,
        Verdict.STRONG_REVIEW: 0.8,
        Verdict.POSSIBLE_REMOVE: 0.6,
        Verdict.REVIEW: 0.5,
        Verdict.UNCERTAIN: 0.45,
        Verdict.LIKELY_RARE: 0.35,
        Verdict.KEEP: 0.1,
    }[verdict]
    strength = max(
        ci.suspicion or 0.0,
        {"low": 0.3, "medium": 0.6, "high": 0.85, "critical": 1.0}.get(
            (ci.leakage or {}).get("risk", ""), 0.0
        ),
        max((SEVERITY_ORDER[q["severity"]] / 3 for q in ci.quality), default=0.0) * 0.8,
    )
    # Impact: how much acting on this case could matter for the model or its evaluation.
    impact = 0.0
    if ci.influence:
        impact = max(
            impact,
            min(
                1.0,
                0.5 * ci.influence.get("self_pct", 0) + 0.25 * min(ci.influence.get("failures_harmed", 0), 2),
            ),
        )
    if ci.split in ("val", "test"):
        impact = max(impact, 0.6 * strength)  # likely errors in evaluation data distort every reported metric
    if ci.family and not ci.family.get("is_root"):
        impact = max(impact, min(1.0, 0.15 * ci.family.get("size", 1)))
    priority = round(0.45 * severity + 0.4 * strength + 0.15 * impact, 4)

    cats = []
    if (
        model_d
        or has("HIGH_NEIGHBOR_DISAGREEMENT")
        or has("OUTSIDE_CLASS_CLUSTER")
        or has("MISLABEL_HYPOTHESIS")
    ):
        cats.append("label")
    if ci.leakage:
        cats.append("leakage")
    if ci.family:
        cats.append("duplicate")
    if ci.quality:
        cats.append("quality")
    if has("RARE_HYPOTHESIS"):
        cats.append("rare")
    if ci.shortcut:
        cats.append("shortcut")
    if ci.privacy:
        cats.append("privacy")
    if ci.influence and (has("HIGH_SELF_INFLUENCE") or has("HARMS_EVAL_FAILURES")):
        cats.append("influence")

    primary = {
        Verdict.LEAKAGE_ACTION_NEEDED: "evaluation_leakage",
        Verdict.POSSIBLE_RELABEL: "possible_mislabel",
        Verdict.LIKELY_RARE: "rare_sample",
        Verdict.UNCERTAIN: "conflicting_evidence",
    }.get(verdict)
    if primary is None:
        if has("DUPLICATE_LABEL_CONFLICT"):
            primary = "duplicate_label_conflict"
        elif has("EXACT_DUPLICATE_COPY"):
            primary = "redundant_duplicate"
        elif "label" in cats:
            primary = "possible_mislabel"
        elif "quality" in cats:
            primary = "quality"
        elif "duplicate" in cats:
            primary = "near_duplicate"
        elif "influence" in cats:
            primary = "high_influence"
        else:
            primary = "other"
    minutes = 0.75 + (
        0.5 if verdict in (Verdict.POSSIBLE_RELABEL, Verdict.STRONG_REVIEW, Verdict.UNCERTAIN) else 0.0
    )
    if ci.leakage or ci.family:
        minutes += min(2.0, 0.2 * (ci.family or {}).get("size", 2))
    return Deliberation(
        verdict=verdict,
        reason_codes=codes,
        rule_trace=trace,
        uncertainty=unc,
        scores={
            "prosecution_weight": round(pro, 4),
            "defense_weight": round(de, 4),
            "severity": severity,
            "evidence_strength": round(strength, 4),
            "impact": round(impact, 4),
        },
        priority=priority,
        impact=round(impact, 4),
        primary_concern=primary,
        categories=cats,
        target_label=target,
        est_minutes=round(minutes, 2),
    )
