"""Dataset Debt (`debt-v1`), Preflight Gate (`preflight-v1`) and Data Contracts (`contract-v1`).

All three are pure functions of an audit's *facts* (counts produced by the pipeline and
the current human-review state), so they are reproducible and explainable. The formula
for every dimension is returned alongside its value.
"""

from __future__ import annotations

from typing import Any

from datacourt import algorithms
from datacourt.enums import DebtLevel, PreflightStatus

DEBT_VERSION = algorithms.DEBT
PREFLIGHT_VERSION = algorithms.PREFLIGHT
CONTRACT_VERSION = algorithms.CONTRACT

LEVELS = [DebtLevel.LOW, DebtLevel.MODERATE, DebtLevel.HIGH, DebtLevel.CRITICAL]


def _level(value: float, moderate: float, high: float, critical: float) -> DebtLevel:
    if value >= critical:
        return DebtLevel.CRITICAL
    if value >= high:
        return DebtLevel.HIGH
    if value >= moderate:
        return DebtLevel.MODERATE
    return DebtLevel.LOW


def _dim(
    name: str,
    value: float,
    thresholds: tuple[float, float, float],
    formula: str,
    metrics: dict,
    recommendation: str,
    contributors: list | None = None,
    floor: DebtLevel | None = None,
    scored: bool = True,
    unit: str = "rate",
) -> dict:
    lvl = _level(value, *thresholds)
    if floor is not None and LEVELS.index(floor) > LEVELS.index(lvl):
        lvl = floor
    return {
        "name": name,
        "level": str(lvl),
        "value": round(float(value), 5),
        "unit": unit,
        "score": round(min(100.0, 100.0 * value / thresholds[2]) if thresholds[2] else 0.0, 1),
        "thresholds": {"moderate": thresholds[0], "high": thresholds[1], "critical": thresholds[2]},
        "formula": formula,
        "metrics": metrics,
        "recommendation": recommendation,
        "contributors": contributors or [],
        "included_in_overall": scored,
    }


