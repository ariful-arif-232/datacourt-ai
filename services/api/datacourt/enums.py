"""Domain enumerations. Stored as constrained strings in the database."""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    REVIEWER = "reviewer"
    VIEWER = "viewer"


ROLE_RANK = {Role.VIEWER: 0, Role.REVIEWER: 1, Role.ADMIN: 2, Role.OWNER: 3}


class Split(StrEnum):
    TRAIN = "train"
    VAL = "val"
    TEST = "test"
    UNSPLIT = "unsplit"


EVAL_SPLITS = (Split.VAL, Split.TEST)


class VersionStatus(StrEnum):
    AWAITING_UPLOAD = "awaiting_upload"
    UPLOADED = "uploaded"
    INGESTING = "ingesting"
    READY = "ready"
    FAILED = "failed"


class FileStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    IGNORED = "ignored"


class AuditProfile(StrEnum):
    FAST = "fast"
    DEEP = "deep"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class Stage(StrEnum):
    INGESTING = "INGESTING"
    VALIDATING = "VALIDATING"
    PROFILING = "PROFILING"
    QUALITY_ANALYSIS = "QUALITY_ANALYSIS"
    EMBEDDING = "EMBEDDING"
    DUPLICATE_ANALYSIS = "DUPLICATE_ANALYSIS"
    LEAKAGE_ANALYSIS = "LEAKAGE_ANALYSIS"
    BASELINE_TRAINING = "BASELINE_TRAINING"
    LABEL_FORENSICS = "LABEL_FORENSICS"
    SHORTCUT_ANALYSIS = "SHORTCUT_ANALYSIS"
    COVERAGE_ANALYSIS = "COVERAGE_ANALYSIS"
    TRAINING_DYNAMICS = "TRAINING_DYNAMICS"
    INFLUENCE_ANALYSIS = "INFLUENCE_ANALYSIS"
    COURT_CASE_GENERATION = "COURT_CASE_GENERATION"
    DEBT_CALCULATION = "DEBT_CALCULATION"
    PREFLIGHT = "PREFLIGHT"
    COMPLETE = "COMPLETE"


class Severity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Risk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class LeakageKind(StrEnum):
    EXACT_CROSS_SPLIT = "exact_cross_split"
    TRANSFORMED_CROSS_SPLIT = "transformed_cross_split"
    NEAR_CROSS_SPLIT = "near_cross_split"
    SIMILAR_SUBJECT_CLUSTER = "similar_subject_cluster"


class Relation(StrEnum):
    EXACT = "exact"
    RESIZED = "resized"
    FLIPPED = "flipped"
    CROPPED = "possible_crop"
    PHOTOMETRIC = "photometric"
    NEAR = "near_duplicate"


class LabelAction(StrEnum):
    REVIEW = "REVIEW"
    LOW_PRIORITY_REVIEW = "LOW_PRIORITY_REVIEW"
    LIKELY_RARE = "LIKELY_RARE"
    NO_ACTION = "NO_ACTION"


class Hypothesis(StrEnum):
    LIKELY_MISLABELED = "likely_mislabeled"
    RARE_VALID = "rare_valid"
    POOR_QUALITY = "poor_quality"
    DOMAIN_SHIFTED = "domain_shifted"
    AMBIGUOUS = "ambiguous"
    UNCERTAIN = "uncertain"


class DynamicsCategory(StrEnum):
    EASY = "easy"
    AMBIGUOUS = "ambiguous"
    HARD = "consistently_hard"
    FORGOTTEN = "forgotten"
    UNSTABLE = "unstable"


class Verdict(StrEnum):
    KEEP = "KEEP"
    REVIEW = "REVIEW"
    STRONG_REVIEW = "STRONG_REVIEW"
    POSSIBLE_RELABEL = "POSSIBLE_RELABEL"
    POSSIBLE_REMOVE = "POSSIBLE_REMOVE"
    LIKELY_RARE = "LIKELY_RARE"
    LEAKAGE_ACTION_NEEDED = "LEAKAGE_ACTION_NEEDED"
    UNCERTAIN = "UNCERTAIN"


class Uncertainty(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class CaseStatus(StrEnum):
    OPEN = "open"
    DECIDED = "decided"
    DISPUTED = "disputed"
    RESOLVED = "resolved"


class Witness(StrEnum):
    QUALITY = "quality"
    EMBEDDING = "embedding"
    NEIGHBOR = "neighbor"
    MODEL = "model"
    SPLIT = "split"
    DUPLICATE = "duplicate"
    DYNAMICS = "training_dynamics"
    INFLUENCE = "influence"
    SHORTCUT = "shortcut"
    PRIVACY = "privacy"


class Stance(StrEnum):
    PROSECUTION = "prosecution"
    DEFENSE = "defense"
    NEUTRAL = "neutral"


class EvidenceKind(StrEnum):
    MEASURED = "measured"
    HEURISTIC = "heuristic"
    MODEL_PREDICTION = "model_prediction"
    MODEL_ESTIMATE = "model_estimate"


class DecisionAction(StrEnum):
    KEEP = "keep"
    RELABEL = "relabel"
    REMOVE = "remove"
    UNSURE = "unsure"
    ESCALATE = "escalate"


class JobType(StrEnum):
    INGEST_VERSION = "ingest_version"
    RUN_AUDIT = "run_audit"
    WHAT_IF = "what_if"
    EXPORT = "export"
    REPORT = "report"
    VERSION_DIFF = "version_diff"
    CONTAMINATION = "contamination"
    PURGE_PREFIX = "purge_prefix"


class PreflightStatus(StrEnum):
    READY = "READY"
    READY_WITH_WARNINGS = "READY_WITH_WARNINGS"
    BLOCKED = "BLOCKED"


class DebtLevel(StrEnum):
    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ExplanationSource(StrEnum):
    GEMINI = "gemini"
    TEMPLATE = "template"


class LinkType(StrEnum):
    NEAREST_NEIGHBOR = "nearest_neighbor"
    HARMFUL_INFLUENCE = "harmful_influence"
    HELPFUL_INFLUENCE = "helpful_influence"
    DUPLICATE = "duplicate"


class CollectionStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    DISMISSED = "dismissed"
