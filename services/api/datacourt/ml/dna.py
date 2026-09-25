"""Dataset DNA (`dna-v1`) — a versioned semantic/statistical fingerprint — and drift.

DNA is a *descriptive profile*, not a cryptographic identity: identical DNA does not
prove identical files (use the manifest SHA-256 for that). It is designed for comparing
versions: class mix, capture conditions, resolution/format mix, embedding geometry
(same backend only) and the issue profile.
"""

from __future__ import annotations

import hashlib
from collections import Counter

import numpy as np

from datacourt import algorithms

VERSION = algorithms.DNA

BRIGHT_EDGES = [0, 40, 70, 100, 130, 160, 190, 220, 256]
SAT_EDGES = [0, 25, 50, 80, 110, 140, 180, 256]
RES_ORDER = ["<32", "32-63", "64-127", "128-255", "256-511", "512-1023", ">=1024"]


def _hist(values: list[float], edges: list[float]) -> list[float]:
    h, _ = np.histogram(np.asarray(values, dtype=float), bins=edges)
    return (h / max(1, h.sum())).round(5).tolist()


def _dist(counter: Counter, keys: list[str] | None = None) -> dict[str, float]:
    tot = sum(counter.values()) or 1
    ks = keys or sorted(counter)
    return {k: round(counter.get(k, 0) / tot, 5) for k in ks}


def _res_bucket(side: int) -> str:
    for limit, label in (
        (32, "<32"),
        (64, "32-63"),
        (128, "64-127"),
        (256, "128-255"),
        (512, "256-511"),
        (1024, "512-1023"),
    ):
        if side < limit:
            return label
    return ">=1024"


def build_profile(
    *,
    labels: np.ndarray,
    class_names: list[str],
    split: list[str],
    attrs: list[dict],
    meta: list[dict],
    emb: np.ndarray,
    backend: str,
    issue_profile: dict,
) -> dict:
    per_class: dict[str, dict] = {}
    for c, name in enumerate(class_names):
        m = labels == c
        if not m.any():
            continue
        idx = np.nonzero(m)[0]
        v = emb[m].mean(axis=0)
        cent = v / (np.linalg.norm(v) + 1e-12)
        per_class[name] = {
            "count": int(m.sum()),
            "brightness_hist": _hist([attrs[i]["brightness"] for i in idx], BRIGHT_EDGES),
            "border_color": _dist(Counter(attrs[i]["border_color"] for i in idx)),
            "grayscale_fraction": round(float(np.mean([attrs[i]["is_grayscale"] for i in idx])), 4),
            "dispersion": round(float(1 - (emb[m] @ cent).mean()), 5),
            "centroid": [round(float(x), 5) for x in cent],
        }
    v = emb.mean(axis=0)
    g = v / (np.linalg.norm(v) + 1e-12)
    profile = {
        "dna_version": VERSION,
        "embedding_backend": backend,
        "sample_count": int(len(labels)),
        "class_distribution": _dist(Counter(class_names[i] for i in labels), class_names),
        "split_distribution": _dist(Counter(split)),
        "resolution_distribution": _dist(Counter(_res_bucket(int(a["min_side"])) for a in attrs), RES_ORDER),
        "format_distribution": _dist(Counter(mm.get("format", "?") for mm in meta)),
        "brightness_hist": _hist([a["brightness"] for a in attrs], BRIGHT_EDGES),
        "saturation_hist": _hist([a["saturation"] for a in attrs], SAT_EDGES),
        "border_color_distribution": _dist(Counter(a["border_color"] for a in attrs)),
        "grayscale_fraction": round(float(np.mean([a["is_grayscale"] for a in attrs])), 4) if attrs else 0.0,
        "embedding": {
            "global_centroid": [round(float(x), 5) for x in g],
            "dispersion": round(float(1 - (emb @ g).mean()), 5),
        },
        "per_class": per_class,
        "issue_profile": issue_profile,
    }
    profile["strip"] = dna_strip(profile)
    return profile


def dna_strip(p: dict) -> list[float]:
    """24 bands in [0, 1] summarizing the profile, for a compact visual signature."""
    bands: list[float] = []
    cd = list(p["class_distribution"].values())
    bands += (cd + [0.0] * 6)[:6]
    bands += p["brightness_hist"][:6]
    bands += (list(p["resolution_distribution"].values()) + [0.0] * 4)[:4]
    bands += p["saturation_hist"][:4]
    ip = p["issue_profile"]
    bands += [
        min(1.0, ip.get("duplicate_rate", 0) * 5),
        min(1.0, ip.get("label_review_rate", 0) * 10),
        min(1.0, ip.get("quality_rate", 0) * 5),
        p["grayscale_fraction"],
    ]
    mx = max(bands) or 1.0
    return [round(b / mx, 4) for b in bands]