def compute_debt(f: dict[str, Any]) -> dict[str, Any]:
    n = max(1, f["sample_count"])
    dims: dict[str, dict] = {}

    label_rate = (f["unresolved_label_strong"] + 0.5 * f["unresolved_label_review"]) / n
    dims["label"] = _dim(
        "Label Debt",
        label_rate,
        (0.01, 0.03, 0.08),
        "(unresolved STRONG_REVIEW/POSSIBLE_RELABEL label cases + 0.5 × unresolved REVIEW label cases) / samples",
        {
            "unresolved_strong": f["unresolved_label_strong"],
            "unresolved_review": f["unresolved_label_review"],
            "samples": n,
        },
        "Work through the label review queue, starting with POSSIBLE_RELABEL cases.",
        f.get("top_label_cases", []),
    )
    ev = f["eval_count"]
    leak_rate = f["eval_samples_leaking"] / ev if ev else 0.0
    dims["leakage"] = _dim(
        "Leakage Debt",
        leak_rate,
        (0.005, 0.02, 0.05),
        "evaluation samples in medium/high/critical cross-split families / evaluation samples (floor HIGH if any exact evaluation duplicate is unresolved)",
        {
            "eval_samples_leaking": f["eval_samples_leaking"],
            "eval_count": ev,
            "exact_eval_leaks": f["exact_eval_leaks"],
            "leakage_findings": f["leakage_by_risk"],
        },
        "Move or remove leaked evaluation copies so evaluation measures generalization."
        if ev
        else "No evaluation split; not applicable.",
        f.get("top_leakage", []),
        floor=DebtLevel.HIGH if f["exact_eval_leaks"] > 0 else None,
    )
    q_rate = f["quality_samples_medium_plus"] / n
    dims["quality"] = _dim(
        "Quality Debt",
        q_rate,
        (0.02, 0.05, 0.12),
        "samples with ≥1 medium/high potential quality issue / samples",
        {"affected_samples": f["quality_samples_medium_plus"], "by_type": f["quality_by_type"]},
        "Review high-severity quality cases; exclude only after confirming the image is unusable.",
    )
    cov_points = (
        2 * f["gaps_high"]
        + f["gaps_medium"]
        + (3 if (f["imbalance_ratio"] or 1) >= 10 else 1 if (f["imbalance_ratio"] or 1) >= 4 else 0)
    )
    dims["coverage"] = _dim(
        "Coverage Debt",
        cov_points,
        (2, 5, 9),
        "2 × high-priority gaps + medium-priority gaps + imbalance points (3 if ratio ≥ 10, 1 if ≥ 4)",
        {
            "gaps_high": f["gaps_high"],
            "gaps_medium": f["gaps_medium"],
            "imbalance_ratio": f["imbalance_ratio"],
        },
        "Follow the Active Collection Planner's high-priority recommendations.",
        unit="points",
    )
    sc = f["shortcut_max_strength"]
    dims["shortcut"] = _dim(
        "Shortcut Debt",
        sc,
        (0.3, 0.5, 0.7),
        "maximum bias-corrected Cramér's V between any non-content cue and the label (training split)",
        {
            "max_cramers_v": sc,
            "strong_findings": f["shortcut_strong"],
            "cue_only_balanced_accuracy": f.get("cue_only_accuracy"),
            "chance": f.get("cue_chance"),
        },
        "Diversify backgrounds/capture conditions per class and test the model with the cue removed.",
        unit="cramers_v",
    )
    dup_rate = f["redundant_samples"] / n
    dims["duplication"] = _dim(
        "Duplication Debt",
        dup_rate,
        (0.02, 0.05, 0.15),
        "Σ(family size − 1) over duplicate families / samples",
        {
            "families": f["duplicate_families"],
            "redundant_samples": f["redundant_samples"],
            "exact_families": f["exact_families"],
        },
        "Keep one source-like member per family unless variants are intentional augmentation.",
    )
    rv_rate = f["unresolved_high_priority"] / n
    dims["review"] = _dim(
        "Review Debt",
        rv_rate,
        (0.01, 0.03, 0.08),
        "(unresolved STRONG_REVIEW + POSSIBLE_RELABEL + POSSIBLE_REMOVE + LEAKAGE_ACTION_NEEDED + disputed cases) / samples",
        {
            "unresolved_high_priority": f["unresolved_high_priority"],
            "disputed": f["disputed_cases"],
            "decided": f["decided_cases"],
        },
        "Use the Review Budget optimizer to clear the highest-value cases first.",
    )
    unknown = 1.0 - f["provenance_coverage"]
    dims["provenance"] = _dim(
        "Provenance Debt",
        unknown,
        (0.1, 0.5, 0.9),
        "fraction of samples without source/license metadata (tracked; excluded from overall by default)",
        {
            "provenance_coverage": f["provenance_coverage"],
            "dataset_level_provenance": f.get("dataset_provenance", {}),
        },
        "Attach a provenance.csv (source, license, collector, consent note) to future uploads.",
        scored=False,
    )
    scored = [d for d in dims.values() if d["included_in_overall"]]
    levels = [DebtLevel(d["level"]) for d in scored]
    if DebtLevel.CRITICAL in levels:
        overall = DebtLevel.CRITICAL
    elif DebtLevel.HIGH in levels or levels.count(DebtLevel.MODERATE) >= 3:
        overall = DebtLevel.HIGH
    elif DebtLevel.MODERATE in levels:
        overall = DebtLevel.MODERATE
    else:
        overall = DebtLevel.LOW
    return {
        "formula_version": DEBT_VERSION,
        "overall": str(overall),
        "overall_rule": "CRITICAL if any scored dimension is CRITICAL; HIGH if any is HIGH or ≥3 are MODERATE; MODERATE if any is MODERATE; else LOW.",
        "dimensions": dims,
    }


def _check(
    cid: str, title: str, status: str, message: str, measured: Any = None, threshold: Any = None
) -> dict:
    return {
        "id": cid,
        "title": title,
        "status": status,
        "message": message,
        "measured": measured,
        "threshold": threshold,
    }


