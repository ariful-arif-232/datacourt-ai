"""Privacy contamination scan (`privacy-scan-v1`).

Flags *potential* privacy-sensitive content only:
  * faces: OpenCV Haar cascade (frontal face) detector — detection, never recognition;
  * text-like regions: MSER blobs arranged like characters (a heuristic, no OCR).
Configurable per project because many datasets legitimately contain people.
"""

from __future__ import annotations

import os
import threading

import cv2
import numpy as np

from datacourt import algorithms

VERSION = algorithms.PRIVACY
_local = threading.local()


def _cascade() -> cv2.CascadeClassifier:
    if not hasattr(_local, "face"):
        path = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
        _local.face = cv2.CascadeClassifier(path)
    return _local.face


def detect_faces(rgb: np.ndarray, min_size_frac: float) -> list[list[int]]:
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    ms = max(20, int(min(h, w) * min_size_frac))
    faces = _cascade().detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(ms, ms))
    return [[int(x), int(y), int(fw), int(fh)] for (x, y, fw, fh) in (faces if len(faces) else [])]


def text_like_regions(rgb: np.ndarray) -> int:
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    mser = cv2.MSER_create()
    mser.setMinArea(max(12, (h * w) // 40000))
    mser.setMaxArea(max(200, (h * w) // 200))
    _, boxes = mser.detectRegions(gray)
    if boxes is None or len(boxes) == 0:
        return 0
    cand = []
    for x, y, bw, bh in boxes:
        ar = bw / max(1, bh)
        if 6 <= bh <= h * 0.12 and 0.15 <= ar <= 1.5:
            cand.append((x, y, bw, bh))
    if len(cand) < 3:
        return 0
    # Count characters that have a horizontally-aligned, similar-height neighbour (line structure).
    arr = np.array(cand, dtype=np.float32)
    cy = arr[:, 1] + arr[:, 3] / 2
    aligned = 0
    for i in range(len(arr)):
        dy = np.abs(cy - cy[i])
        dx = np.abs(arr[:, 0] - arr[i, 0])
        hs = np.abs(arr[:, 3] - arr[i, 3]) / max(1.0, arr[i, 3])
        close = (dy < arr[i, 3] * 0.3) & (dx > 0) & (dx < arr[i, 3] * 2.5) & (hs < 0.3)
        if close.sum() >= 1:
            aligned += 1
    return int(aligned)


def scan(rgb: np.ndarray, cfg: dict) -> list[dict]:
    out = []
    faces = detect_faces(rgb, cfg["face_min_size_frac"])
    if faces:
        out.append(
            {
                "kind": "face",
                "count": len(faces),
                "score": 1.0,
                "boxes": faces[:10],
                "image_size": [int(rgb.shape[1]), int(rgb.shape[0])],
            }
        )
    t = text_like_regions(rgb)
    if t >= cfg["text_min_regions"]:
        out.append(
            {
                "kind": "text_region",
                "count": t,
                "score": min(1.0, t / 30),
                "boxes": [],
                "image_size": [int(rgb.shape[1]), int(rgb.shape[0])],
            }
        )
    return out
