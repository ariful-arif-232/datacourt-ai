"""Coverage map, blind spots and the Active Collection Planner (`coverage-v1`).

* 2-D map: t-SNE (deterministic seed) on up to `tsne_max` samples; remaining samples are
  placed at the similarity-weighted mean of their nearest mapped neighbours. PCA fallback.
* Regions: k-means on embeddings (fixed seed); per-region class/split composition,
  purity and density.
* Gaps: under-represented classes, weak class modes, class boundary zones, evaluation
  blind spots, train-coverage gaps, and capture-condition gaps (e.g. a class with no
  low-light images while other classes have many).
* Planner: turns gaps into collection recommendations with a transparent priority score
  and a heuristic quantity range. It recommends what to *collect*; it never fabricates data.
"""

from __future__ import annotations

import math
from collections import Counter

import numpy as np
from sklearn.cluster import KMeans
from sklearn.decomposition import PCA
from sklearn.manifold import TSNE

from datacourt import algorithms
from datacourt.ml.knn import knn

VERSION = algorithms.COVERAGE


def project_2d(emb: np.ndarray, tsne_max: int, seed: int) -> tuple[np.ndarray, str]:
    n = emb.shape[0]
    if n < 5:
        return np.zeros((n, 2), dtype=np.float32), "none"
    if n <= 30 or tsne_max <= 0:
        return PCA(2, random_state=seed).fit_transform(emb).astype(np.float32), "pca"
    rng = np.random.default_rng(seed)
    sub = np.sort(rng.choice(n, size=min(n, tsne_max), replace=False))
    red = PCA(min(50, emb.shape[1], len(sub) - 1), random_state=seed).fit(emb[sub])
    Z = red.transform(emb[sub])
    perplexity = float(min(30, max(5, (len(sub) - 1) / 4)))
    coords_sub = TSNE(
        2, perplexity=perplexity, init="pca", random_state=seed, learning_rate="auto", max_iter=750
    ).fit_transform(Z)
    coords = np.zeros((n, 2), dtype=np.float32)
    coords[sub] = coords_sub
    rest = np.setdiff1d(np.arange(n), sub)
    if len(rest):
        sims, idx = knn(emb[rest], emb[sub], 5)
        w = np.clip(sims, 1e-3, None)
        coords[rest] = (coords_sub[idx] * w[..., None]).sum(1) / w.sum(1, keepdims=True)
    method = "tsne" if len(rest) == 0 else "tsne+knn-placement"
    # Normalize to [-1, 1] for display.
    span = np.abs(coords).max() or 1.0
    return (coords / span).astype(np.float32), method


def cluster(emb: np.ndarray, seed: int) -> tuple[np.ndarray, int]:
    n = emb.shape[0]
    k = int(np.clip(round(math.sqrt(n / 3)), 2, 48)) if n >= 8 else 1
    if k <= 1:
        return np.zeros(n, dtype=int), 1
    km = KMeans(k, n_init=3, random_state=seed).fit(emb)
    return km.labels_.astype(int), k


def local_density(knn_sims: np.ndarray, k: int) -> np.ndarray:
    s = np.where(np.isfinite(knn_sims[:, :k]), knn_sims[:, :k], 0.0)
    return s.mean(axis=1)


CONDITIONS = {
    "low_light": ("brightness", lambda v: v < 70, "low light (mean luminance < 70/255)"),
    "bright_scene": ("brightness", lambda v: v > 185, "bright / high-key scenes (mean luminance > 185/255)"),
    "dark_background": ("border_brightness", lambda v: v < 60, "dark backgrounds"),
    "light_background": ("border_brightness", lambda v: v > 200, "light / white backgrounds"),
    "low_saturation": ("saturation", lambda v: v < 35, "muted / desaturated colour"),
    "grayscale": ("is_grayscale", lambda v: bool(v), "grayscale capture"),
    "low_resolution": ("min_side", lambda v: v < 128, "low-resolution captures (< 128 px)"),
}


