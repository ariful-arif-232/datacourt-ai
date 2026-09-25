"""Exact cosine nearest neighbours (FAISS IndexFlatIP, NumPy fallback).

Exact search keeps results reproducible; for very large datasets swap in an IVF/HNSW
index behind the same function.
"""

from __future__ import annotations

import numpy as np

try:  # pragma: no cover - import guard
    import faiss

    faiss.omp_set_num_threads(4)
except Exception:  # noqa: BLE001
    faiss = None


def knn(
    query: np.ndarray, base: np.ndarray, k: int, exclude_self: bool = False
) -> tuple[np.ndarray, np.ndarray]:
    """Returns (similarities, indices) of shape (n_query, k). Missing slots are (-inf, -1)."""
    query = np.ascontiguousarray(query, dtype=np.float32)
    base = np.ascontiguousarray(base, dtype=np.float32)
    kk = min(base.shape[0], k + (1 if exclude_self else 0))
    if kk == 0 or query.shape[0] == 0:
        return np.full((query.shape[0], k), -np.inf, np.float32), np.full((query.shape[0], k), -1, np.int64)
    if faiss is not None:
        index = faiss.IndexFlatIP(base.shape[1])
        index.add(base)
        sims, idx = index.search(query, kk)
    else:
        sims = np.empty((query.shape[0], kk), np.float32)
        idx = np.empty((query.shape[0], kk), np.int64)
        for s in range(0, query.shape[0], 2048):
            block = query[s : s + 2048] @ base.T
            part = np.argpartition(-block, kk - 1, axis=1)[:, :kk]
            ps = np.take_along_axis(block, part, axis=1)
            order = np.argsort(-ps, axis=1, kind="stable")
            idx[s : s + 2048] = np.take_along_axis(part, order, axis=1)
            sims[s : s + 2048] = np.take_along_axis(ps, order, axis=1)
    if exclude_self:
        out_s = np.full((query.shape[0], k), -np.inf, np.float32)
        out_i = np.full((query.shape[0], k), -1, np.int64)
        for r in range(query.shape[0]):
            keep = idx[r] != r
            si, ii = sims[r][keep][:k], idx[r][keep][:k]
            out_s[r, : len(si)] = si
            out_i[r, : len(ii)] = ii
        return out_s, out_i
    if kk < k:
        pad = k - kk
        sims = np.pad(sims, ((0, 0), (0, pad)), constant_values=-np.inf)
        idx = np.pad(idx, ((0, 0), (0, pad)), constant_values=-1)
    return sims.astype(np.float32), idx.astype(np.int64)