def preflight(f: dict[str, Any], cfg: dict) -> dict[str, Any]:
    checks: list[dict] = []
    unread = f["unreadable_fraction"]
    checks.append(
        _check(
            "readable",
            "Dataset is readable",
            "block" if unread > cfg["block_max_unreadable_fraction"] else "warn" if unread > 0 else "pass",
            f"{unread:.1%} of image files could not be decoded.",
            round(unread, 4),
            cfg["block_max_unreadable_fraction"],
        )
    )
    checks.append(
        _check(
            "min_classes",
            "At least two classes",
            "block" if f["class_count"] < cfg["block_min_classes"] else "pass",
            f"{f['class_count']} class(es) with valid images.",
            f["class_count"],
            cfg["block_min_classes"],
        )
    )
    missing_train = f.get("classes_missing_from_train", [])
    checks.append(
        _check(
            "train_covers_eval",
            "Every evaluated class has training data",
            "block" if missing_train else "pass",
            ("Classes present in evaluation but absent from training: " + ", ".join(missing_train))
            if missing_train
            else "All evaluation classes appear in training.",
            missing_train,
            [],
        )
    )
    missing_eval = f.get("classes_missing_from_eval", [])
    if f["eval_count"]:
        checks.append(
            _check(
                "eval_covers_classes",
                "Every class is evaluated",
                "warn" if missing_eval else "pass",
                ("Classes without evaluation samples: " + ", ".join(missing_eval))
                if missing_eval
                else "All classes have evaluation samples.",
                missing_eval,
                [],
            )
        )
    else:
        checks.append(
            _check(
                "has_eval_split",
                "Evaluation split present",
                "warn",
                "No validation/test split was found; metrics are cross-validated estimates.",
                0,
                1,
            )
        )
    exact = f["exact_eval_leaks"]
    checks.append(
        _check(
            "exact_eval_leakage",
            "No exact duplicates across train/evaluation",
            "block" if exact and cfg["block_on_exact_eval_leakage"] else "warn" if exact else "pass",
            f"{exact} unresolved exact duplicate evaluation sample(s) also appear in another split.",
            exact,
            0,
        )
    )
    leak = f["eval_samples_leaking"]
    checks.append(
        _check(
            "near_eval_leakage",
            "Limited near-duplicate leakage",
            "warn" if leak > exact else "pass",
            f"{leak} evaluation sample(s) belong to cross-split duplicate families.",
            leak,
            0,
        )
    )
    crit_frac = f["unresolved_high_priority"] / max(1, f["sample_count"])
    checks.append(
        _check(
            "critical_cases",
            "Critical cases under control",
            "block"
            if crit_frac > cfg["block_unresolved_critical_fraction"]
            else "warn"
            if f["unresolved_high_priority"]
            else "pass",
            f"{f['unresolved_high_priority']} high-priority case(s) unresolved ({crit_frac:.1%} of samples).",
            round(crit_frac, 4),
            cfg["block_unresolved_critical_fraction"],
        )
    )
    ir = f["imbalance_ratio"] or 1.0
    checks.append(
        _check(
            "imbalance",
            "Class balance",
            "warn" if ir > cfg["warn_imbalance_ratio"] else "pass",
            f"Largest/smallest class ratio is {ir:.1f}.",
            round(ir, 2),
            cfg["warn_imbalance_ratio"],
        )
    )
    checks.append(
        _check(
            "min_per_class",
            "Minimum images per class",
            "warn" if f["min_class_count"] < cfg["warn_min_per_class"] else "pass",
            f"Smallest class has {f['min_class_count']} images.",
            f["min_class_count"],
            cfg["warn_min_per_class"],
        )
    )
    checks.append(
        _check(
            "shortcut",
            "No strong shortcut cue",
            "warn" if f["shortcut_strong"] else "pass",
            f"{f['shortcut_strong']} strong cue–label association(s).",
            f["shortcut_strong"],
            0,
        )
    )
    checks.append(
        _check(
            "coverage",
            "No high-priority coverage gaps",
            "warn" if f["gaps_high"] else "pass",
            f"{f['gaps_high']} high-priority coverage gap(s).",
            f["gaps_high"],
            0,
        )
    )
    checks.append(
        _check(
            "review_queue",
            "Review queue addressed",
            "warn" if f["unresolved_cases"] >= cfg["warn_unresolved_review"] else "pass",
            f"{f['unresolved_cases']} open or disputed case(s).",
            f["unresolved_cases"],
            cfg["warn_unresolved_review"],
        )
    )
    unknown = 1 - f["provenance_coverage"]
    checks.append(
        _check(
            "provenance",
            "Provenance recorded",
            "warn" if unknown > cfg["warn_provenance_unknown_fraction"] else "pass",
            f"{unknown:.0%} of samples have no provenance metadata.",
            round(unknown, 4),
            cfg["warn_provenance_unknown_fraction"],
        )
    )
    status = (
        PreflightStatus.BLOCKED
        if any(c["status"] == "block" for c in checks)
        else PreflightStatus.READY_WITH_WARNINGS
        if any(c["status"] == "warn" for c in checks)
        else PreflightStatus.READY
    )
    return {"rules_version": PREFLIGHT_VERSION, "status": str(status), "checks": checks}


