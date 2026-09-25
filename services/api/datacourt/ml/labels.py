"""Label forensics (`label-forensics-v1`) and Rare-or-Wrong hypotheses (`rare-or-wrong-v1`).

Model disagreement is one signal, never a verdict. The suspicion score is an *evidence
score* in [0, 1] (a weighted mean of normalized signals) — not a probability that the
label is wrong. Hypothesis scores in Rare-or-Wrong are likewise evidence scores.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from datacourt import algorithms
from datacourt.enums import Hypothesis, LabelAction

LABEL_VERSION = algorithms.LABELS
RARE_VERSION = algorithms.RARE_OR_WRONG

DYNAMICS_SIGNAL = {"consistently_hard": 1.0, "forgotten": 0.7, "ambiguous": 0.5, "unstable": 0.4, "easy": 0.0}


@dataclass
class LabelSignals:
    p_given: np.ndarray  # calibrated out-of-sample probability of the given label
    pred: np.ndarray  # predicted class index
    p_pred: np.ndarray
    top12_gap: np.ndarray
    neighbor_same: np.ndarray  # similarity-weighted share of neighbors with the same label
    neighbor_major: np.ndarray  # class index of the dominant *other* label among neighbors (-1 none)
    neighbor_major_share: np.ndarray
    neighbor_dist: list[dict]  # per sample {class_index: share}
    centroid_ratio: np.ndarray  # d_own / (d_own + d_nearest_other); > 0.5 => closer to another class
    nearest_other_class: np.ndarray
    density_pct: np.ndarray  # percentile of local density (0 = most isolated)
    dynamics: list[str | None]
    dup_conflict: np.ndarray  # bool
    quality_severity: np.ndarray  # 0..0.9
    self_influence_pct: np.ndarray | None
    attribute_deviation: np.ndarray  # max robust |z| of capture attributes vs own class
    reliability: dict | None = None  # witness -> reliability in [0, 1] measured on this dataset


def witness_reliability(y: np.ndarray, pred: np.ndarray, n_classes: int) -> float:
    """Chance-corrected balanced accuracy of a witness's own label prediction: (bacc - 1/C) / (1 - 1/C)."""
    from sklearn.metrics import balanced_accuracy_score

    present = len(set(y.tolist()))
    if present < 2 or len(y) == 0:
        return 0.0
    bacc = balanced_accuracy_score(y, pred)
    chance = 1.0 / present
    return float(np.clip((bacc - chance) / (1 - chance), 0.0, 1.0))


def neighbor_label_stats(
    labels: np.ndarray, knn_sims: np.ndarray, knn_idx: np.ndarray, exclude: list[set[int]], n_classes: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[dict]]:
    n = len(labels)
    same = np.zeros(n)
    major = np.full(n, -1)
    major_share = np.zeros(n)
    dists: list[dict] = []
    for i in range(n):
        w = np.zeros(n_classes)
        for c in range(knn_idx.shape[1]):
            j = int(knn_idx[i, c])
            if j < 0 or j in exclude[i]:
                continue
            w[labels[j]] += max(0.0, float(knn_sims[i, c]))
        tot = w.sum()
        if tot <= 0:
            same[i] = 1.0
            dists.append({})
            continue
        share = w / tot
        same[i] = share[labels[i]]
        other = share.copy()
        other[labels[i]] = -1
        k = int(other.argmax())
        major[i] = k if other[k] > 0 else -1
        major_share[i] = max(0.0, float(other[k]))
        dists.append({int(c): round(float(v), 4) for c, v in enumerate(share) if v > 0})
    return same, major, major_share, dists


