"""Shortcut / spurious-correlation detective (`shortcut-detective-v1`).

Measurable proxies only — association is not causation:
  1. Non-content cues are derived per image (border/background colour and brightness,
     grayscale, frame, overlay proxy, resolution, aspect, file format, camera model).
  2. For each cue: bias-corrected Cramér's V against the class label, per-cell lift and
     support, and whether the same association holds in the evaluation split.
  3. Cue-only predictability: a classifier trained on *only* these cues, cross-validated.
     If labels are predictable from background/format alone, a model can take the shortcut.
  4. (Deep profile) Perturbation tests on evaluation images: accuracy of the baseline model
     on border-only images (content masked) and on content-only images (border masked).
"""

from __future__ import annotations

from collections import Counter

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import balanced_accuracy_score
from sklearn.model_selection import StratifiedKFold, cross_val_predict

from datacourt import algorithms

VERSION = algorithms.SHORTCUTS

CUE_LABELS = {
    "border_color": "Background / border colour",
    "border_brightness": "Background brightness",
    "grayscale": "Grayscale vs colour",
    "frame": "Solid image frame",
    "overlay": "Corner overlay / watermark proxy",
    "resolution": "Resolution bucket",
    "aspect": "Aspect ratio",
    "format": "File format",
    "camera": "Camera model (EXIF)",
}


def derive_cues(attrs: list[dict], meta: list[dict]) -> dict[str, list[str]]:
    def res_bucket(ms: int) -> str:
        return "<128px" if ms < 128 else "128-255px" if ms < 256 else "256-511px" if ms < 512 else ">=512px"

    def aspect(a: float) -> str:
        return "portrait" if a < 0.9 else "landscape" if a > 1.1 else "square"

    def bright(b: float) -> str:
        return "dark" if b < 70 else "bright" if b > 185 else "mid"

    cues = {
        "border_color": [a["border_color"] for a in attrs],
        "border_brightness": [bright(float(a["border_brightness"])) for a in attrs],
        "grayscale": ["grayscale" if a["is_grayscale"] else "colour" for a in attrs],
        "frame": ["framed" if a["has_frame"] else "no frame" for a in attrs],
        "overlay": [
            "overlay-like corner" if float(a["corner_overlay_ratio"]) > 4.0 else "none" for a in attrs
        ],
        "resolution": [res_bucket(int(a["min_side"])) for a in attrs],
        "aspect": [aspect(float(a["aspect_ratio"])) for a in attrs],
        "format": [m.get("format", "?") for m in meta],
        "camera": [((m.get("attributes") or {}).get("camera_model") or "unknown") for m in meta],
    }
    # Drop cues with a single value (no information) or with no known values.
    return {
        k: v for k, v in cues.items() if len(set(v)) > 1 and not (k == "camera" and set(v) == {"unknown"})
    }


def cramers_v(x: list[str], y: list[int]) -> float:
    xs = sorted(set(x))
    ys = sorted(set(y))
    if len(xs) < 2 or len(ys) < 2:
        return 0.0
    xi = {v: i for i, v in enumerate(xs)}
    yi = {v: i for i, v in enumerate(ys)}
    table = np.zeros((len(xs), len(ys)))
    for a, b in zip(x, y, strict=True):
        table[xi[a], yi[b]] += 1
    n = table.sum()
    expected = table.sum(1, keepdims=True) @ table.sum(0, keepdims=True) / n
    chi2 = ((table - expected) ** 2 / np.maximum(expected, 1e-12)).sum()
    phi2 = chi2 / n
    r, k = table.shape
    phi2c = max(0.0, phi2 - (k - 1) * (r - 1) / max(1, n - 1))
    rc = r - (r - 1) ** 2 / max(1, n - 1)
    kc = k - (k - 1) ** 2 / max(1, n - 1)
    denom = min(kc - 1, rc - 1)
    return float(np.sqrt(phi2c / denom)) if denom > 0 else 0.0


def _onehot(cues: dict[str, list[str]], keys: list[str]) -> np.ndarray:
    cols = []
    for k in keys:
        vals = sorted(set(cues[k]))
        idx = {v: i for i, v in enumerate(vals)}
        m = np.zeros((len(cues[k]), len(vals)))
        for r, v in enumerate(cues[k]):
            m[r, idx[v]] = 1
        cols.append(m)
    return np.hstack(cols) if cols else np.zeros((0, 0))


def cue_predictability(cues: dict[str, list[str]], y: np.ndarray, keys: list[str], seed: int) -> float | None:
    counts = np.bincount(y)
    present = counts[counts > 0]
    if len(present) < 2 or present.min() < 3 or not keys:
        return None
    X = _onehot(cues, keys)
    k = int(min(5, present.min()))
    clf = LogisticRegression(max_iter=500, class_weight="balanced")
    pred = cross_val_predict(clf, X, y, cv=StratifiedKFold(k, shuffle=True, random_state=seed))
    return float(balanced_accuracy_score(y, pred))


def strength_label(v: float, cfg: dict) -> str:
    if v >= cfg["strong_cramers_v"]:
        return "strong"
    if v >= cfg["moderate_cramers_v"]:
        return "moderate"
    return "weak"


