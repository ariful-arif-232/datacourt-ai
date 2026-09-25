"""Registry of versioned algorithms and embedding backends (served at GET /api/v1/algorithms).

Versions come from `datacourt.algorithms`, the same constants the implementing modules use, so
the registry cannot drift from the code and stays import-light for the API (no ML stack).
`kind` follows the UI's evidence taxonomy: measured / heuristic / model_prediction / model_estimate.
"""

from __future__ import annotations

from datacourt import algorithms as A

ALGORITHMS: list[dict] = [
    {
        "area": "Ingestion",
        "id": A.IMAGE_HASH,
        "kind": "measured",
        "summary": "SHA-256 file identity plus 64-bit DCT perceptual hash (and dHash) per image.",
    },
    {
        "area": "Ingestion",
        "id": A.PIXEL_HASH,
        "kind": "measured",
        "summary": "SHA-256 of the decoded picture (rotation applied, 8-bit RGB): the same value for files "
        "that hold identical pixels in different formats, such as a HEIC and a lossless PNG of it.",
    },
    {
        "area": "Quality",
        "id": A.ATTRIBUTES,
        "kind": "measured",
        "summary": "Brightness, contrast, normalised sharpness, entropy, saturation, border and aspect features.",
    },
    {
        "area": "Quality",
        "id": A.QUALITY_RULES,
        "kind": "heuristic",
        "summary": "Absolute thresholds combined with class-relative robust z-scores for blur, exposure, "
        "contrast, near-empty and resolution outliers.",
    },
    {
        "area": "Duplicates",
        "id": A.DUPLICATES,
        "kind": "measured",
        "summary": "Candidates from SHA-256, decoded-pixel hash, pHash (incl. mirrored) and embedding kNN, "
        "verified by detail-layer NCC or ORB+RANSAC with warped-overlap NCC for crops; union-find "
        "families; Prim lineage tree.",
    },
    {
        "area": "Leakage",
        "id": A.LEAKAGE,
        "kind": "measured",
        "summary": "Families that cross train/validation/test splits, graded by transformation, plus "
        "adaptive-threshold similar-subject clusters.",
    },
    {
        "area": "Baseline",
        "id": A.BASELINE,
        "kind": "model_prediction",
        "summary": "Class-balanced L2 multinomial logistic regression on frozen embeddings; K-fold out-of-fold "
        "predictions; temperature scaling and ECE.",
    },
    {
        "area": "Training dynamics",
        "id": A.DYNAMICS,
        "kind": "model_estimate",
        "summary": "Dataset-cartography confidence, variability and correctness across SGD epochs.",
    },
    {
        "area": "Influence",
        "id": A.INFLUENCE,
        "kind": "model_estimate",
        "summary": "TracIn (checkpoint gradient dot products) on the linear head — an approximation, not a "
        "causal proof.",
    },
    {
        "area": "Labels",
        "id": A.LABELS,
        "kind": "heuristic",
        "summary": "Reliability-weighted evidence from out-of-fold disagreement, neighbour votes, margin and "
        "training dynamics; witness weights are chance-corrected balanced accuracies.",
    },
    {
        "area": "Labels",
        "id": A.RARE_OR_WRONG,
        "kind": "heuristic",
        "summary": "Competing hypothesis scores: valid rare example vs mislabel vs corrupt vs out-of-distribution.",
    },
    {
        "area": "Shortcuts",
        "id": A.SHORTCUTS,
        "kind": "measured",
        "summary": "Cramér's V and lift between classes and cues, cue-only cross-validated accuracy, "
        "border-only / content-only perturbation tests.",
    },
    {
        "area": "Coverage",
        "id": A.COVERAGE,
        "kind": "measured",
        "summary": "2-D projection, K-means regions, class/condition gaps and an Active Collection Planner.",
    },
    {
        "area": "Privacy",
        "id": A.PRIVACY,
        "kind": "heuristic",
        "summary": "OpenCV Haar face detector and MSER text-region detector. No identity recognition.",
    },
    {
        "area": "Court",
        "id": A.JURY,
        "kind": "heuristic",
        "summary": "Ordered deterministic rules (R1–R10) over witness evidence with reason codes and a rule "
        "trace. No LLM involvement in verdicts.",
    },
    {
        "area": "Review",
        "id": A.REVIEW_BUDGET,
        "kind": "heuristic",
        "summary": "Greedy review-budget selection with diminishing returns per family and class.",
    },
    {
        "area": "Review",
        "id": A.COUNTERFACTUAL,
        "kind": "model_estimate",
        "summary": "Nearest opposite-verdict neighbour and minimal evidence change that would flip the verdict.",
    },
    {
        "area": "Experiments",
        "id": A.WHAT_IF,
        "kind": "model_estimate",
        "summary": "Retrain the baseline head on a modified training set; paired Poisson training bootstrap and "
        "paired evaluation bootstrap confidence intervals.",
    },
    {
        "area": "Governance",
        "id": A.DEBT,
        "kind": "heuristic",
        "summary": "Dataset Debt dimensions with published formulas and thresholds.",
    },
    {
        "area": "Governance",
        "id": A.PREFLIGHT,
        "kind": "heuristic",
        "summary": "Blocking / warning rules answering whether the dataset is ready to train on.",
    },
    {
        "area": "Governance",
        "id": A.CONTRACT,
        "kind": "measured",
        "summary": "Declarative data-contract checks evaluated in the UI and via the CI endpoint.",
    },
    {
        "area": "Versions",
        "id": A.DNA,
        "kind": "measured",
        "summary": "Descriptive dataset fingerprint; Jensen–Shannon divergence drift between versions.",
    },
    {
        "area": "Versions",
        "id": A.VERSION_DIFF,
        "kind": "measured",
        "summary": "File, label, split, finding, debt and metric differences between two versions.",
    },
    {
        "area": "Versions",
        "id": A.CONTAMINATION,
        "kind": "measured",
        "summary": "Exact and verified near-duplicate overlap with a reference version in the same workspace.",
    },
    {
        "area": "Export",
        "id": A.EXPORT,
        "kind": "measured",
        "summary": "Deterministic clean export from human decisions with manifest, action log and SHA256SUMS.",
    },
    {
        "area": "Retention",
        "id": A.RETENTION,
        "kind": "measured",
        "summary": "Time-based purge of redundant archives (uploads after ingestion, exports, report files).",
    },
    {
        "area": "Configuration",
        "id": A.AUDIT_CONFIG,
        "kind": "measured",
        "summary": "Versioned default audit configuration; per-project overrides are stored with every audit.",
    },
]

EMBEDDING_BACKENDS: list[dict] = [
    {
        "id": "dc-descriptor-v1",
        "default": True,
        "dim": 2588,
        "source": "built-in (OpenCV/NumPy), CPU-only, deterministic",
        "summary": "HSV colour histogram, Lab spatial layout, coarse and fine HOG, LBP texture and a tiny "
        "image thumbnail, L2-normalised per block. Fast and dependency-free; weaker semantics than "
        "self-supervised models.",
    },
    {
        "id": "dinov2-small",
        "default": False,
        "dim": 768,
        "source": "https://huggingface.co/facebook/dinov2-small (optional 'deep' extra)",
        "summary": "Self-supervised ViT-S/14; CLS token concatenated with mean patch token. Pin the model "
        "revision with DINOV2_REVISION. Falls back to the descriptor if unavailable.",
    },
]
