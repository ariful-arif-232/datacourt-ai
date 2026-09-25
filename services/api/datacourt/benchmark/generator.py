"""Controlled benchmark generator ("DataCourt Synthetic Shapes").

Procedurally renders an image-classification dataset and injects issues with a
ground-truth record, so every detector can be measured with precision/recall rather
than anecdotes. Everything is seeded and reproducible.

Injected issues
  label_errors         train/test images saved under a wrong class folder
  exact_duplicates     byte-identical copies inside train
  near_duplicates      resized / cropped / flipped / brightness variants inside train
  cross_split_exact    test images that are byte-identical to train images
  cross_split_variant  val images that are transformed copies of train images
  quality              heavy blur, tiny resolution, severe underexposure
  imbalance            one class has far fewer images
  rare_valid           a valid but visually unusual sub-type of one class
  shortcut             one class mostly photographed on a blue background (train only)
  condition_gap        one class never appears in low light while others often do
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass, field

import cv2
import numpy as np
from PIL import Image

CLASSES = ["cross", "disc", "square", "star", "triangle"]
PALETTE_BG = [
    (214, 196, 160),
    (160, 190, 150),
    (190, 170, 200),
    (200, 200, 200),
    (170, 150, 120),
    (150, 170, 190),
    (220, 210, 180),
    (120, 140, 110),
]
BLUE_BG = (70, 110, 215)


@dataclass
class GroundTruth:
    label_errors: dict[str, dict] = field(default_factory=dict)  # path -> {"true": cls, "given": cls}
    exact_duplicates: list[list[str]] = field(default_factory=list)  # [original, copy]
    near_duplicates: list[dict] = field(default_factory=list)  # {original, copy, relation}
    cross_split_exact: list[list[str]] = field(default_factory=list)
    cross_split_variant: list[dict] = field(default_factory=list)
    quality: dict[str, str] = field(default_factory=dict)  # path -> kind
    rare_valid: list[str] = field(default_factory=list)
    shortcut: dict = field(default_factory=dict)
    condition_gap: dict = field(default_factory=dict)
    imbalance: dict = field(default_factory=dict)
    counts: dict = field(default_factory=dict)


def _star(cx: float, cy: float, r: float, rot: float) -> np.ndarray:
    pts = []
    for k in range(10):
        ang = rot + k * np.pi / 5
        rr = r if k % 2 == 0 else r * 0.45
        pts.append((cx + rr * np.cos(ang), cy + rr * np.sin(ang)))
    return np.array(pts, dtype=np.int32)


def _poly(cx: float, cy: float, r: float, rot: float, n: int) -> np.ndarray:
    return np.array(
        [
            (cx + r * np.cos(rot + 2 * np.pi * k / n), cy + r * np.sin(rot + 2 * np.pi * k / n))
            for k in range(n)
        ],
        dtype=np.int32,
    )


def render(
    cls: str,
    rng: np.random.Generator,
    *,
    size: int = 160,
    bg: tuple | None = None,
    low_light: bool = False,
    rare: bool = False,
) -> np.ndarray:
    bgc = np.array(bg if bg is not None else PALETTE_BG[rng.integers(len(PALETTE_BG))], dtype=np.float32)
    bgc = np.clip(bgc + rng.normal(0, 12, 3), 0, 255)
    img = np.ones((size, size, 3), np.float32) * bgc
    # background texture
    noise = cv2.GaussianBlur(rng.normal(0, 18, (size, size)).astype(np.float32), (0, 0), 3)
    img += noise[..., None]
    for _ in range(int(rng.integers(0, 3))):
        c = tuple(float(x) for x in np.clip(bgc + rng.normal(0, 12, 3), 0, 255))
        cv2.circle(
            img, (int(rng.integers(0, size)), int(rng.integers(0, size))), int(rng.integers(3, 10)), c, -1
        )
    fg = np.array([rng.integers(10, 90), rng.integers(30, 130), rng.integers(10, 110)], dtype=np.float32)
    if rng.random() < 0.5:
        fg = fg[::-1].copy()
    color = tuple(float(x) for x in fg)
    cx, cy = size / 2 + rng.normal(0, size * 0.07), size / 2 + rng.normal(0, size * 0.07)
    r = size * rng.uniform(0.22, 0.34)
    rot = rng.uniform(0, 2 * np.pi)
    if cls == "disc":
        if rare:
            # rare valid sub-type: a thick ring with a contrasting core dot
            cv2.circle(img, (int(cx), int(cy)), int(r), color, max(3, int(r * 0.25)))
            cv2.circle(img, (int(cx), int(cy)), max(2, int(r * 0.18)), (240, 240, 240), -1)
        else:
            cv2.circle(img, (int(cx), int(cy)), int(r), color, -1)
    elif cls == "square":
        cv2.fillPoly(img, [_poly(cx, cy, r * 1.15, rot, 4)], color)
    elif cls == "triangle":
        cv2.fillPoly(img, [_poly(cx, cy, r * 1.2, rot, 3)], color)
    elif cls == "star":
        cv2.fillPoly(img, [_star(cx, cy, r * 1.25, rot)], color)
    elif cls == "cross":
        w = r * 0.35
        m1 = cv2.getRotationMatrix2D((cx, cy), np.degrees(rot), 1.0)
        for rect in (((cx - r, cy - w), (cx + r, cy + w)), ((cx - w, cy - r), (cx + w, cy + r))):
            (x0, y0), (x1, y1) = rect
            pts = np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype=np.float32)
            pts = cv2.transform(pts[None], m1)[0].astype(np.int32)
            cv2.fillPoly(img, [pts], color)
    img += rng.normal(0, 4, img.shape)
    if low_light:
        img *= rng.uniform(0.18, 0.3)
    return np.clip(img, 0, 255).astype(np.uint8)


def encode(rgb: np.ndarray, fmt: str = "JPEG", quality: int = 90) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, format=fmt, quality=quality)
    return buf.getvalue()


def generate(
    seed: int = 7,
    per_class_train: int = 170,
    per_class_eval: int = 30,
    minority_train: int = 55,
    label_error_rate: float = 0.05,
) -> tuple[bytes, GroundTruth]:
    rng = np.random.default_rng(seed)
    files: dict[str, bytes] = {}
    images: dict[str, np.ndarray] = {}
    gt = GroundTruth()
    root = "synthetic-shapes"

    def add(path: str, rgb: np.ndarray, fmt: str = "JPEG") -> None:
        files[path] = encode(rgb, fmt)
        images[path] = rgb

    counter = {"n": 0}

    def name(split: str, cls: str) -> str:
        counter["n"] += 1
        return f"{root}/{split}/{cls}/img_{counter['n']:05d}.jpg"

    # ---- clean renders ------------------------------------------------------
    for split, n_per in (("train", per_class_train), ("val", per_class_eval), ("test", per_class_eval)):
        for cls in CLASSES:
            n = minority_train if (cls == "cross" and split == "train") else n_per
            for k in range(n):
                size = int(rng.choice([128, 160, 192, 224]))
                bg = None
                low = False
                if (
                    cls == "star"
                    and split == "train"
                    and rng.random() < 0.85
                    or cls != "star"
                    and rng.random() < 0.03
                ):
                    bg = BLUE_BG
                if cls != "triangle" and rng.random() < 0.2:
                    low = True
                rare = cls == "disc" and split == "train" and k < 8
                rgb = render(cls, rng, size=size, bg=bg, low_light=low, rare=rare)
                p = name(split, cls)
                add(p, rgb)
                if rare:
                    gt.rare_valid.append(p)
    gt.shortcut = {"class": "star", "cue": "border_color", "value": "blue", "train_fraction": 0.85}
    gt.condition_gap = {"class": "triangle", "condition": "low_light"}
    gt.imbalance = {"class": "cross", "train_count": minority_train, "others": per_class_train}

    def paths(split: str, cls: str | None = None) -> list[str]:
        return [
            p
            for p in files
            if p.split("/")[1] == split and (cls is None or p.split("/")[2] == cls) and p not in gt.rare_valid
        ]

    # ---- label errors (train + test) -----------------------------------------
    moved: dict[str, str] = {}
    for split, rate in (("train", label_error_rate), ("test", label_error_rate * 0.6)):
        pool = paths(split)
        chosen = rng.choice(len(pool), size=int(len(pool) * rate), replace=False)
        for ci in chosen:
            p = pool[int(ci)]
            true = p.split("/")[2]
            wrong = str(rng.choice([c for c in CLASSES if c != true]))
            newp = p.replace(f"/{split}/{true}/", f"/{split}/{wrong}/")
            files[newp] = files.pop(p)
            images[newp] = images.pop(p)
            moved[p] = newp
            gt.label_errors[newp] = {"true": true, "given": wrong, "split": split}

    clean_train = [p for p in paths("train") if p not in gt.label_errors]

    def pick(k: int) -> list[str]:
        idx = rng.choice(len(clean_train), size=k, replace=False)
        out = [clean_train[int(i)] for i in idx]
        for o in out:
            clean_train.remove(o)
        return out

    # ---- exact duplicates inside train ------------------------------------------
    for src in pick(20):
        cls = src.split("/")[2]
        dst = f"{root}/train/{cls}/copy_{counter['n']:05d}.jpg"
        counter["n"] += 1
        files[dst] = files[src]
        images[dst] = images[src]
        gt.exact_duplicates.append([src, dst])

    # ---- near duplicates inside train ------------------------------------------
    def variant(rgb: np.ndarray, kind: str) -> np.ndarray:
        h, w = rgb.shape[:2]
        if kind == "resized":
            f = float(rng.choice([0.6, 0.75, 1.4]))
            return cv2.resize(rgb, (int(w * f), int(h * f)), interpolation=cv2.INTER_AREA)
        if kind == "cropped":
            m = int(min(h, w) * 0.1)
            return rgb[m : h - m // 2, m // 2 : w - m].copy()
        if kind == "flipped":
            return rgb[:, ::-1].copy()
        if kind == "photometric":
            return np.clip(rgb.astype(np.float32) * 1.35 + 18, 0, 255).astype(np.uint8)
        raise ValueError(kind)

    for kind, k in (("resized", 12), ("cropped", 10), ("flipped", 10), ("photometric", 10)):
        for src in pick(k):
            cls = src.split("/")[2]
            dst = f"{root}/train/{cls}/var_{kind}_{counter['n']:05d}.jpg"
            counter["n"] += 1
            add(dst, variant(images[src], kind))
            gt.near_duplicates.append({"original": src, "copy": dst, "relation": kind})

    # ---- cross-split leakage ---------------------------------------------------
    for src in pick(12):
        cls = src.split("/")[2]
        dst = f"{root}/test/{cls}/leak_{counter['n']:05d}.jpg"
        counter["n"] += 1
        files[dst] = files[src]
        images[dst] = images[src]
        gt.cross_split_exact.append([src, dst])
    for kind, k in (("resized", 4), ("flipped", 3), ("photometric", 3)):
        for src in pick(k):
            cls = src.split("/")[2]
            dst = f"{root}/val/{cls}/leakvar_{kind}_{counter['n']:05d}.jpg"
            counter["n"] += 1
            add(dst, variant(images[src], kind))
            gt.cross_split_variant.append({"original": src, "copy": dst, "relation": kind})

    # ---- quality problems (train) ------------------------------------------------
    for src in pick(12):
        files[src] = encode(cv2.GaussianBlur(images[src], (0, 0), 6))
        gt.quality[src] = "blur"
    for src in pick(8):
        small = cv2.resize(images[src], (22, 22), interpolation=cv2.INTER_AREA)
        files[src] = encode(small)
        gt.quality[src] = "low_resolution"
    for src in pick(8):
        files[src] = encode((images[src].astype(np.float32) * 0.07).astype(np.uint8))
        gt.quality[src] = "underexposed"

    # ---- a corrupt file and a non-image file (ingestion robustness) ---------------
    files[f"{root}/train/square/corrupt_00001.jpg"] = b"\xff\xd8\xff\xe0 not really a jpeg"
    files[f"{root}/README.txt"] = b"DataCourt synthetic benchmark. See ground_truth.json."

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(files):
            zf.writestr(p, files[p])
    gt.counts = {
        "files": len(files),
        "label_errors": len(gt.label_errors),
        "exact_duplicates": len(gt.exact_duplicates),
        "near_duplicates": len(gt.near_duplicates),
        "cross_split_exact": len(gt.cross_split_exact),
        "cross_split_variant": len(gt.cross_split_variant),
        "quality": len(gt.quality),
        "rare_valid": len(gt.rare_valid),
    }
    return buf.getvalue(), gt


def ground_truth_json(gt: GroundTruth) -> str:
    return json.dumps(gt.__dict__, indent=2, sort_keys=True)


if __name__ == "__main__":  # pragma: no cover
    import sys

    data, truth = generate()
    out = sys.argv[1] if len(sys.argv) > 1 else "synthetic-shapes.zip"
    with open(out, "wb") as fh:
        fh.write(data)
    with open(out.replace(".zip", ".ground_truth.json"), "w") as fh:
        fh.write(ground_truth_json(truth))
    print(json.dumps(truth.counts))
