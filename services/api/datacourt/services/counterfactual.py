"""Counterfactual data actions (`counterfactual-proxy-v1`).

Fast first-order estimates of what each alternative action would do to the total
evaluation loss of the baseline head, derived from the TracIn basis computed during the
audit (no retraining). They rank options; a What-if experiment verifies them.

For training sample i with checkpoint probabilities P_c(i) and eval-gradient basis
G_c(i) = Σ_e r_e,c (x_e·x_i + 1):
    influence(i, label y) = Σ_c η_c (P_c(i) − e_y) · G_c(i)
    remove i         → Δloss ≈ +influence(i, y_given)
    relabel i to y'  → Δloss ≈ +influence(i, y_given) − influence(i, y')
Negative Δ means the evaluation loss is expected to fall.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from datacourt import algorithms

VERSION = algorithms.COUNTERFACTUAL


def estimate(
    basis: dict[str, np.ndarray], row: int, given: int, class_names: list[str], n_eval: int
) -> dict[str, Any]:
    G, P, lrs = basis["G"], basis["P"], basis["lrs"]
    scale = float(basis["scale"][0])

    def infl(y: int) -> float:
        tot = 0.0
        for c in range(len(lrs)):
            r = P[c, row].astype(np.float64).copy()
            r[y] -= 1.0
            tot += float(lrs[c]) * float(r @ G[c, row])
        return tot / scale

    base = infl(given)
    per_eval = max(1, n_eval)
    options = [
        {"action": "keep", "delta_eval_loss": 0.0, "note": "Baseline: no change."},
        {
            "action": "remove",
            "delta_eval_loss": round(base / per_eval, 6),
            "note": "Estimated change in mean evaluation loss if this sample is removed from training.",
        },
    ]
    for y, name in enumerate(class_names):
        if y == given:
            continue
        options.append(
            {
                "action": "relabel",
                "target": name,
                "delta_eval_loss": round((base - infl(y)) / per_eval, 6),
                "note": f"Estimated change in mean evaluation loss if relabelled to '{name}'.",
            }
        )
    best = min(options, key=lambda o: o["delta_eval_loss"])
    return {
        "algorithm": VERSION,
        "influence_on_eval_loss": round(base / per_eval, 6),
        "options": options,
        "best_estimated": best,
        "caveat": "First-order TracIn estimate for the linear baseline head; not a retraining result. "
        "Launch a What-if experiment to verify.",
    }


def qualitative_options(split: str, in_leakage: bool, is_rare: bool, density_pct: float | None) -> list[dict]:
    out = []
    if in_leakage:
        out.append(
            {
                "action": "move_split",
                "note": "Move or drop the evaluation copy so evaluation measures generalization "
                "(no model-loss estimate: this changes the evaluation set itself).",
            }
        )
    if density_pct is not None and density_pct < 0.1:
        out.append(
            {
                "action": "collect_more_neighbors",
                "note": "Sample sits in a sparse region; collecting similar examples "
                "strengthens coverage instead of deleting evidence.",
            }
        )
    if is_rare:
        out.append(
            {"action": "mark_as_rare", "note": "Keep and mark as a valuable rare example; no model change."}
        )
    return out