def centroid_evidence(
    emb: np.ndarray, labels: np.ndarray, fit_mask: np.ndarray, n_classes: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    cents = np.zeros((n_classes, emb.shape[1]), dtype=np.float32)
    for c in range(n_classes):
        m = fit_mask & (labels == c)
        if not m.any():
            m = labels == c
        if m.any():
            v = emb[m].mean(axis=0)
            cents[c] = v / (np.linalg.norm(v) + 1e-12)
    dist = 1.0 - emb @ cents.T  # cosine distance
    own = dist[np.arange(len(labels)), labels]
    other = dist.copy()
    other[np.arange(len(labels)), labels] = np.inf
    nearest_other = other.argmin(axis=1)
    d_other = other.min(axis=1)
    ratio = own / np.maximum(own + d_other, 1e-9)
    return ratio, nearest_other, cents


def effective_weights(cfg: dict, reliability: dict | None) -> dict[str, float]:
    """Configured weights scaled by each witness's measured reliability (floor 0.05)."""
    w = dict(cfg["weights"])
    if reliability:
        for key, rel_key in (
            ("model", "model"),
            ("neighbor", "neighbor"),
            ("centroid", "centroid"),
            ("dynamics", "model"),
        ):
            if rel_key in reliability:
                w[key] = w[key] * max(0.05, float(reliability[rel_key]))
    return w


def score_labels(sig: LabelSignals, cfg: dict) -> dict[str, np.ndarray]:
    w = effective_weights(cfg, sig.reliability)
    s_model = 1.0 - sig.p_given
    s_neighbor = 1.0 - sig.neighbor_same
    s_centroid = np.clip((sig.centroid_ratio - 0.4) / 0.3, 0.0, 1.0)
    s_dyn = np.array([DYNAMICS_SIGNAL.get(d or "", np.nan) for d in sig.dynamics], dtype=np.float64)
    s_dup = sig.dup_conflict.astype(float)
    comps = [
        (s_model, w["model"]),
        (s_neighbor, w["neighbor"]),
        (s_centroid, w["centroid"]),
        (s_dup, w["duplicate_conflict"]),
    ]
    num = sum(v * wt for v, wt in comps)
    den = np.full(len(s_model), float(sum(wt for _, wt in comps)))
    has_dyn = ~np.isnan(s_dyn)
    num = num + np.where(has_dyn, np.nan_to_num(s_dyn) * w["dynamics"], 0.0)
    den = den + np.where(has_dyn, w["dynamics"], 0.0)
    suspicion = num / den

    rare_like = (
        (s_model >= cfg["rare_model_disagreement_min"])
        & (sig.neighbor_major_share < cfg["rare_max_other_neighbor_share"])
        & (sig.density_pct <= cfg["rare_max_density_pct"])
        & (sig.centroid_ratio < cfg["rare_max_centroid_ratio"])
        & (sig.quality_severity < 0.6)
    )
    action = np.where(
        rare_like,
        str(LabelAction.LIKELY_RARE),
        np.where(
            suspicion >= cfg["review_threshold"],
            str(LabelAction.REVIEW),
            np.where(
                suspicion >= cfg["low_priority_threshold"],
                str(LabelAction.LOW_PRIORITY_REVIEW),
                str(LabelAction.NO_ACTION),
            ),
        ),
    )
    return {
        "suspicion": suspicion,
        "s_model": s_model,
        "s_neighbor": s_neighbor,
        "s_centroid": s_centroid,
        "s_dynamics": s_dyn,
        "action": action,
        "weights": w,
    }


def rare_or_wrong(
    sig: LabelSignals, scores: dict[str, np.ndarray], cfg: dict, i: int
) -> tuple[Hypothesis, dict, list[str], list[str]]:
    """Scores competing hypotheses for one sample. Returns (hypothesis, scores, reasons, valuable_reasons)."""
    isolation = 1.0 - sig.density_pct[i]
    other_pred = sig.pred[i] != -1 and sig.p_given[i] < sig.p_pred[i]
    p_other = float(sig.p_pred[i]) if other_pred else 0.0
    consistent_other = (
        other_pred and sig.neighbor_major[i] == sig.pred[i] and sig.nearest_other_class[i] == sig.pred[i]
    )
    s_centroid = float(scores["s_centroid"][i])
    rel = sig.reliability or {}
    wts = np.array(
        [
            max(0.05, rel.get("model", 1.0)),
            max(0.05, rel.get("neighbor", 1.0)),
            max(0.05, rel.get("centroid", 1.0)),
        ]
    )
    mislabeled = float(np.average([p_other, sig.neighbor_major_share[i], s_centroid], weights=wts)) + (
        0.15 if consistent_other else 0.0
    )
    rare = float(
        np.mean(
            [
                isolation,
                1.0 - s_centroid,
                1.0 - sig.neighbor_major_share[i],
                1.0 if sig.quality_severity[i] < 0.3 else 0.3,
            ]
        )
    )
    if p_other >= 0.6:
        rare *= 0.6
    poor = float(sig.quality_severity[i])
    dev = float(sig.attribute_deviation[i])
    shifted = float(isolation * np.clip((dev - 2.0) / 4.0, 0.0, 1.0))
    top_two = sorted(sig.neighbor_dist[i].values(), reverse=True)[:2] if sig.neighbor_dist[i] else [1.0]
    split_neighbors = len(top_two) == 2 and top_two[1] >= 0.3
    ambiguous = float((1.0 - np.clip(sig.top12_gap[i] / 0.3, 0, 1)) * (0.5 + 0.5 * split_neighbors))
    if sig.dynamics[i] == "ambiguous":
        ambiguous = min(1.0, ambiguous + 0.15)
    hyp_scores = {
        str(Hypothesis.LIKELY_MISLABELED): round(min(1.0, mislabeled), 4),
        str(Hypothesis.RARE_VALID): round(rare, 4),
        str(Hypothesis.POOR_QUALITY): round(poor, 4),
        str(Hypothesis.DOMAIN_SHIFTED): round(shifted, 4),
        str(Hypothesis.AMBIGUOUS): round(ambiguous, 4),
    }
    ranked = sorted(hyp_scores.items(), key=lambda kv: -kv[1])
    top, second = ranked[0], ranked[1]
    if top[1] >= cfg["hypothesis_min_score"] and top[1] - second[1] >= cfg["hypothesis_min_gap"]:
        hyp = Hypothesis(top[0])
    else:
        hyp = Hypothesis.UNCERTAIN
    reasons: list[str] = []
    if consistent_other:
        reasons.append(
            "Model prediction, nearest neighbours and nearest class centroid all point to the same other class."
        )
    elif other_pred:
        reasons.append(
            f"Model prefers another class (p={sig.p_pred[i]:.2f}) but other signals do not all agree."
        )
    if sig.neighbor_major_share[i] >= 0.5:
        reasons.append(
            f"{sig.neighbor_major_share[i]:.0%} of similarity-weighted neighbours carry a different label."
        )
    if isolation >= 0.9:
        reasons.append(f"Sample sits in a sparse region (local density percentile {sig.density_pct[i]:.0%}).")
    if poor >= 0.6:
        reasons.append("Measured quality issues could explain unusual model behaviour.")
    if shifted >= 0.4:
        reasons.append(
            f"Capture attributes deviate from its class ({dev:.1f} robust SDs), suggesting a different domain."
        )
    if split_neighbors:
        reasons.append("Neighbours are split between two classes.")
    valuable: list[str] = []
    if isolation >= 0.8:
        valuable.append(
            f"Few similar samples exist (density percentile {sig.density_pct[i]:.0%}); removing it would reduce coverage."
        )
    if s_centroid < 0.3:
        valuable.append("No other class centroid is a closer match than its own class.")
    if sig.neighbor_major_share[i] < 0.4:
        valuable.append("Neighbours do not consistently point to another class.")
    if sig.quality_severity[i] < 0.3:
        valuable.append("Image quality measurements are within normal ranges.")
    if not other_pred or sig.p_pred[i] < 0.6:
        valuable.append("The model is uncertain rather than confidently predicting another class.")
    return (
        hyp,
        hyp_scores,
        reasons,
        valuable if hyp in (Hypothesis.RARE_VALID, Hypothesis.UNCERTAIN, Hypothesis.DOMAIN_SHIFTED) else [],
    )
