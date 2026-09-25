"""Review Budget optimizer (`review-budget-v1`).

Greedy selection under a budget (items or minutes) maximizing an objective-weighted
value with diminishing returns: a second case from the same duplicate family or the same
class is worth less than the first, so the queue covers more distinct problems. Greedy
selection on a submodular objective is a standard, near-optimal (1 - 1/e) approach.

"Expected issue coverage" is the share of total case value (risk mass) covered by the
selection — a statement about the ranking, not a guaranteed model improvement.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from datacourt import algorithms

VERSION = algorithms.REVIEW_BUDGET

OBJECTIVES: dict[str, dict[str, float]] = {
    "balanced": {
        "risk": 1.0,
        "impact": 0.6,
        "uncertainty": 0.3,
        "leakage": 0.6,
        "label": 0.6,
        "quality": 0.3,
        "coverage": 0.3,
    },
    "leakage": {
        "risk": 0.5,
        "impact": 0.3,
        "uncertainty": 0.1,
        "leakage": 2.0,
        "label": 0.2,
        "quality": 0.1,
        "coverage": 0.1,
    },
    "label_errors": {
        "risk": 0.8,
        "impact": 0.4,
        "uncertainty": 0.2,
        "leakage": 0.1,
        "label": 2.0,
        "quality": 0.1,
        "coverage": 0.2,
    },
    "model_impact": {
        "risk": 0.5,
        "impact": 2.0,
        "uncertainty": 0.2,
        "leakage": 0.6,
        "label": 0.5,
        "quality": 0.1,
        "coverage": 0.2,
    },
    "quality": {
        "risk": 0.5,
        "impact": 0.2,
        "uncertainty": 0.1,
        "leakage": 0.1,
        "label": 0.2,
        "quality": 2.0,
        "coverage": 0.1,
    },
}
UNC = {"low": 0.2, "medium": 0.5, "high": 1.0}


def case_value(c: dict, w: dict[str, float]) -> float:
    cats = set(c["categories"])
    strength = float(c.get("strength", c["priority"]))
    v = (
        w["risk"] * c["priority"]
        + w["impact"] * c["impact"]
        + w["uncertainty"] * UNC.get(c["uncertainty"], 0.5) * 0.3
    )
    if "leakage" in cats:
        v += w["leakage"] * strength
    if "label" in cats:
        v += w["label"] * strength
    if "quality" in cats:
        v += w["quality"] * 0.6 * strength
    if c["split"] in ("val", "test"):
        v += w["coverage"] * 0.2 * strength
    if c["verdict"] == "LIKELY_RARE":
        v *= 0.6  # rare samples mostly need confirmation, not action
    return max(0.0, v)


def optimize(
    cases: list[dict],
    *,
    max_items: int | None,
    max_minutes: float | None,
    objective: str,
    diminishing: float = 0.5,
) -> dict[str, Any]:
    if objective not in OBJECTIVES:
        raise ValueError(f"unknown objective {objective!r}")
    w = OBJECTIVES[objective]
    open_cases = [c for c in cases if c["status"] in ("open", "disputed")]
    for c in open_cases:
        c["value"] = case_value(c, w)
    total_value = sum(c["value"] for c in open_cases) or 1.0
    fam_count: dict[Any, int] = defaultdict(int)
    cls_count: dict[str, int] = defaultdict(int)
    chosen: list[dict] = []
    minutes = 0.0
    remaining = list(open_cases)
    while remaining:
        if max_items is not None and len(chosen) >= max_items:
            break
        best, best_ratio, best_gain = None, -1.0, 0.0
        for c in remaining:
            if max_minutes is not None and minutes + c["est_minutes"] > max_minutes:
                continue
            factor = diminishing ** fam_count[c["family_id"]] if c["family_id"] else 1.0
            factor *= 0.85 ** min(cls_count[c["label"]], 10)
            gain = c["value"] * factor
            ratio = gain / max(0.25, c["est_minutes"])
            if ratio > best_ratio:
                best, best_ratio, best_gain = c, ratio, gain
        if best is None:
            break
        remaining.remove(best)
        best["marginal_value"] = round(best_gain, 4)
        chosen.append(best)
        minutes += best["est_minutes"]
        if best["family_id"]:
            fam_count[best["family_id"]] += 1
        cls_count[best["label"]] += 1
    covered = sum(c["value"] for c in chosen)
    by_cat: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for c in open_cases:
        for cat in c["categories"] or ["other"]:
            by_cat[cat][1] += 1
    for c in chosen:
        for cat in c["categories"] or ["other"]:
            by_cat[cat][0] += 1
    high = [
        c
        for c in open_cases
        if c["verdict"] in ("POSSIBLE_RELABEL", "STRONG_REVIEW", "LEAKAGE_ACTION_NEEDED", "POSSIBLE_REMOVE")
    ]
    high_chosen = [c for c in chosen if c in high]
    for rank, c in enumerate(chosen, start=1):
        c["rank"] = rank
        c["reason"] = _reason(c, objective)
    return {
        "algorithm": VERSION,
        "objective": objective,
        "weights": w,
        "selected": chosen,
        "selected_count": len(chosen),
        "open_cases": len(open_cases),
        "estimated_minutes": round(minutes, 1),
        "expected_issue_coverage": round(covered / total_value, 4),
        "high_impact_coverage": round(len(high_chosen) / len(high), 4) if high else None,
        "coverage_by_category": {k: {"selected": v[0], "open": v[1]} for k, v in sorted(by_cat.items())},
        "distinct_families": len({c["family_id"] for c in chosen if c["family_id"]}),
        "distinct_classes": len({c["label"] for c in chosen}),
        "disclaimer": "Coverage is the share of total case value (evidence risk × impact) in the queue. It ranks review effort; it does not guarantee a model improvement.",
    }


def _reason(c: dict, objective: str) -> str:
    parts = [f"{c['verdict'].replace('_', ' ').lower()} (priority {c['priority']:.2f})"]
    if "leakage" in c["categories"]:
        parts.append("affects evaluation integrity")
    if c["impact"] >= 0.5:
        parts.append(f"high estimated impact ({c['impact']:.2f})")
    if c["split"] in ("val", "test"):
        parts.append(f"{c['split']} sample — label errors here distort metrics")
    if c.get("uncertainty") == "high":
        parts.append("evidence conflicts; human judgment most valuable")
    return "; ".join(parts) + f" [objective: {objective}]"
