"""Duplicate & near-duplicate forensics (algorithm `dup-families-v2`).

Evidence combined per candidate pair:
  * SHA-256 equality                  -> exact duplicate (deterministic)
  * decoded-pixel SHA-256 equality    -> exact duplicate: the same picture in another encoding
                                         (a HEIC and a lossless PNG of it, say; v2)
  * perceptual hash Hamming distance  -> resized / re-encoded / photometric variants
  * pHash against the mirrored image  -> horizontally flipped variants
  * embedding cosine similarity       -> candidate generation
  * detail-layer NCC (direct / mirrored) -> required verification for resize/edit/flip
  * ORB keypoints + RANSAC transform, then detail NCC of the warped overlap -> crops

Relation types other than `exact` are *heuristic* descriptions of how two images relate;
they are labeled as such in the UI. Pairs are grouped into families with union-find and
each family gets a lineage tree rooted at its highest-resolution ("source-like") member.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

from datacourt import algorithms
from datacourt.enums import Relation

VERSION = algorithms.DUPLICATES
TRANSFORM_RELATIONS = {Relation.RESIZED, Relation.FLIPPED, Relation.CROPPED, Relation.PHOTOMETRIC}


@dataclass
class Edge:
    a: int
    b: int
    relation: Relation
    cosine: float
    phash_distance: int
    evidence: dict


@dataclass
class Family:
    members: list[int]
    root: int
    parent: dict[int, int | None]
    parent_edge: dict[int, Edge | None]
    depth: dict[int, int]
    edges: list[Edge] = field(default_factory=list)
    kind: str = "near"


def popcount64(x: np.ndarray) -> np.ndarray:
    return np.bitwise_count(x)


STRUCT_SIDE = 64


def structure_thumb(gray: np.ndarray) -> np.ndarray:
    """64x64 grayscale thumbnail used for structural verification of duplicate candidates."""
    import cv2

    return cv2.resize(gray, (STRUCT_SIDE, STRUCT_SIDE), interpolation=cv2.INTER_AREA).astype(np.uint8)


def _z(v: np.ndarray) -> np.ndarray:
    v = v.astype(np.float32).ravel()
    v = v - v.mean()
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-6 else v * 0.0


def _detail(t: np.ndarray) -> np.ndarray:
    import cv2

    t = t.astype(np.float32)
    return t - cv2.GaussianBlur(t, (0, 0), 1.5)


def _band(t: np.ndarray) -> np.ndarray:
    """Difference-of-Gaussians band (σ 0.7–3): texture that survives warping and JPEG noise."""
    import cv2

    t = t.astype(np.float32)
    return cv2.GaussianBlur(t, (0, 0), 0.7) - cv2.GaussianBlur(t, (0, 0), 3.0)


def structural_similarity(a: np.ndarray, b: np.ndarray) -> dict:
    """Normalized cross-correlation of the detail (high-pass) layer, direct and mirrored.

    The detail layer carries texture/edges that survive resizing, re-encoding and
    brightness changes but differ between distinct photos, even when their overall
    silhouettes are alike (a failure mode of raw-intensity correlation and pHash).
    """
    da, db = _z(_detail(a)), _z(_detail(b))
    return {
        "ncc": round(float(_z(a) @ _z(b)), 4),
        "detail_ncc": round(float(da @ db), 4),
        "detail_ncc_mirror": round(float(da @ _z(_detail(b[:, ::-1]))), 4),
    }


class KeypointVerifier:
    """ORB keypoints + RANSAC similarity transform, for crops that correlation cannot align."""

    def __init__(self, load_gray, work_side: int = 320) -> None:
        import cv2

        self._cv2 = cv2
        self._load = load_gray
        self.work_side = work_side
        self._orb = cv2.ORB_create(nfeatures=600, fastThreshold=10)
        self._bf = cv2.BFMatcher(cv2.NORM_HAMMING)
        self._cache: dict[int, tuple] = {}

    def _features(self, i: int) -> tuple:
        if i not in self._cache:
            if len(self._cache) > 3000:
                self._cache.clear()
            g = self._load(i)
            scale = self.work_side / max(g.shape)
            if abs(scale - 1) > 0.05:
                g = self._cv2.resize(
                    g,
                    (max(8, round(g.shape[1] * scale)), max(8, round(g.shape[0] * scale))),
                    interpolation=self._cv2.INTER_LINEAR if scale > 1 else self._cv2.INTER_AREA,
                )
            k, d = self._orb.detectAndCompute(g, None)
            self._cache[i] = (k, d, g)
        return self._cache[i]

    def __call__(self, i: int, j: int) -> dict:
        cv2 = self._cv2
        k1, d1, g1 = self._features(i)
        k2, d2, g2 = self._features(j)
        if d1 is None or d2 is None or len(k1) < 8 or len(k2) < 8:
            return {"inliers": 0, "inlier_ratio": 0.0}
        pairs = [x for x in self._bf.knnMatch(d1, d2, k=2) if len(x) == 2]
        good = [a for a, b in pairs if a.distance < 0.8 * b.distance]
        if len(good) < 6:
            return {"inliers": 0, "inlier_ratio": 0.0, "matches": len(good)}
        src = np.float32([k1[g.queryIdx].pt for g in good])
        dst = np.float32([k2[g.trainIdx].pt for g in good])
        M, inl = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=4.0)
        if M is None or inl is None:
            return {"inliers": 0, "inlier_ratio": 0.0, "matches": len(good)}
        n = int(inl.sum())
        scale = float(np.sqrt(abs(np.linalg.det(M[:, :2]))))
        rot = float(np.degrees(np.arctan2(M[1, 0], M[0, 0])))
        # Verify pixel content: warp image i into j's frame and correlate fine detail on the overlap.
        h2, w2 = g2.shape
        warped = cv2.warpAffine(g1.astype(np.float32), M, (w2, h2), flags=cv2.INTER_LINEAR, borderValue=-1)
        mask = (
            cv2.warpAffine(
                np.ones_like(g1, dtype=np.float32), M, (w2, h2), flags=cv2.INTER_NEAREST, borderValue=0
            )
            > 0.5
        )
        mask = cv2.erode(mask.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
        overlap = float(mask.mean())
        overlap_ncc = -1.0
        if mask.sum() > 400:
            da = _band(warped)[mask]
            db = _band(g2.astype(np.float32))[mask]
            overlap_ncc = float(_z(da) @ _z(db))
        return {
            "inliers": n,
            "inlier_ratio": round(n / len(good), 3),
            "matches": len(good),
            "scale": round(scale, 3),
            "rotation_deg": round(rot, 1),
            "overlap_fraction": round(overlap, 3),
            "overlap_detail_ncc": round(overlap_ncc, 4),
        }


def phash_pairs(ph: np.ndarray, ph_flip: np.ndarray, max_dist: int, max_n: int) -> set[tuple[int, int]]:
    """All pairs (i<j) with direct or mirrored pHash distance <= max_dist (blockwise brute force)."""
    n = len(ph)
    pairs: set[tuple[int, int]] = set()
    if n < 2 or n > max_n:
        return pairs
    block = 1024
    for s in range(0, n, block):
        rows = ph[s : s + block][:, None]
        d = np.minimum(popcount64(rows ^ ph[None, :]), popcount64(rows ^ ph_flip[None, :]))
        ii, jj = np.nonzero(d <= max_dist)
        for i, j in zip(ii + s, jj, strict=True):
            if i < j:
                pairs.add((int(i), int(j)))
    return pairs


def classify_pair(
    i: int,
    j: int,
    *,
    sha: list[str],
    ph: np.ndarray,
    ph_flip: np.ndarray,
    width: np.ndarray,
    height: np.ndarray,
    brightness: np.ndarray,
    saturation: np.ndarray,
    emb: np.ndarray,
    struct: np.ndarray,
    cfg: dict,
    verifier: KeypointVerifier | None = None,
    pixel: list[str | None] | None = None,
) -> Edge | None:
    """Relation between two candidate images, or None if they are not duplicates.

    Candidates come from hashes and embeddings; acceptance always requires structural
    evidence (NCC), so embedding similarity alone never creates a duplicate edge.
    """
    cos = float(emb[i] @ emb[j])
    d = int(popcount64(np.array([ph[i] ^ ph[j]], dtype=np.uint64))[0])
    d_flip = int(
        min(
            popcount64(np.array([ph[i] ^ ph_flip[j]], dtype=np.uint64))[0],
            popcount64(np.array([ph_flip[i] ^ ph[j]], dtype=np.uint64))[0],
        )
    )
    ar_i, ar_j = width[i] / height[i], width[j] / height[j]
    ar_diff = abs(ar_i - ar_j) / max(ar_i, ar_j)
    area_i, area_j = float(width[i] * height[i]), float(width[j] * height[j])
    area_ratio = min(area_i, area_j) / max(area_i, area_j)
    same_dims = width[i] == width[j] and height[i] == height[j]
    photometric_delta = abs(float(brightness[i] - brightness[j])) + 0.5 * abs(
        float(saturation[i] - saturation[j])
    )
    t = cfg
    ev = {
        "cosine": round(cos, 4),
        "phash_distance": d,
        "phash_mirror_distance": d_flip,
        "aspect_ratio_diff": round(float(ar_diff), 4),
        "area_ratio": round(float(area_ratio), 4),
        "photometric_delta": round(photometric_delta, 2),
    }
    if sha[i] == sha[j]:
        return Edge(i, j, Relation.EXACT, cos, d, {**ev, "ncc": 1.0, "rule": "identical SHA-256"})
    if pixel is not None and pixel[i] and pixel[i] == pixel[j]:
        return Edge(
            i,
            j,
            Relation.EXACT,
            cos,
            d,
            {**ev, "ncc": 1.0, "rule": "identical decoded pixels (same picture, different file encoding)"},
        )
    st = structural_similarity(struct[i], struct[j])
    ev.update(st)
    T = t["struct_min_detail_ncc"]
    if st["detail_ncc"] >= T:
        if not same_dims and ar_diff < 0.03:
            return Edge(
                i,
                j,
                Relation.RESIZED,
                cos,
                d,
                {**ev, "rule": "same fine detail and aspect ratio, different size"},
            )
        if photometric_delta > t["photometric_min_delta"]:
            return Edge(
                i,
                j,
                Relation.PHOTOMETRIC,
                cos,
                d,
                {**ev, "rule": "same fine detail with brightness/colour change"},
            )
        if ar_diff >= 0.03:
            return Edge(
                i, j, Relation.CROPPED, cos, d, {**ev, "rule": "same fine detail with changed aspect ratio"}
            )
        return Edge(
            i, j, Relation.NEAR, cos, d, {**ev, "rule": "same fine detail (re-encoded or minor edit)"}
        )
    if st["detail_ncc_mirror"] >= T:
        return Edge(i, j, Relation.FLIPPED, cos, d, {**ev, "rule": "fine detail matches the mirrored image"})
    if verifier is not None and (cos >= t["crop_min_cosine"] or d <= t["phash_candidate_max"]):
        # Match from the larger image onto the smaller one, so a crop's overlap covers the smaller frame.
        kp = verifier(i, j) if area_i >= area_j else verifier(j, i)
        ev["keypoints"] = kp
        if (
            kp["inliers"] >= t["crop_min_inliers"]
            and kp["inlier_ratio"] >= t["crop_min_inlier_ratio"]
            and kp.get("overlap_detail_ncc", -1) >= t["crop_min_overlap_ncc"]
            and kp.get("overlap_fraction", 0) >= t["crop_min_overlap_fraction"]
        ):
            return Edge(
                i,
                j,
                Relation.CROPPED,
                cos,
                d,
                {**ev, "rule": "keypoints agree on a geometric transform (crop / rescale / shift)"},
            )
    return None


class _UF:
    def __init__(self, n: int) -> None:
        self.p = list(range(n))

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def build_families(n: int, edges: list[Edge], width: np.ndarray, height: np.ndarray) -> list[Family]:
    uf = _UF(n)
    for e in edges:
        uf.union(e.a, e.b)
    groups: dict[int, list[int]] = defaultdict(list)
    touched = {e.a for e in edges} | {e.b for e in edges}
    for i in touched:
        groups[uf.find(i)].append(i)
    adj: dict[int, list[Edge]] = defaultdict(list)
    for e in edges:
        adj[e.a].append(e)
        adj[e.b].append(e)
    families: list[Family] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort()
        root = max(members, key=lambda k: (int(width[k]) * int(height[k]), -k))
        # Prim's maximum spanning tree from the root, preferring exact > transforms > near, then similarity.
        rel_rank = {
            Relation.EXACT: 3,
            Relation.RESIZED: 2,
            Relation.FLIPPED: 2,
            Relation.PHOTOMETRIC: 2,
            Relation.CROPPED: 1,
            Relation.NEAR: 1,
        }
        parent: dict[int, int | None] = {root: None}
        parent_edge: dict[int, Edge | None] = {root: None}
        depth = {root: 0}
        frontier = list(adj[root])
        while frontier and len(parent) < len(members):
            frontier.sort(key=lambda e: (rel_rank[e.relation], e.cosine, -e.phash_distance))
            e = frontier.pop()
            src, dst = (e.a, e.b) if e.a in parent else (e.b, e.a)
            if dst in parent:
                continue
            parent[dst] = src
            parent_edge[dst] = e
            depth[dst] = depth[src] + 1
            frontier.extend(x for x in adj[dst] if (x.a not in parent or x.b not in parent))
        fam_edges = [e for e in edges if uf.find(e.a) == uf.find(members[0])]
        rels = {e.relation for e in fam_edges}
        kind = (
            "exact" if rels == {Relation.EXACT} else ("transformed" if rels & TRANSFORM_RELATIONS else "near")
        )
        families.append(
            Family(
                members=members,
                root=root,
                parent=parent,
                parent_edge=parent_edge,
                depth=depth,
                edges=fam_edges,
                kind=kind,
            )
        )
    families.sort(key=lambda f: (-len(f.members), f.root))
    return families


def find_duplicates(
    *,
    sha: list[str],
    ph: np.ndarray,
    ph_flip: np.ndarray,
    width: np.ndarray,
    height: np.ndarray,
    brightness: np.ndarray,
    saturation: np.ndarray,
    emb: np.ndarray,
    struct: np.ndarray,
    knn_sims: np.ndarray,
    knn_idx: np.ndarray,
    cfg: dict,
    verifier: KeypointVerifier | None = None,
    pixel: list[str | None] | None = None,
) -> tuple[list[Edge], list[Family]]:
    """`pixel`: decoded-pixel hashes (None where unknown, e.g. samples ingested before they existed)."""
    n = len(sha)
    candidates: set[tuple[int, int]] = set()
    by_sha: dict[str, list[int]] = defaultdict(list)
    for i, h in enumerate(sha):
        by_sha[h].append(i)
    by_pixels: dict[str, list[int]] = defaultdict(list)
    for i, px in enumerate(pixel or []):
        if px:
            by_pixels[px].append(i)
    for idxs in (*by_sha.values(), *by_pixels.values()):
        for a in range(len(idxs)):
            for b in range(a + 1, len(idxs)):
                candidates.add((idxs[a], idxs[b]))
    candidates |= phash_pairs(ph, ph_flip, cfg["phash_candidate_max"], cfg["phash_bruteforce_max_n"])
    lo = cfg["candidate_min_cosine"]
    rows, cols = np.nonzero(knn_sims >= lo)
    for r, c in zip(rows, cols, strict=True):
        j = int(knn_idx[r, c])
        if j >= 0 and j != r:
            candidates.add((min(r, j), max(r, j)))
    edges = []
    for i, j in sorted(candidates):
        e = classify_pair(
            i,
            j,
            sha=sha,
            ph=ph,
            ph_flip=ph_flip,
            width=width,
            height=height,
            brightness=brightness,
            saturation=saturation,
            emb=emb,
            struct=struct,
            cfg=cfg,
            verifier=verifier,
            pixel=pixel,
        )
        if e is not None:
            edges.append(e)
    return edges, build_families(n, edges, width, height)