def analyze(
    *,
    emb: np.ndarray,
    labels: np.ndarray,
    class_names: list[str],
    split: list[str],
    attrs: list[dict],
    knn_sims: np.ndarray,
    cfg: dict,
    seed: int,
) -> dict:
    n = len(labels)
    C = len(class_names)
    coords, method = project_2d(emb, cfg["tsne_max"], seed)
    assign, k = cluster(emb, seed)
    density = local_density(knn_sims, cfg["density_k"])
    split_arr = np.array(split)
    is_train = np.isin(split_arr, ["train", "unsplit"])
    is_eval = np.isin(split_arr, ["val", "test"])
    has_eval = bool(is_eval.any())
    class_counts = np.bincount(labels, minlength=C)
    train_counts = np.bincount(labels[is_train], minlength=C)

    clusters = []
    dens_by_cluster = np.array([density[assign == c].mean() if (assign == c).any() else 0 for c in range(k)])
    dens_cut = np.quantile(dens_by_cluster, cfg["sparse_cluster_quantile"]) if k > 1 else -1
    for c in range(k):
        m = assign == c
        size = int(m.sum())
        comp = Counter(labels[m].tolist())
        sp = Counter(split_arr[m].tolist())
        dom, dom_n = comp.most_common(1)[0] if comp else (-1, 0)
        a_sub = [attrs[i] for i in np.nonzero(m)[0]]
        clusters.append(
            {
                "cluster_index": c,
                "size": size,
                "centroid_2d": [round(float(v), 4) for v in coords[m].mean(axis=0)] if size else [0, 0],
                "class_composition": {class_names[k2]: v for k2, v in comp.items()},
                "split_composition": dict(sp),
                "purity": round(dom_n / max(1, size), 4),
                "density": round(float(dens_by_cluster[c]), 4),
                "is_sparse": bool(
                    dens_by_cluster[c] <= dens_cut or size < max(3, cfg["sparse_min_fraction"] * n)
                ),
                "dominant_class_index": int(dom),
                "attributes": {
                    "mean_brightness": round(float(np.mean([a["brightness"] for a in a_sub])), 2)
                    if a_sub
                    else None,
                    "dominant_border_color": Counter(a["border_color"] for a in a_sub).most_common(1)[0][0]
                    if a_sub
                    else None,
                    "grayscale_fraction": round(float(np.mean([a["is_grayscale"] for a in a_sub])), 3)
                    if a_sub
                    else None,
                },
            }
        )

    gaps: list[dict] = []
    # Under-representation is judged on training data, which is what the model learns from.
    basis = train_counts if (train_counts > 0).sum() >= 2 else class_counts
    med = float(np.median(basis[basis > 0])) if (basis > 0).any() else 0.0

    def members(mask: np.ndarray, limit: int = 6) -> list[int]:
        idx = np.nonzero(mask)[0]
        if len(idx) == 0:
            return []
        order = np.argsort(-density[idx])
        return [int(x) for x in idx[order][:limit]]

    for c in range(C):
        if class_counts[c] == 0:
            continue
        target = max(cfg["min_per_class"], cfg["underrepresented_rel"] * med)
        if basis[c] < target:
            deficit = int(math.ceil(target - basis[c]))
            gaps.append(
                {
                    "kind": "underrepresented_class",
                    "class_index": c,
                    "cluster_index": None,
                    "condition": {},
                    "title": f"Collect more '{class_names[c]}' images",
                    "description": f"'{class_names[c]}' has {int(basis[c])} training images vs a median of {int(med)} per class.",
                    "severity": min(1.0, 0.4 + 0.6 * (1.0 - basis[c] / max(target, 1))),
                    "quantity": _qty(deficit),
                    "anchors": members(labels == c),
                    "evidence": {
                        "training_count": int(basis[c]),
                        "median_training_count": med,
                        "target": round(target, 1),
                    },
                }
            )

    for cl in clusters:
        ci = cl["cluster_index"]
        m = assign == ci
        comp = Counter(labels[m].tolist())
        for c, cnt in comp.items():
            share_of_class = cnt / max(1, class_counts[c])
            if (
                cnt >= 3
                and cnt / cl["size"] >= 0.5
                and share_of_class < cfg["weak_mode_share"]
                and cl["is_sparse"]
            ):
                gaps.append(
                    {
                        "kind": "weak_mode",
                        "class_index": c,
                        "cluster_index": ci,
                        "condition": {},
                        "title": f"Strengthen a sparse visual mode of '{class_names[c]}'",
                        "description": (
                            f"Region {ci} holds only {cnt} '{class_names[c]}' images ({share_of_class:.1%} of the class) "
                            f"and is sparse (density {cl['density']:.3f}). Examples like these are under-covered."
                        ),
                        "severity": 0.5 + 0.5 * (1 - share_of_class / cfg["weak_mode_share"]),
                        "quantity": _qty(max(10, int(0.05 * class_counts[c]) - cnt)),
                        "anchors": members(m & (labels == c)),
                        "evidence": {
                            "cluster_size": cl["size"],
                            "class_count_in_cluster": cnt,
                            "density": cl["density"],
                        },
                    }
                )
        top = comp.most_common(2)
        if (
            len(top) == 2
            and cl["purity"] < cfg["boundary_max_purity"]
            and top[1][1] / cl["size"] >= 0.2
            and cl["size"] >= 6
        ):
            (a, na), (b, nb) = top
            gaps.append(
                {
                    "kind": "boundary_zone",
                    "class_index": a,
                    "cluster_index": ci,
                    "condition": {"other_class_index": b},
                    "title": f"Clarify the boundary between '{class_names[a]}' and '{class_names[b]}'",
                    "description": (
                        f"Region {ci} mixes '{class_names[a]}' ({na}) and '{class_names[b]}' ({nb}). Collect clearly "
                        "labelled, distinguishing examples of both and review ambiguous labels in this region."
                    ),
                    "severity": 0.4 + 0.6 * (1 - cl["purity"]),
                    "quantity": _qty(max(10, cl["size"] // 2)),
                    "anchors": members(m, 8),
                    "evidence": {
                        "purity": cl["purity"],
                        "composition": {class_names[a]: na, class_names[b]: nb},
                    },
                }
            )
        if has_eval:
            n_tr = int((m & is_train).sum())
            n_ev = int((m & is_eval).sum())
            if n_tr >= cfg["blind_spot_min_train"] and n_ev == 0:
                dom = cl["dominant_class_index"]
                gaps.append(
                    {
                        "kind": "evaluation_blind_spot",
                        "class_index": dom,
                        "cluster_index": ci,
                        "condition": {},
                        "title": f"Evaluation does not cover a region of '{class_names[dom]}'",
                        "description": f"Region {ci} has {n_tr} training images but no validation/test images, so performance there is unmeasured.",
                        "severity": 0.45,
                        "quantity": _qty(max(5, n_tr // 5)),
                        "anchors": members(m & is_train),
                        "evidence": {"train": n_tr, "eval": 0},
                    }
                )
            expected_tr = n_ev * (is_train.sum() / max(1, is_eval.sum()))
            if n_ev >= 3 and n_tr < 0.25 * expected_tr:
                dom = cl["dominant_class_index"]
                gaps.append(
                    {
                        "kind": "train_coverage_gap",
                        "class_index": dom,
                        "cluster_index": ci,
                        "condition": {},
                        "title": f"Training data is thin where evaluation images of '{class_names[dom]}' live",
                        "description": f"Region {ci} has {n_ev} evaluation images but only {n_tr} training images (expected ≈{expected_tr:.0f}).",
                        "severity": 0.7,
                        "quantity": _qty(max(10, int(expected_tr - n_tr))),
                        "anchors": members(m & is_eval),
                        "evidence": {
                            "train": n_tr,
                            "eval": n_ev,
                            "expected_train": round(float(expected_tr), 1),
                        },
                    }
                )

    for cond, (key, pred, text) in CONDITIONS.items():
        has = np.array([bool(pred(a[key])) for a in attrs])
        for c in range(C):
            mc = labels == c
            if mc.sum() < 10:
                continue
            others = ~mc
            frac_c = has[mc].mean()
            frac_o = has[others].mean() if others.any() else 0.0
            if (
                frac_o >= cfg["condition_other_min"]
                and frac_c <= cfg["condition_class_max"]
                and has[others].sum() >= 5
            ):
                want = int(math.ceil(frac_o * mc.sum() - has[mc].sum()))
                gaps.append(
                    {
                        "kind": "condition_gap",
                        "class_index": c,
                        "cluster_index": None,
                        "condition": {"condition": cond, "attribute": key, "description": text},
                        "title": f"Collect '{class_names[c]}' images with {text}",
                        "description": (
                            f"{frac_o:.0%} of other classes' images show {text}, but only {frac_c:.0%} of "
                            f"'{class_names[c]}' images do. The model has little evidence for '{class_names[c]}' "
                            "under this condition."
                        ),
                        "severity": min(1.0, 0.4 + (frac_o - frac_c)),
                        "quantity": _qty(max(10, want)),
                        "anchors": members(others & has, 4) + members(mc, 2),
                        "evidence": {
                            "class_fraction": round(float(frac_c), 4),
                            "other_fraction": round(float(frac_o), 4),
                        },
                    }
                )

    total = max(1, n)
    for g in gaps:
        c = g["class_index"]
        rarity = 1.0 - (class_counts[c] / total) if c is not None and c >= 0 else 0.5
        eval_rel = 1.2 if g["kind"] in {"train_coverage_gap", "evaluation_blind_spot"} else 1.0
        g["priority_score"] = round(float(g["severity"] * (0.6 + 0.4 * rarity) * eval_rel), 4)
        g["priority"] = (
            "high" if g["priority_score"] >= 0.65 else "medium" if g["priority_score"] >= 0.4 else "low"
        )
    gaps.sort(key=lambda g: -g["priority_score"])
    return {
        "coords": coords,
        "projection_method": method,
        "assign": assign,
        "density": density,
        "clusters": clusters,
        "gaps": gaps[: cfg["max_gaps"]],
        "k": k,
        "train_counts": train_counts.tolist(),
    }


def _qty(n: int) -> str:
    lo = max(5, int(round(n * 0.8 / 5) * 5))
    hi = max(lo + 5, int(round(n * 1.3 / 5) * 5))
    return f"{lo}–{hi}"
