"""Baseline classifier on frozen embeddings (algorithm `baseline-logreg-v2`).

The same model family — a multinomial logistic (linear softmax) head on standardized
embeddings with class-balanced loss — is used everywhere, in two solvers:

  * `fit_logreg`: converged L2-regularized L-BFGS. Deterministic and seed-free; used for
    out-of-fold evidence, evaluation metrics and What-if experiments, so comparisons
    reflect data changes rather than optimizer noise.
  * `train_softmax`: seeded mini-batch SGD (momentum, cosine schedule). Used where the
    *training trajectory* is the evidence: training dynamics (cartography) and TracIn
    checkpoints.

Out-of-fold (OOF) probabilities come from stratified K-fold, so every training sample is
scored by a model that never saw it. Probabilities are temperature-scaled on the OOF
logits; expected calibration error (ECE) is reported before and after so users can see
how far to trust them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import minimize_scalar
from sklearn.metrics import (
    balanced_accuracy_score,
    confusion_matrix,
    f1_score,
    precision_recall_fscore_support,
)
from sklearn.model_selection import StratifiedKFold

from datacourt import algorithms

VERSION = algorithms.BASELINE


@dataclass
class TrainConfig:
    epochs: int = 20
    batch_size: int = 64
    lr: float = 0.05
    momentum: float = 0.9
    weight_decay: float = 1e-4
    class_balanced: bool = True
    seed: int = 1234
    checkpoint_every: int = 2

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class TrainResult:
    W: np.ndarray
    b: np.ndarray
    mean: np.ndarray
    std: np.ndarray
    # Per-epoch probability of the *given* label and argmax correctness on the training set.
    epoch_p_given: np.ndarray | None = None
    epoch_correct: np.ndarray | None = None
    # (W, b, lr) snapshots for TracIn.
    checkpoints: list[tuple[np.ndarray, np.ndarray, float]] = field(default_factory=list)
    loss_curve: list[float] = field(default_factory=list)

    def logits(self, X: np.ndarray) -> np.ndarray:
        return ((X - self.mean) / self.std) @ self.W + self.b

    def standardize(self, X: np.ndarray) -> np.ndarray:
        return (X - self.mean) / self.std


def softmax(z: np.ndarray) -> np.ndarray:
    z = z - z.max(axis=1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=1, keepdims=True)


def train_softmax(
    X: np.ndarray,
    y: np.ndarray,
    n_classes: int,
    cfg: TrainConfig,
    *,
    record_dynamics: bool = False,
    record_checkpoints: bool = False,
    sample_weight: np.ndarray | None = None,
) -> TrainResult:
    rng = np.random.default_rng(cfg.seed)
    X = X.astype(np.float64)
    n, d = X.shape
    mean = X.mean(axis=0)
    std = X.std(axis=0) + 1e-6
    Xs = (X - mean) / std
    W = np.zeros((d, n_classes))
    b = np.zeros(n_classes)
    vW = np.zeros_like(W)
    vb = np.zeros_like(b)
    Y = np.eye(n_classes)[y]
    if cfg.class_balanced:
        counts = np.bincount(y, minlength=n_classes).astype(np.float64)
        cw = np.where(counts > 0, n / (n_classes * np.maximum(counts, 1)), 0.0)
        w = cw[y]
    else:
        w = np.ones(n)
    if sample_weight is not None:
        w = w * sample_weight
    epoch_p = np.zeros((cfg.epochs, n)) if record_dynamics else None
    epoch_c = np.zeros((cfg.epochs, n), dtype=bool) if record_dynamics else None
    res = TrainResult(W=W, b=b, mean=mean, std=std)
    total_steps = cfg.epochs * max(1, int(np.ceil(n / cfg.batch_size)))
    step = 0
    for ep in range(cfg.epochs):
        order = rng.permutation(n)
        for s in range(0, n, cfg.batch_size):
            bi = order[s : s + cfg.batch_size]
            lr = cfg.lr * 0.5 * (1 + np.cos(np.pi * step / total_steps))
            P = softmax(Xs[bi] @ W + b)
            R = (P - Y[bi]) * w[bi, None]
            gW = Xs[bi].T @ R / len(bi) + cfg.weight_decay * W
            gb = R.mean(axis=0)
            vW = cfg.momentum * vW + gW
            vb = cfg.momentum * vb + gb
            W = W - lr * vW
            b = b - lr * vb
            step += 1
        P_all = softmax(Xs @ W + b)
        loss = float(-(np.log(P_all[np.arange(n), y] + 1e-12) * w).sum() / w.sum())
        res.loss_curve.append(round(loss, 5))
        if record_dynamics:
            epoch_p[ep] = P_all[np.arange(n), y]
            epoch_c[ep] = P_all.argmax(axis=1) == y
        if record_checkpoints and ((ep + 1) % cfg.checkpoint_every == 0 or ep == cfg.epochs - 1):
            ep_lr = cfg.lr * 0.5 * (1 + np.cos(np.pi * (ep + 0.5) / cfg.epochs))
            res.checkpoints.append((W.copy(), b.copy(), float(ep_lr * cfg.checkpoint_every)))
    res.W, res.b = W, b
    res.epoch_p_given, res.epoch_correct = epoch_p, epoch_c
    return res


@dataclass
class LinearModel:
    """Converged L2-regularized multinomial logistic regression (deterministic, seed-free)."""

    W: np.ndarray
    b: np.ndarray
    mean: np.ndarray
    std: np.ndarray
    iterations: int = 0
    converged: bool = True

    def logits(self, X: np.ndarray) -> np.ndarray:
        return ((X - self.mean) / self.std) @ self.W + self.b


def fit_logreg(
    X: np.ndarray,
    y: np.ndarray,
    n_classes: int,
    *,
    C: float = 0.001,
    class_balanced: bool = True,
    sample_weight: np.ndarray | None = None,
    max_iter: int = 500,
) -> LinearModel:
    """Minimizes sum_i w_i CE(x_i, y_i) + ||W||^2 / (2C) with L-BFGS (scikit-learn's objective).

    Convex, so the solution does not depend on initialization or data order: before/after
    comparisons reflect the data change, not optimizer noise.
    """
    from scipy.optimize import minimize

    X = X.astype(np.float64)
    n, d = X.shape
    mean = X.mean(axis=0)
    std = X.std(axis=0) + 1e-6
    Xs = (X - mean) / std
    Y = np.eye(n_classes)[y]
    w = np.ones(n)
    if class_balanced:
        counts = np.bincount(y, minlength=n_classes).astype(np.float64)
        cw = np.where(counts > 0, n / (n_classes * np.maximum(counts, 1)), 0.0)
        w = cw[y]
    if sample_weight is not None:
        w = w * sample_weight
    lam = 1.0 / C

    def f(theta: np.ndarray) -> tuple[float, np.ndarray]:
        W = theta[: d * n_classes].reshape(d, n_classes)
        b = theta[d * n_classes :]
        Z = Xs @ W + b
        Z = Z - Z.max(axis=1, keepdims=True)
        logsum = np.log(np.exp(Z).sum(axis=1))
        loss = float((w * (logsum - (Z * Y).sum(axis=1))).sum() + 0.5 * lam * (W * W).sum())
        P = np.exp(Z - logsum[:, None])
        R = (P - Y) * w[:, None]
        gW = Xs.T @ R + lam * W
        return loss, np.concatenate([gW.ravel(), R.sum(axis=0)])

    theta0 = np.zeros(d * n_classes + n_classes)
    res = minimize(f, theta0, jac=True, method="L-BFGS-B", options={"maxiter": max_iter, "gtol": 1e-5})
    W = res.x[: d * n_classes].reshape(d, n_classes)
    b = res.x[d * n_classes :]
    return LinearModel(W=W, b=b, mean=mean, std=std, iterations=int(res.nit), converged=bool(res.success))


def out_of_fold_logits_lr(
    X: np.ndarray, y: np.ndarray, n_classes: int, *, C: float, class_balanced: bool, k: int, seed: int
) -> tuple[np.ndarray, np.ndarray]:
    """Out-of-fold logits from the converged logistic head (stratified K-fold, fixed split seed)."""
    n = len(y)
    counts = np.bincount(y, minlength=n_classes)
    present = counts[counts > 0]
    k_eff = int(max(2, min(k, present.min() if len(present) else 2)))
    logits = np.zeros((n, n_classes))
    fold_id = np.full(n, -1)
    if present.min() < 2:
        rng = np.random.default_rng(seed)
        folds = rng.integers(0, k_eff, n)
        splits = [(np.nonzero(folds != f)[0], np.nonzero(folds == f)[0]) for f in range(k_eff)]
    else:
        splits = list(StratifiedKFold(n_splits=k_eff, shuffle=True, random_state=seed).split(X, y))
    for f, (tr, te) in enumerate(splits):
        if len(te) == 0 or len(tr) == 0:
            continue
        mdl = fit_logreg(X[tr], y[tr], n_classes, C=C, class_balanced=class_balanced)
        logits[te] = mdl.logits(X[te])
        fold_id[te] = f
    return logits, fold_id


def out_of_fold_logits(
    X: np.ndarray, y: np.ndarray, n_classes: int, cfg: TrainConfig, k: int
) -> tuple[np.ndarray, np.ndarray]:
    """Returns (oof_logits, fold_id). Classes with < k samples fall back to fewer folds."""
    n = len(y)
    counts = np.bincount(y, minlength=n_classes)
    present = counts[counts > 0]
    k_eff = int(max(2, min(k, present.min() if len(present) else 2)))
    logits = np.zeros((n, n_classes))
    fold_id = np.full(n, -1)
    if present.min() < 2:
        # Too few samples for stratification: leave-one-out-style fallback on a random split.
        rng = np.random.default_rng(cfg.seed)
        folds = rng.integers(0, k_eff, n)
        splits = [(np.nonzero(folds != f)[0], np.nonzero(folds == f)[0]) for f in range(k_eff)]
    else:
        skf = StratifiedKFold(n_splits=k_eff, shuffle=True, random_state=cfg.seed)
        splits = list(skf.split(X, y))
    for f, (tr, te) in enumerate(splits):
        if len(te) == 0 or len(tr) == 0:
            continue
        m = train_softmax(X[tr], y[tr], n_classes, cfg)
        logits[te] = m.logits(X[te])
        fold_id[te] = f
    return logits, fold_id


def fit_temperature(logits: np.ndarray, y: np.ndarray) -> float:
    def nll(t: float) -> float:
        p = softmax(logits / t)
        return float(-np.log(p[np.arange(len(y)), y] + 1e-12).mean())

    if len(y) < 20:
        return 1.0
    res = minimize_scalar(nll, bounds=(0.05, 20.0), method="bounded")
    return float(res.x) if res.success else 1.0


def expected_calibration_error(probs: np.ndarray, y: np.ndarray, bins: int = 15) -> float:
    conf = probs.max(axis=1)
    pred = probs.argmax(axis=1)
    edges = np.linspace(0, 1, bins + 1)
    ece = 0.0
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        mask = (conf > lo) & (conf <= hi)
        if mask.any():
            ece += mask.mean() * abs((pred[mask] == y[mask]).mean() - conf[mask].mean())
    return float(ece)


def classification_metrics(y_true: np.ndarray, probs: np.ndarray, class_names: list[str]) -> dict:
    n_classes = len(class_names)
    if len(y_true) == 0:
        return {"n": 0}
    pred = probs.argmax(axis=1)
    labels = list(range(n_classes))
    p, r, f, s = precision_recall_fscore_support(y_true, pred, labels=labels, zero_division=0)
    present = [c for c in labels if (y_true == c).any()]
    return {
        "n": int(len(y_true)),
        "accuracy": round(float((pred == y_true).mean()), 5),
        "macro_f1": round(float(f1_score(y_true, pred, labels=present, average="macro", zero_division=0)), 5),
        "balanced_accuracy": round(float(balanced_accuracy_score(y_true, pred)), 5),
        "ece": round(expected_calibration_error(probs, y_true), 5),
        "nll": round(float(-np.log(probs[np.arange(len(y_true)), y_true] + 1e-12).mean()), 5),
        "per_class": {
            class_names[c]: {
                "precision": round(float(p[c]), 4),
                "recall": round(float(r[c]), 4),
                "f1": round(float(f[c]), 4),
                "support": int(s[c]),
            }
            for c in labels
        },
        "confusion_matrix": confusion_matrix(y_true, pred, labels=labels).tolist(),
        "class_names": class_names,
    }


def prediction_rows(probs: np.ndarray, y: np.ndarray) -> dict[str, np.ndarray]:
    n = len(y)
    order = np.argsort(-probs, axis=1)
    pred = order[:, 0]
    p_pred = probs[np.arange(n), pred]
    p_given = probs[np.arange(n), y]
    masked = probs.copy()
    masked[np.arange(n), y] = -1.0
    best_other = masked.max(axis=1) if probs.shape[1] > 1 else np.zeros(n)
    margin = p_given - best_other  # >0 means the given label wins
    top2 = probs[np.arange(n), order[:, 1]] if probs.shape[1] > 1 else np.zeros(n)
    ent = -(probs * np.log(probs + 1e-12)).sum(axis=1)
    return {
        "pred": pred,
        "p_pred": p_pred,
        "p_given": p_given,
        "margin": margin,
        "top12_gap": p_pred - top2,
        "entropy": ent,
        "order": order,
    }