def fingerprint(p: dict) -> str:
    coarse = {
        "c": {k: round(v, 2) for k, v in p["class_distribution"].items()},
        "s": {k: round(v, 2) for k, v in p["split_distribution"].items()},
        "b": [round(x, 1) for x in p["brightness_hist"]],
        "r": {k: round(v, 1) for k, v in p["resolution_distribution"].items()},
        "e": p["embedding_backend"],
    }
    return "DNA-" + hashlib.sha256(repr(sorted(coarse.items())).encode()).hexdigest()[:12].upper()


def _js(p: list[float] | dict, q: list[float] | dict) -> float:
    if isinstance(p, dict) or isinstance(q, dict):
        keys = sorted(set(p) | set(q))  # type: ignore[arg-type]
        pa = np.array([p.get(k, 0.0) for k in keys])  # type: ignore[union-attr]
        qa = np.array([q.get(k, 0.0) for k in keys])  # type: ignore[union-attr]
    else:
        pa, qa = np.asarray(p, float), np.asarray(q, float)
    pa = pa / max(pa.sum(), 1e-12)
    qa = qa / max(qa.sum(), 1e-12)
    mm = 0.5 * (pa + qa)

    def kl(a: np.ndarray, b: np.ndarray) -> float:
        mask = a > 0
        return float((a[mask] * np.log2(a[mask] / np.maximum(b[mask], 1e-12))).sum())

    return round(0.5 * kl(pa, mm) + 0.5 * kl(qa, mm), 5)


def compare(
    a: dict, b: dict, *, label_a: str = "previous", label_b: str = "current", threshold: float = 0.05
) -> dict:
    """Drift report between two DNA profiles (a = older, b = newer)."""
    signals: list[dict] = []

    def add(dimension: str, value: float, message: str, scope: str = "dataset") -> None:
        sev = "strong" if value >= threshold * 3 else "moderate" if value >= threshold else "none"
        signals.append(
            {"dimension": dimension, "scope": scope, "value": value, "severity": sev, "message": message}
        )

    add(
        "class_distribution",
        _js(a["class_distribution"], b["class_distribution"]),
        "Class proportions changed",
    )
    add(
        "brightness",
        _js(a["brightness_hist"], b["brightness_hist"]),
        "Lighting (brightness) distribution changed",
    )
    add(
        "saturation",
        _js(a["saturation_hist"], b["saturation_hist"]),
        "Colour saturation distribution changed",
    )
    add(
        "resolution",
        _js(a["resolution_distribution"], b["resolution_distribution"]),
        "Resolution mix changed",
    )
    add(
        "background",
        _js(a["border_color_distribution"], b["border_color_distribution"]),
        "Background colour mix changed",
    )
    add("format", _js(a["format_distribution"], b["format_distribution"]), "File format mix changed")
    same_backend = a.get("embedding_backend") == b.get("embedding_backend")
    if same_backend:
        ga, gb = np.array(a["embedding"]["global_centroid"]), np.array(b["embedding"]["global_centroid"])
        shift = round(float(1 - ga @ gb), 5)
        add("embedding_centroid", shift * 10, "Overall visual appearance (embedding centroid) shifted")
    for cls in sorted(set(a["per_class"]) & set(b["per_class"])):
        pa, pb = a["per_class"][cls], b["per_class"][cls]
        add(
            "class_brightness",
            _js(pa["brightness_hist"], pb["brightness_hist"]),
            f"Lighting changed within class '{cls}'",
            cls,
        )
        add(
            "class_background",
            _js(pa["border_color"], pb["border_color"]),
            f"Backgrounds changed within class '{cls}'",
            cls,
        )
        if same_backend:
            ca, cb = np.array(pa["centroid"]), np.array(pb["centroid"])
            add(
                "class_embedding",
                round(float(1 - ca @ cb), 5) * 10,
                f"Visual appearance of class '{cls}' shifted",
                cls,
            )
    added = sorted(set(b["per_class"]) - set(a["per_class"]))
    removed = sorted(set(a["per_class"]) - set(b["per_class"]))
    material = [s for s in signals if s["severity"] != "none"]
    headline = []
    for s in sorted(material, key=lambda x: -x["value"])[:3]:
        where = f" in class '{s['scope']}'" if s["scope"] != "dataset" else ""
        headline.append(f"{label_b} differs {s['severity']}ly from {label_a}{where}: {s['message'].lower()}.")
    return {
        "dna_version": VERSION,
        "same_embedding_backend": same_backend,
        "signals": sorted(signals, key=lambda x: -x["value"]),
        "classes_added": added,
        "classes_removed": removed,
        "materially_different": bool(material or added or removed),
        "headline": headline,
        "threshold": threshold,
        "method": "Jensen–Shannon divergence (bits) for distributions; 10 × (1 − cosine) for embedding centroids.",
    }
