"""Train/validation/test leakage evidence (algorithm `leakage-v1`).

A duplicate family that spans splits is possible evaluation leakage. Visual similarity
across splits that falls short of near-duplicate thresholds is reported separately as a
low-risk "similar subject" cluster: it may be the same object/scene photographed twice,
but identity is never asserted.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from datacourt import algorithms
from datacourt.enums import LeakageKind, Relation, Risk
from datacourt.ml.duplicates import _UF, TRANSFORM_RELATIONS, Family

VERSION = algorithms.LEAKAGE
EVAL = {"val", "test"}


@dataclass
class Leak:
    family_index: int | None
    kind: LeakageKind
    risk: Risk
    splits: list[str]
    members: list[int]
    max_similarity: float
    explanation: str
    evidence: dict


def _crosses(splits: set[str]) -> bool:
    return len(splits - {"unsplit"}) >= 2


def family_leaks(families: list[Family], split: list[str], labels: list[str]) -> list[Leak]:
    leaks: list[Leak] = []
    for fi, fam in enumerate(families):
        splits = {split[i] for i in fam.members}
        if not _crosses(splits):
            continue
        cross_edges = [e for e in fam.edges if split[e.a] != split[e.b]]
        rels = {e.relation for e in cross_edges}
        pairs = {tuple(sorted((split[e.a], split[e.b]))) for e in cross_edges}
        touches_test = any("test" in p for p in pairs)
        if Relation.EXACT in rels:
            kind = LeakageKind.EXACT_CROSS_SPLIT
            risk = Risk.CRITICAL if touches_test else Risk.HIGH
        elif rels & TRANSFORM_RELATIONS:
            kind = LeakageKind.TRANSFORMED_CROSS_SPLIT
            risk = Risk.HIGH if touches_test else Risk.MEDIUM
        else:
            kind = LeakageKind.NEAR_CROSS_SPLIT
            risk = Risk.MEDIUM
        label_set = {labels[i] for i in fam.members}
        max_sim = max((e.cosine for e in cross_edges), default=0.0)
        n_eval = sum(1 for i in fam.members if split[i] in EVAL)
        explanation = (
            f"Family of {len(fam.members)} visually matching samples spans {', '.join(sorted(splits))}. "
            f"{n_eval} evaluation sample(s) have a {'byte-identical' if kind == LeakageKind.EXACT_CROSS_SPLIT else 'transformed or near-identical'} "
            "counterpart elsewhere, so evaluation may overstate generalization."
        )
        if len(label_set) > 1:
            explanation += f" Members also carry conflicting labels ({', '.join(sorted(label_set))})."
        leaks.append(
            Leak(
                fi,
                kind,
                risk,
                sorted(splits),
                list(fam.members),
                round(max_sim, 4),
                explanation,
                {
                    "cross_split_relations": sorted(str(r) for r in rels),
                    "split_pairs": [list(p) for p in sorted(pairs)],
                    "label_conflict": len(label_set) > 1,
                    "eval_members": n_eval,
                },
            )
        )
    return leaks


def similar_subject_clusters(
    knn_sims: np.ndarray,
    knn_idx: np.ndarray,
    split: list[str],
    in_family: set[int],
    lo: float,
    hi: float,
    max_clusters: int,
) -> list[Leak]:
    n = len(split)
    uf = _UF(n)
    pair_sims: dict[tuple[int, int], float] = {}
    for i in range(n):
        for c in range(knn_idx.shape[1]):
            j = int(knn_idx[i, c])
            s = float(knn_sims[i, c])
            if j < 0 or s < lo or s >= hi:
                continue
            if split[i] == split[j] or "unsplit" in (split[i], split[j]):
                continue
            if i in in_family and j in in_family:
                continue
            uf.union(i, j)
            key = (min(i, j), max(i, j))
            pair_sims[key] = max(pair_sims.get(key, 0.0), s)
    groups: dict[int, set[int]] = {}
    group_sims: dict[int, list[float]] = {}
    for (i, j), sim in pair_sims.items():
        r = uf.find(i)
        groups.setdefault(r, set()).update((i, j))
        group_sims.setdefault(r, []).append(sim)
    out: list[Leak] = []
    for r, members_set in groups.items():
        members = sorted(members_set)
        splits = sorted({split[k] for k in members})
        sims = group_sims[r]
        out.append(
            Leak(
                None,
                LeakageKind.SIMILAR_SUBJECT_CLUSTER,
                Risk.LOW,
                splits,
                members,
                round(max(sims), 4),
                f"{len(members)} samples across {', '.join(splits)} are highly similar (cosine {min(sims):.3f}–{max(sims):.3f}) "
                "without meeting duplicate thresholds. They may show the same subject or scene; review whether the split "
                "should be grouped by subject.",
                {"pair_count": len(sims), "similarity_range": [round(min(sims), 4), round(max(sims), 4)]},
            )
        )
    out.sort(key=lambda lk: (-lk.max_similarity, -len(lk.members)))
    return out[:max_clusters]


def split_integrity(
    split: list[str],
    labels: list[str],
    emb: np.ndarray,
    families: list[Family],
    sha: list[str],
    thresholds: dict,
) -> dict:
    """Per-evaluation-split contamination and coverage metrics."""
    from datacourt.ml.knn import knn

    split_arr = np.array(split)
    train_idx = np.nonzero(split_arr == "train")[0]
    metrics: dict = {"splits": {}, "class_presence": {}}
    classes = sorted(set(labels))
    for sp in sorted(set(split)):
        sp_labels = {labels[i] for i in range(len(labels)) if split[i] == sp}
        metrics["class_presence"][sp] = {c: (c in sp_labels) for c in classes}
    train_sha = {sha[i] for i in train_idx}
    member_of: dict[int, int] = {}
    for fi, f in enumerate(families):
        for mbr in f.members:
            member_of[mbr] = fi
    for sp in ("val", "test"):
        idx = np.nonzero(split_arr == sp)[0]
        if len(idx) == 0:
            continue
        exact = sum(1 for i in idx if sha[i] in train_sha)
        fam_cross = sum(
            1
            for i in idx
            if i in member_of and any(split[m] == "train" for m in families[member_of[i]].members)
        )
        entry: dict = {
            "count": int(len(idx)),
            "exact_in_train": int(exact),
            "family_linked_to_train": int(fam_cross),
        }
        if len(train_idx):
            sims, _ = knn(emb[idx], emb[train_idx], 1)
            nearest = sims[:, 0]
            hist, edges = np.histogram(nearest, bins=10, range=(float(min(0.0, nearest.min())), 1.0))
            entry.update(
                {
                    "nearest_train_similarity_median": round(float(np.median(nearest)), 4),
                    "above_near_duplicate": int((nearest >= thresholds["near_duplicate_cosine"]).sum()),
                    "above_similar_subject": int((nearest >= thresholds["similar_subject_cosine"]).sum()),
                    "nearest_train_similarity_hist": {
                        "counts": hist.tolist(),
                        "edges": [round(float(e), 3) for e in edges],
                    },
                }
            )
        entry["contaminated_fraction"] = round(entry["family_linked_to_train"] / len(idx), 4)
        metrics["splits"][sp] = entry
    return metrics
