"""Visual embedding backends.

`dc-descriptor-v1`
    Deterministic CPU descriptor: HSV colour histogram, spatial Lab layout, coarse and
    fine HOG gradients, spatial uniform-LBP texture and a normalized tiny image,
    block-normalized, weighted and concatenated (2588 dims). No downloads, runs anywhere, bit-reproducible. It captures
    appearance (color, texture, shape layout), not deep semantics — see
    docs/ALGORITHM_VERSIONS.md for the trade-off.

`dinov2-small`
    Self-supervised ViT-S/14 (facebook/dinov2-small) via `transformers`. The Hub revision
    comes from DINOV2_REVISION (set it to a commit SHA in production to pin weights).
    Requires the optional `deep` extra and network access to the HuggingFace Hub (or a
    pre-populated MODEL_CACHE_DIR). CLS token and mean patch token, concatenated (768 dims).

All outputs are float32 and L2-normalized, so inner product == cosine similarity.
"""

from __future__ import annotations

from typing import Protocol

import cv2
import numpy as np


class EmbeddingBackend(Protocol):
    name: str
    version: str
    preprocessing_version: str
    dim: int
    source: str

    def embed(self, images: list[np.ndarray]) -> np.ndarray: ...


def _l2(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v, axis=-1, keepdims=True)
    return v / np.maximum(n, 1e-12)


def _uniform_lbp_table() -> np.ndarray:
    table = np.full(256, 58, dtype=np.int32)
    k = 0
    for code in range(256):
        bits = [(code >> i) & 1 for i in range(8)]
        transitions = sum(bits[i] != bits[(i + 1) % 8] for i in range(8))
        if transitions <= 2:
            table[code] = k
            k += 1
    return table  # 58 uniform patterns + 1 non-uniform bin


_LBP_TABLE = _uniform_lbp_table()


def _lbp(gray: np.ndarray) -> np.ndarray:
    g = gray.astype(np.int16)
    c = g[1:-1, 1:-1]
    offsets = [(-1, -1), (-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1)]
    code = np.zeros_like(c, dtype=np.int32)
    for bit, (dy, dx) in enumerate(offsets):
        nb = g[1 + dy : g.shape[0] - 1 + dy, 1 + dx : g.shape[1] - 1 + dx]
        code |= (nb >= c).astype(np.int32) << bit
    return _LBP_TABLE[code]


class DescriptorBackend:
    name = "dc-descriptor"
    version = "v1"
    preprocessing_version = "resize128-area-v1"
    source = "built-in (OpenCV/NumPy)"
    dim = 72 + 48 + 324 + 1764 + 236 + 144

    def __init__(self) -> None:
        self._hog_coarse = cv2.HOGDescriptor((64, 64), (32, 32), (16, 16), (16, 16), 9)
        self._hog_fine = cv2.HOGDescriptor((64, 64), (16, 16), (8, 8), (8, 8), 9)

    def _one(self, rgb: np.ndarray) -> np.ndarray:
        img = cv2.resize(rgb, (128, 128), interpolation=cv2.INTER_AREA)
        hsv = cv2.cvtColor(img, cv2.COLOR_RGB2HSV)
        hist = cv2.calcHist([hsv], [0, 1, 2], None, [8, 3, 3], [0, 180, 0, 256, 0, 256]).ravel()
        color = np.sqrt(hist / max(hist.sum(), 1.0))

        lab = cv2.cvtColor(img, cv2.COLOR_RGB2LAB).astype(np.float32)
        grid = cv2.resize(lab, (4, 4), interpolation=cv2.INTER_AREA).reshape(-1) / 255.0
        layout = grid - grid.mean()

        gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
        g64 = cv2.resize(gray, (64, 64), interpolation=cv2.INTER_AREA)
        hog_c = self._hog_coarse.compute(g64).ravel()
        hog_f = self._hog_fine.compute(g64).ravel()

        codes = _lbp(gray)
        h2, w2 = codes.shape[0] // 2, codes.shape[1] // 2
        lbp_parts = []
        for qy in range(2):
            for qx in range(2):
                q = codes[qy * h2 : (qy + 1) * h2, qx * w2 : (qx + 1) * w2]
                hq = np.bincount(q.ravel(), minlength=59).astype(np.float32)
                lbp_parts.append(np.sqrt(hq / max(hq.sum(), 1.0)))
        lbp = np.concatenate(lbp_parts)

        tiny = cv2.resize(gray, (12, 12), interpolation=cv2.INTER_AREA).astype(np.float32).ravel()
        tiny = (tiny - tiny.mean()) / (tiny.std() + 1e-6)

        blocks = [(color, 0.8), (layout, 0.5), (hog_c, 1.0), (hog_f, 1.0), (lbp, 0.7), (tiny, 0.4)]
        return np.concatenate([_l2(b.astype(np.float32)) * w for b, w in blocks])

    def embed(self, images: list[np.ndarray]) -> np.ndarray:
        if not images:
            return np.zeros((0, self.dim), dtype=np.float32)
        return _l2(np.stack([self._one(im) for im in images]).astype(np.float32))