# ---------------------------------------------------------------------------
# Data contracts
# ---------------------------------------------------------------------------

CONTRACT_METRICS: dict[str, tuple[str, str]] = {
    "exact_cross_split_duplicates": ("Exact duplicate evaluation samples across splits", "exact_eval_leaks"),
    "leakage_eval_samples": ("Evaluation samples in cross-split duplicate families", "eval_samples_leaking"),
    "leakage_findings_high": ("High/critical leakage findings", "leakage_high_count"),
    "min_images_per_class": ("Smallest class size", "min_class_count"),
    "class_imbalance_ratio": ("Largest / smallest class ratio", "imbalance_ratio"),
    "unresolved_strong_review_cases": (
        "Unresolved STRONG_REVIEW / POSSIBLE_RELABEL cases",
        "unresolved_label_strong",
    ),
    "unresolved_high_priority_cases": ("Unresolved high-priority cases", "unresolved_high_priority"),
    "quality_issue_rate": ("Share of samples with medium/high quality issues", "quality_rate"),
    "duplicate_rate": ("Share of redundant duplicate samples", "duplicate_rate"),
    "shortcut_max_cramers_v": ("Strongest cue–label association", "shortcut_max_strength"),
    "unreadable_fraction": ("Share of undecodable files", "unreadable_fraction"),
    "debt_level": ("Overall Dataset Debt level", "debt_level_rank"),
    "required_splits": ("Required splits present", "splits_present"),
    "preflight_status": ("Preflight status", "preflight_rank"),
}
OPS = {
    "<=": lambda a, b: a <= b,
    "<": lambda a, b: a < b,
    ">=": lambda a, b: a >= b,
    ">": lambda a, b: a > b,
    "==": lambda a, b: a == b,
    "includes": lambda a, b: set(b) <= set(a),
}
LEVEL_RANK = {"LOW": 0, "MODERATE": 1, "HIGH": 2, "CRITICAL": 3}
PREFLIGHT_RANK = {"READY": 0, "READY_WITH_WARNINGS": 1, "BLOCKED": 2}

DEFAULT_CONTRACT = [
    {"metric": "exact_cross_split_duplicates", "op": "<=", "value": 0},
    {"metric": "leakage_findings_high", "op": "<=", "value": 0},
    {"metric": "min_images_per_class", "op": ">=", "value": 30},
    {"metric": "unresolved_strong_review_cases", "op": "<=", "value": 20},
    {"metric": "class_imbalance_ratio", "op": "<=", "value": 10},
    {"metric": "required_splits", "op": "includes", "value": ["train"]},
]


def validate_rules(rules: list[dict]) -> list[str]:
    errors = []
    for i, r in enumerate(rules):
        if r.get("metric") not in CONTRACT_METRICS:
            errors.append(f"rule {i}: unknown metric {r.get('metric')!r}")
        if r.get("op") not in OPS:
            errors.append(f"rule {i}: unknown operator {r.get('op')!r}")
        if r.get("op") == "includes" and not isinstance(r.get("value"), list):
            errors.append(f"rule {i}: 'includes' needs a list value")
        elif r.get("op") != "includes":
            v = r.get("value")
            if r.get("metric") == "debt_level" and isinstance(v, str):
                continue
            if r.get("metric") == "preflight_status" and isinstance(v, str):
                continue
            if not isinstance(v, (int, float)):
                errors.append(f"rule {i}: value must be a number")
    return errors


def evaluate_contract(rules: list[dict], f: dict[str, Any]) -> dict[str, Any]:
    results = []
    for r in rules:
        title, key = CONTRACT_METRICS[r["metric"]]
        actual = f.get(key)
        expected = r["value"]
        if r["metric"] == "debt_level" and isinstance(expected, str):
            expected = LEVEL_RANK[expected]
        if r["metric"] == "preflight_status" and isinstance(expected, str):
            expected = PREFLIGHT_RANK[expected]
        passed = actual is not None and OPS[r["op"]](actual, expected)
        results.append(
            {
                "metric": r["metric"],
                "title": title,
                "op": r["op"],
                "expected": r["value"],
                "actual": actual,
                "passed": bool(passed),
            }
        )
    return {
        "contract_version": CONTRACT_VERSION,
        "passed": all(x["passed"] for x in results),
        "results": results,
    }
