"""Hash helpers that only need NumPy (safe to import from the API process)."""

from __future__ import annotations

import numpy as np


def hex_to_uint64(values: list[str]) -> np.ndarray:
    return np.array([int(v, 16) for v in values], dtype=np.uint64)