class Dinov2Backend:
    name = "dinov2-small"
    preprocessing_version = "resize256-center224-imagenet-v1"
    source = "https://huggingface.co/facebook/dinov2-small"
    dim = 768

    def __init__(self, cache_dir: str) -> None:
        import os

        import torch  # optional dependency
        from transformers import AutoModel

        self.revision = os.environ.get("DINOV2_REVISION", "main")
        self.version = f"facebook/dinov2-small@{self.revision[:12]}"
        self._torch = torch
        torch.set_num_threads(max(1, (torch.get_num_threads() or 2)))
        self.model = AutoModel.from_pretrained(
            "facebook/dinov2-small", revision=self.revision, cache_dir=cache_dir
        )
        self.model.eval()
        self.mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
        self.std = np.array([0.229, 0.224, 0.225], dtype=np.float32)

    def _prep(self, rgb: np.ndarray) -> np.ndarray:
        h, w = rgb.shape[:2]
        s = 256 / min(h, w)
        img = cv2.resize(rgb, (max(224, round(w * s)), max(224, round(h * s))), interpolation=cv2.INTER_AREA)
        hh, ww = img.shape[:2]
        y, x = (hh - 224) // 2, (ww - 224) // 2
        img = img[y : y + 224, x : x + 224].astype(np.float32) / 255.0
        return ((img - self.mean) / self.std).transpose(2, 0, 1)

    def embed(self, images: list[np.ndarray]) -> np.ndarray:
        torch = self._torch
        if not images:
            return np.zeros((0, self.dim), dtype=np.float32)
        batch = torch.from_numpy(np.stack([self._prep(im) for im in images]))
        with torch.no_grad():
            out = self.model(pixel_values=batch).last_hidden_state
        cls = out[:, 0]
        patch = out[:, 1:].mean(dim=1)
        vec = torch.cat([cls, patch], dim=1).numpy().astype(np.float32)
        return _l2(vec)


def load_backend(name: str, cache_dir: str) -> tuple[EmbeddingBackend, list[str]]:
    """Returns (backend, warnings). Falls back to the descriptor backend if DINOv2 is unavailable."""
    warnings: list[str] = []
    if name == "dinov2-small":
        try:
            return Dinov2Backend(cache_dir), warnings
        except Exception as exc:  # noqa: BLE001
            warnings.append(
                f"DINOv2 backend unavailable ({type(exc).__name__}); fell back to dc-descriptor-v1. "
                "Install the 'deep' extra and allow HuggingFace access to enable it."
            )
    return DescriptorBackend(), warnings


def backend_id(backend: EmbeddingBackend) -> str:
    return f"{backend.name}-{backend.version}"
