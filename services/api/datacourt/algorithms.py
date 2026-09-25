"""Version identifiers of every DataCourt algorithm, in one import-light module.

Each implementing module takes its version from here (`VERSION = algorithms.X`), and the public
registry (`datacourt.registry`) reads the same constants, so the API can describe algorithms
without importing the numerical stack (OpenCV, scikit-learn, SciPy, FAISS) that only the
worker installs. Changing an algorithm's output means bumping its version here.
"""

from __future__ import annotations

AUDIT_CONFIG = "audit-config-v1"
IMAGE_HASH = "phash-dct32-v1"
PIXEL_HASH = "pixel-sha256-rgb8-v1"
ATTRIBUTES = "attributes-v1"
QUALITY_RULES = "quality-rules-v1"
DUPLICATES = "dup-families-v2"
LEAKAGE = "leakage-v1"
BASELINE = "baseline-logreg-v2"
DYNAMICS = "cartography-v1"
INFLUENCE = "tracin-linear-v1"
LABELS = "label-forensics-v1"
RARE_OR_WRONG = "rare-or-wrong-v1"
SHORTCUTS = "shortcut-detective-v1"
COVERAGE = "coverage-v1"
PRIVACY = "privacy-scan-v1"
JURY = "jury-v1"
REVIEW_BUDGET = "review-budget-v1"
COUNTERFACTUAL = "counterfactual-proxy-v1"
WHAT_IF = "what-if-v1"
DEBT = "debt-v1"
PREFLIGHT = "preflight-v1"
CONTRACT = "contract-v1"
DNA = "dna-v1"
VERSION_DIFF = "version-diff-v1"
CONTAMINATION = "contamination-v2"
EXPORT = "export-v1"
RETENTION = "retention-v1"
