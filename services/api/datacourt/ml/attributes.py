"""Per-image measurable attributes (algorithm `attributes-v1`).

All values are computed on a downscaled RGB copy (short side normalized to 256 px where
applicable) so metrics are comparable across resolutions. These are *measurements*;
the quality rules and shortcut detective interpret them.
"""

from __future__ import annotations

import cv2
import numpy as np

from datacourt import algorithms

VERSION = algorithms.ATTRIBUTES
_NORM_SIDE = 256
_BORDER_FRAC = 0.08

HUE_NAMES = ["red", "orange", "yellow", "green", "cyan", "blue", "purple", "magenta"]


def _normalize_scale(rgb: np.ndarray) -> np.ndarray:
    h, w = rgb.shape[:2]
    short = min(h, w)
    if short == _NORM_SIDE:
        return rgb
    scale = _NORM_SIDE / short
    new = (max(1, round(w * scale)), max(1, round(h * scale)))
    interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
    return cv2.resize(rgb, new, interpolation=interp)


def color_name(rgb_mean: np.ndarray) -> str:
    """Coarse perceptual color name for a mean RGB color."""
    px = np.uint8([[np.clip(rgb_mean, 0, 255)]])
    h, s, v = cv2.cvtColor(px, cv2.COLOR_RGB2HSV)[0, 0].astype(float)
    if v < 50:
        return "black"
    if s < 40:
        return "white" if v > 200 else "gray"
    hue_deg = h * 2.0
    bins = [15, 40, 70, 160, 195, 260, 290, 330]
    for i, edge in enumerate(bins):
        if hue_deg < edge:
            return HUE_NAMES[i]
    return "red"


def compute_attributes(rgb: np.ndarray, width: int, height: int) -> dict[str, float | str | bool]:
    img = _normalize_scale(rgb)
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    g = gray.astype(np.float32)
    h, w = gray.shape

    brightness = float(g.mean())
    contrast = float(g.std())
    blur = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    hist = np.bincount(gray.ravel(), minlength=256).astype(np.float64)
    p = hist / hist.sum()
    nz = p[p > 0]
    entropy = float(-(nz * np.log2(nz)).sum())
    hsv = cv2.cvtColor(img, cv2.COLOR_RGB2HSV)
    saturation = float(hsv[..., 1].mean())

    f = img.astype(np.float32)
    rg = f[..., 0] - f[..., 1]
    yb = 0.5 * (f[..., 0] + f[..., 1]) - f[..., 2]
    colorfulness = float(
        np.sqrt(rg.std() ** 2 + yb.std() ** 2) + 0.3 * np.sqrt(rg.mean() ** 2 + yb.mean() ** 2)
    )
    chan_diff = float(np.abs(f[..., 0] - f[..., 1]).mean() + np.abs(f[..., 1] - f[..., 2]).mean())
    is_gray = chan_diff < 3.0

    edges = cv2.Canny(gray, 80, 160)
    edge_density = float((edges > 0).mean())

    bh, bw = max(1, int(h * _BORDER_FRAC)), max(1, int(w * _BORDER_FRAC))
    border_mask = np.zeros((h, w), dtype=bool)
    border_mask[:bh, :] = True
    border_mask[-bh:, :] = True
    border_mask[:, :bw] = True
    border_mask[:, -bw:] = True
    border_px = f[border_mask]
    center_px = f[~border_mask] if (~border_mask).any() else f.reshape(-1, 3)
    border_mean = border_px.mean(axis=0)
    border_lum = float(0.299 * border_mean[0] + 0.587 * border_mean[1] + 0.114 * border_mean[2])
    border_std = float(g[border_mask].std())
    center_lum = float(center_px.mean(axis=0) @ np.array([0.299, 0.587, 0.114]))

    # Solid frame: a thin outer band that is nearly constant and differs from inside.
    thin = max(1, int(min(h, w) * 0.03))
    band = np.concatenate([g[:thin].ravel(), g[-thin:].ravel(), g[:, :thin].ravel(), g[:, -thin:].ravel()])
    inner = g[thin * 2 : h - thin * 2, thin * 2 : w - thin * 2]
    has_frame = bool(
        band.std() < 4.0 and inner.size > 0 and abs(float(band.mean()) - float(inner.mean())) > 20
    )

    # Watermark / overlay proxy: edge energy concentrated in a corner relative to the image.
    ch, cw = max(1, h // 5), max(1, w // 5)
    corners = [edges[:ch, :cw], edges[:ch, -cw:], edges[-ch:, :cw], edges[-ch:, -cw:]]
    corner_energy = max(float((c > 0).mean()) for c in corners)
    overlay_ratio = corner_energy / (edge_density + 1e-3)

    dark_frac = float((gray < 10).mean())
    bright_frac = float((gray > 245).mean())
    aspect = width / max(1, height)

    return {
        "brightness": round(brightness, 3),
        "contrast": round(contrast, 3),
        "blur_laplacian_var": round(blur, 3),
        "sharpness_norm": round(blur / (contrast**2 + 1.0), 5),
        "entropy_bits": round(entropy, 4),
        "saturation": round(saturation, 3),
        "colorfulness": round(colorfulness, 3),
        "is_grayscale": is_gray,
        "edge_density": round(edge_density, 5),
        "border_brightness": round(border_lum, 3),
        "border_uniformity_std": round(border_std, 3),
        "border_color": color_name(border_mean),
        "center_brightness": round(center_lum, 3),
        "has_frame": has_frame,
        "corner_overlay_ratio": round(overlay_ratio, 4),
        "dark_fraction": round(dark_frac, 5),
        "bright_fraction": round(bright_frac, 5),
        "aspect_ratio": round(aspect, 5),
        "min_side": int(min(width, height)),
        "megapixels": round(width * height / 1e6, 4),
    }


NUMERIC_KEYS = [
    "brightness",
    "contrast",
    "blur_laplacian_var",
    "sharpness_norm",
    "entropy_bits",
    "saturation",
    "colorfulness",
    "edge_density",
    "border_brightness",
    "border_uniformity_std",
    "center_brightness",
    "corner_overlay_ratio",
    "dark_fraction",
    "bright_fraction",
    "aspect_ratio",
    "min_side",
    "megapixels",
]


def robust_z(values: np.ndarray) -> np.ndarray:
    """Robust z-score using median and MAD (scaled to be consistent with std for normal data)."""
    med = np.median(values)
    mad = np.median(np.abs(values - med)) * 1.4826
    if mad < 1e-9:
        mad = values.std() + 1e-9
    return (values - med) / mad