def analyze(
    cues: dict[str, list[str]],
    labels: np.ndarray,
    class_names: list[str],
    fit_mask: np.ndarray,
    eval_mask: np.ndarray,
    cfg: dict,
    seed: int,
) -> tuple[list[dict], dict]:
    y = labels[fit_mask]
    findings: list[dict] = []
    per_cue: dict[str, dict] = {}
    base_rate = np.bincount(y, minlength=len(class_names)) / max(1, len(y))
    for key, values in cues.items():
        vals = [v for v, m in zip(values, fit_mask, strict=True) if m]
        v_fit = cramers_v(vals, y.tolist())
        v_eval = None
        if eval_mask.sum() >= 20:
            ev = [v for v, m in zip(values, eval_mask, strict=True) if m]
            v_eval = cramers_v(ev, labels[eval_mask].tolist())
        single = (
            cue_predictability({key: vals}, y, [key], seed) if v_fit >= cfg["moderate_cramers_v"] else None
        )
        per_cue[key] = {
            "cramers_v": round(v_fit, 4),
            "cramers_v_eval": None if v_eval is None else round(v_eval, 4),
            "cue_only_balanced_accuracy": None if single is None else round(single, 4),
        }
        if v_fit < cfg["moderate_cramers_v"]:
            continue
        # Strongest (value, class) cells.
        cnt = Counter(zip(vals, y.tolist(), strict=True))
        val_tot = Counter(vals)
        cls_tot = Counter(y.tolist())
        cells = []
        for (val, c), n_vc in cnt.items():
            if n_vc < cfg["min_support"]:
                continue
            p_c_given_v = n_vc / val_tot[val]
            lift = p_c_given_v / max(base_rate[c], 1e-9)
            share_of_class = n_vc / cls_tot[c]
            if lift >= cfg["min_lift"] and share_of_class >= cfg["min_class_share"]:
                cells.append((lift * share_of_class, val, c, n_vc, p_c_given_v, lift, share_of_class))
        cells.sort(reverse=True)
        for _, val, c, n_vc, p_cv, lift, share in cells[:3]:
            affected = [
                int(i)
                for i, (v, m) in enumerate(zip(values, fit_mask, strict=True))
                if m and v == val and labels[i] == c
            ]
            chance = 1.0 / max(1, (base_rate > 0).sum())
            eval_note = ""
            if v_eval is not None:
                eval_note = (
                    " The same association appears in the evaluation split, so evaluation may not reveal the shortcut."
                    if v_eval >= cfg["moderate_cramers_v"]
                    else " The association is much weaker in the evaluation split, so a model relying on it may lose accuracy there."
                )
            findings.append(
                {
                    "cue": key,
                    "cue_label": CUE_LABELS.get(key, key),
                    "cue_value": val,
                    "class_index": int(c),
                    "strength": round(v_fit, 4),
                    "strength_label": strength_label(v_fit, cfg),
                    "association": {
                        "cramers_v": round(v_fit, 4),
                        "cramers_v_eval": per_cue[key]["cramers_v_eval"],
                        "lift": round(float(lift), 3),
                        "p_class_given_cue": round(float(p_cv), 4),
                        "share_of_class_with_cue": round(float(share), 4),
                        "support": int(n_vc),
                        "cue_only_balanced_accuracy": per_cue[key]["cue_only_balanced_accuracy"],
                        "chance_balanced_accuracy": round(chance, 4),
                    },
                    "affected": affected,
                    "consequence": (
                        f"{share:.0%} of '{class_names[c]}' training images have {CUE_LABELS.get(key, key).lower()} = '{val}' "
                        f"(lift {lift:.1f}× over the base rate). A model can reach good training accuracy by keying on this cue "
                        f"instead of the object itself.{eval_note}"
                    ),
                    "recommendation": (
                        f"Test the model on '{class_names[c]}' images without '{val}' {CUE_LABELS.get(key, key).lower()}, "
                        f"and collect or augment '{class_names[c]}' examples with varied {CUE_LABELS.get(key, key).lower()}."
                    ),
                }
            )
    all_keys = [k for k in cues if per_cue.get(k, {}).get("cramers_v", 0) > 0]
    overall = cue_predictability(
        {k: [v for v, m in zip(cues[k], fit_mask, strict=True) if m] for k in all_keys}, y, all_keys, seed
    )
    summary = {
        "per_cue": per_cue,
        "all_cues_balanced_accuracy": None if overall is None else round(overall, 4),
        "chance_balanced_accuracy": round(1.0 / max(1, (base_rate > 0).sum()), 4),
    }
    findings.sort(key=lambda f: -f["strength"])
    return findings, summary


def border_only(rgb: np.ndarray, frac: float = 0.2) -> np.ndarray:
    """Keep the outer band, replace the centre with the image mean colour."""
    out = rgb.copy()
    h, w = rgb.shape[:2]
    by, bx = int(h * frac), int(w * frac)
    out[by : h - by, bx : w - bx] = rgb.reshape(-1, 3).mean(axis=0).astype(np.uint8)
    return out


def content_only(rgb: np.ndarray, frac: float = 0.2) -> np.ndarray:
    """Replace the outer band with neutral gray, keeping the centre."""
    out = np.full_like(rgb, 127)
    h, w = rgb.shape[:2]
    by, bx = int(h * frac), int(w * frac)
    out[by : h - by, bx : w - bx] = rgb[by : h - by, bx : w - bx]
    return out
