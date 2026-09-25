"""Training dynamics (dataset cartography) and TracIn influence for the linear head.

Dynamics (`cartography-v1`), following Swayamdipta et al. (2020) and Toneva et al. (2019):
  confidence   mean probability of the given label across epochs
  variability  std of that probability across epochs
  correctness  fraction of epochs where argmax == given label
  forgetting   number of correct -> incorrect transitions

Influence (`tracin-linear-v1`), following Pruthi et al. (2020): for the softmax head with
standardized input x (plus bias), the per-sample loss gradient is (p - y) ⊗ [x, 1], so
    TracIn(z, z') = Σ_c η_c (p_c(z) - y(z))·(p_c(z') - y(z')) · (x·x' + 1)
This is exact for the head's SGD checkpoints but ignores the frozen embedding network,
so it is an *approximation* of influence on the full system and is labeled as such.
Positive values: training on z reduced loss on z' (proponent). Negative: increased it.
"""

from __future__ import annotations

import numpy as np

from datacourt import algorithms
from datacourt.enums import DynamicsCategory
from datacourt.ml.baseline import TrainResult, softmax

DYNAMICS_VERSION = algorithms.DYNAMICS
INFLUENCE_VERSION = algorithms.INFLUENCE


def dynamics_table(res: TrainResult, cfg: dict) -> dict[str, np.ndarray]:
    P = res.epoch_p_given
    C = res.epoch_correct
    assert P is not None and C is not None
    conf = P.mean(axis=0)
    var = P.std(axis=0)
    corr = C.mean(axis=0)
    forgetting = (C[:-1] & ~C[1:]).sum(axis=0) if C.shape[0] > 1 else np.zeros(C.shape[1], int)
    E = C.shape[0]
    first_learned = np.full(C.shape[1], -1)
    for i in range(C.shape[1]):
        col = C[:, i]
        # first epoch after which the sample stays correct
        for e in range(E):
            if col[e:].all():
                first_learned[i] = e
                break
    cats = []
    for i in range(len(conf)):
        if conf[i] < cfg["hard_confidence"] and corr[i] < cfg["hard_correctness"]:
            cats.append(DynamicsCategory.HARD)
        elif forgetting[i] >= cfg["forgotten_min_events"]:
            cats.append(DynamicsCategory.FORGOTTEN)
        elif var[i] >= cfg["ambiguous_variability"]:
            cats.append(DynamicsCategory.AMBIGUOUS)
        elif conf[i] >= cfg["easy_confidence"] and corr[i] >= cfg["easy_correctness"]:
            cats.append(DynamicsCategory.EASY)
        else:
            cats.append(DynamicsCategory.UNSTABLE)
    return {
        "confidence": conf,
        "variability": var,
        "correctness": corr,
        "forgetting": forgetting,
        "first_learned": first_learned,
        "category": np.array([str(c) for c in cats]),
        "trajectory": P.T,
    }


def _residuals(res: TrainResult, Xs: np.ndarray, y: np.ndarray, W: np.ndarray, b: np.ndarray) -> np.ndarray:
    P = softmax(Xs @ W + b)
    P[np.arange(len(y)), y] -= 1.0
    return P


def tracin(
    res: TrainResult,
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_eval: np.ndarray,
    y_eval: np.ndarray,
    block: int = 4096,
) -> tuple[np.ndarray, np.ndarray]:
    """Returns (self_influence[n_train], influence[n_train, n_eval])."""
    Xt = res.standardize(X_train.astype(np.float64))
    Xe = res.standardize(X_eval.astype(np.float64))
    n_t, n_e = len(y_train), len(y_eval)
    self_inf = np.zeros(n_t)
    infl = np.zeros((n_t, n_e), dtype=np.float32)
    xt_sq = (Xt**2).sum(axis=1) + 1.0
    for W, b, lr in res.checkpoints:
        Rt = _residuals(res, Xt, y_train, W, b)
        Re = _residuals(res, Xe, y_eval, W, b) if n_e else np.zeros((0, W.shape[1]))
        self_inf += lr * (Rt**2).sum(axis=1) * xt_sq
        for s in range(0, n_t, block):
            kernel = Xt[s : s + block] @ Xe.T + 1.0
            infl[s : s + block] += (lr * (Rt[s : s + block] @ Re.T) * kernel).astype(np.float32)
    # Per-dimension scale so values are comparable across datasets.
    scale = float(Xt.shape[1])
    return self_inf / scale, infl / scale


def percentile_rank(values: np.ndarray) -> np.ndarray:
    if len(values) == 0:
        return values
    order = values.argsort(kind="stable")
    ranks = np.empty(len(values))
    ranks[order] = np.arange(len(values))
    return ranks / max(1, len(values) - 1)
