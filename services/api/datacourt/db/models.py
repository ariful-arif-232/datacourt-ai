"""Relational schema.

Conventions
- UUID primary keys (non-enumerable, safe to expose in URLs).
- Tenant-owned rows carry `org_id` (directly or through a single parent) and every API
  read goes through `datacourt.tenancy` which verifies membership.
- Enum columns are VARCHAR with CHECK constraints (portable, migration friendly).
- Heavy numeric arrays (embeddings, projections, probability matrices) live in object
  storage as versioned artifacts; the database stores metadata and decision-relevant rows.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from datacourt import enums as E

JSONType = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(UTC)


def _enum(cls: type[StrEnum], name: str) -> Enum:
    return Enum(
        cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=40,
        values_callable=lambda c: [m.value for m in c],
        validate_strings=True,
    )


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONType, list[Any]: JSONType}


def pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


def fk(target: str, *, nullable: bool = False, ondelete: str = "CASCADE", index: bool = True) -> Any:
    return mapped_column(Uuid, ForeignKey(target, ondelete=ondelete), nullable=nullable, index=index)


def created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


def jcol(default: Any = None, nullable: bool = True) -> Any:
    if default is None:
        return mapped_column(JSONType, nullable=nullable)
    return mapped_column(JSONType, default=default, nullable=nullable)


# ---------------------------------------------------------------------------
# Identity & tenancy
# ---------------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = pk()
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = created()
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID] = fk("users.id")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = created()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str | None] = mapped_column(String(200))


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[uuid.UUID] = pk()
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    settings: Mapped[dict[str, Any]] = jcol(default=dict, nullable=False)
    retention_days: Mapped[int | None] = mapped_column(Integer)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = created()


class OrganizationMember(Base):
    __tablename__ = "organization_members"
    __table_args__ = (UniqueConstraint("org_id", "user_id"),)
    id: Mapped[uuid.UUID] = pk()
    org_id: Mapped[uuid.UUID] = fk("organizations.id")
    user_id: Mapped[uuid.UUID] = fk("users.id")
    role: Mapped[E.Role] = mapped_column(_enum(E.Role, "member_role"), nullable=False)
    created_at: Mapped[datetime] = created()


class ApiToken(Base):
    __tablename__ = "api_tokens"
    id: Mapped[uuid.UUID] = pk()
    org_id: Mapped[uuid.UUID] = fk("organizations.id")
    created_by: Mapped[uuid.UUID | None] = fk("users.id", nullable=True, ondelete="SET NULL")
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    scopes: Mapped[list[Any]] = jcol(default=list, nullable=False)
    created_at: Mapped[datetime] = created()
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("org_id", "name"),)
    id: Mapped[uuid.UUID] = pk()
    org_id: Mapped[uuid.UUID] = fk("organizations.id")
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    task_type: Mapped[str] = mapped_column(String(40), default="image_classification", nullable=False)
    created_by: Mapped[uuid.UUID | None] = fk("users.id", nullable=True, ondelete="SET NULL")
    created_at: Mapped[datetime] = created()


class AuditConfig(Base):
    """Named, versioned threshold configuration for a project."""

    __tablename__ = "audit_configs"
    __table_args__ = (UniqueConstraint("project_id", "version"),)
    id: Mapped[uuid.UUID] = pk()
    project_id: Mapped[uuid.UUID] = fk("projects.id")
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    config: Mapped[dict[str, Any]] = jcol(nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[uuid.UUID | None] = fk("users.id", nullable=True, ondelete="SET NULL")
    created_at: Mapped[datetime] = created()


class DataContract(Base):
    __tablename__ = "data_contracts"
    __table_args__ = (UniqueConstraint("project_id", "version"),)
    id: Mapped[uuid.UUID] = pk()
    project_id: Mapped[uuid.UUID] = fk("projects.id")
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(120), default="Default contract", nullable=False)
    rules: Mapped[list[Any]] = jcol(nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[uuid.UUID | None] = fk("users.id", nullable=True, ondelete="SET NULL")
    created_at: Mapped[datetime] = created()


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------


class Dataset(Base):
    __tablename__ = "datasets"
    __table_args__ = (UniqueConstraint("project_id", "name"),)
    id: Mapped[uuid.UUID] = pk()
    org_id: Mapped[uuid.UUID] = fk("organizations.id")
    project_id: Mapped[uuid.UUID] = fk("projects.id")
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    modality: Mapped[str] = mapped_column(String(40), default="image_classification", nullable=False)
    provenance: Mapped[dict[str, Any]] = jcol(default=dict, nullable=False)
    created_by: Mapped[uuid.UUID | None] = fk("users.id", nullable=True, ondelete="SET NULL")
    created_at: Mapped[datetime] = created()


class DatasetVersion(Base):
    __tablename__ = "dataset_versions"
    __table_args__ = (UniqueConstraint("dataset_id", "version_number"),)
    id: Mapped[uuid.UUID] = pk()
    org_id: Mapped[uuid.UUID] = fk("organizations.id")
    dataset_id: Mapped[uuid.UUID] = fk("datasets.id")
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_version_id: Mapped[uuid.UUID | None] = fk(
        "dataset_versions.id", nullable=True, ondelete="SET NULL"
    )
    origin: Mapped[str] = mapped_column(String(20), default="upload", nullable=False)
    status: Mapped[E.VersionStatus] = mapped_column(_enum(E.VersionStatus, "version_status"), nullable=False)
    source_filename: Mapped[str | None] = mapped_column(String(255))
    source_object_key: Mapped[str | None] = mapped_column(String(512))
    source_sha256: Mapped[str | None] = mapped_column(String(64))
    source_bytes: Mapped[int | None] = mapped_column(BigInteger)
    manifest_object_key: Mapped[str | None] = mapped_column(String(512))
    manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    layout: Mapped[dict[str, Any]] = jcol(default=dict, nullable=False)
    stats: Mapped[dict[str, Any]] = jcol(default=dict, nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # In-progress presigned multipart upload of the source archive (object-store upload id).
    upload_id: Mapped[str | None] = mapped_column(String(300))
    upload_meta: Mapped[dict[str, Any]] = jcol(default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = fk("users.id", nullable=True, ondelete="SET NULL")
    created_at: Mapped[datetime] = created()
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DatasetFile(Base):
    __tablename__ = "dataset_files"
    __table_args__ = (UniqueConstraint("dataset_version_id", "relative_path"),)
    id: Mapped[uuid.UUID] = pk()
    dataset_version_id: Mapped[uuid.UUID] = fk("dataset_versions.id")
    relative_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[E.FileStatus] = mapped_column(_enum(E.FileStatus, "file_status"), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255))
    sha256: Mapped[str | None] = mapped_column(String(64))


class DatasetClass(Base):
    __tablename__ = "classes"
    __table_args__ = (
        UniqueConstraint("dataset_version_id", "name"),
        UniqueConstraint("dataset_version_id", "index"),
    )
    id: Mapped[uuid.UUID] = pk()
    dataset_version_id: Mapped[uuid.UUID] = fk("dataset_versions.id")
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    index: Mapped[int] = mapped_column(Integer, nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class DatasetSplit(Base):
    __tablename__ = "splits"
    __table_args__ = (UniqueConstraint("dataset_version_id", "name"),)
    id: Mapped[uuid.UUID] = pk()
    dataset_version_id: Mapped[uuid.UUID] = fk("dataset_versions.id")
    name: Mapped[E.Split] = mapped_column(_enum(E.Split, "split_name"), nullable=False)
    source_folder: Mapped[str | None] = mapped_column(String(255))
    sample_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Sample(Base):
    __tablename__ = "samples"
    __table_args__ = (
        UniqueConstraint("dataset_version_id", "relative_path"),
        UniqueConstraint("dataset_version_id", "idx"),
        Index("ix_samples_version_split", "dataset_version_id", "split"),
        Index("ix_samples_version_class", "dataset_version_id", "class_id"),
        Index("ix_samples_version_sha", "dataset_version_id", "sha256"),
    )
    id: Mapped[uuid.UUID] = pk()
    dataset_version_id: Mapped[uuid.UUID] = fk("dataset_versions.id", index=False)
    file_id: Mapped[uuid.UUID | None] = fk("dataset_files.id", nullable=True, ondelete="SET NULL")
    idx: Mapped[int] = mapped_column(Integer, nullable=False)
    relative_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    split: Mapped[E.Split] = mapped_column(_enum(E.Split, "sample_split"), nullable=False)
    class_id: Mapped[uuid.UUID] = fk("classes.id", index=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    phash: Mapped[str] = mapped_column(String(16), nullable=False)
    phash_flip: Mapped[str] = mapped_column(String(16), nullable=False)
    dhash: Mapped[str] = mapped_column(String(16), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    format: Mapped[str] = mapped_column(String(16), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    thumb_key: Mapped[str | None] = mapped_column(String(512))
    attributes: Mapped[dict[str, Any]] = jcol(default=dict, nullable=False)
    provenance: Mapped[dict[str, Any] | None] = jcol()


class AlgorithmVersion(Base):
    __tablename__ = "algorithm_versions"
    __table_args__ = (UniqueConstraint("name", "version"),)
    id: Mapped[uuid.UUID] = pk()
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = created()


class SystemModelVersion(Base):
    __tablename__ = "system_model_versions"
    __table_args__ = (UniqueConstraint("name", "version"),)
    id: Mapped[uuid.UUID] = pk()
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    version: Mapped[str] = mapped_column(String(80), nullable=False)
    source: Mapped[str] = mapped_column(String(255), nullable=False)
    dim: Mapped[int] = mapped_column(Integer, nullable=False)
    preprocessing_version: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = created()


# ---------------------------------------------------------------------------
# Audits
# ---------------------------------------------------------------------------


class AuditRun(Base):
    __tablename__ = "audit_runs"
    id: Mapped[uuid.UUID] = pk()
    org_id: Mapped[uuid.UUID] = fk("organizations.id")
    dataset_version_id: Mapped[uuid.UUID] = fk("dataset_versions.id")
    profile: Mapped[E.AuditProfile] = mapped_column(_enum(E.AuditProfile, "audit_profile"), nullable=False)
    status: Mapped[E.RunStatus] = mapped_column(_enum(E.RunStatus, "audit_status"), nullable=False)
    config: Mapped[dict[str, Any]] = jcol(nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    algorithm_versions: Mapped[dict[str, Any]] = jcol(default=dict, nullable=False)
    embedding_model: Mapped[str | None] = mapped_column(String(80))
    progress: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    current_stage: Mapped[str | None] = mapped_column(String(40))
    warnings: Mapped[list[Any]] = jcol(default=list, nullable=False)
    summary: Mapped[dict[str, Any]] = jcol(default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_by: Mapped[uuid.UUID | None] = fk("users.id", nullable=True, ondelete="SET NULL")
    created_at: Mapped[datetime] = created()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditPipelineStep(Base):
    __tablename__ = "audit_pipeline_steps"
    __table_args__ = (UniqueConstraint("audit_run_id", "stage"),)
    id: Mapped[uuid.UUID] = pk()
    audit_run_id: Mapped[uuid.UUID] = fk("audit_runs.id")
    stage: Mapped[str] = mapped_column(String(40), nullable=False)
    order: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[E.StepStatus] = mapped_column(_enum(E.StepStatus, "step_status"), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    output: Mapped[dict[str, Any]] = jcol(default=dict, nullable=False)
    warnings: Mapped[list[Any]] = jcol(default=list, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)


class SampleQualityMetrics(Base):
    __tablename__ = "sample_quality_metrics"
    __table_args__ = (UniqueConstraint("audit_run_id", "sample_id"),)
    id: Mapped[uuid.UUID] = pk()
    audit_run_id: Mapped[uuid.UUID] = fk("audit_runs.id")
    sample_id: Mapped[uuid.UUID] = fk("samples.id")
    metrics: Mapped[dict[str, Any]] = jcol(nullable=False)


class QualityFinding(Base):
    __tablename__ = "quality_findings"
    __table_args__ = (Index("ix_quality_findings_audit_type", "audit_run_id", "finding_type"),)
    id: Mapped[uuid.UUID] = pk()
    audit_run_id: Mapped[uuid.UUID] = fk("audit_runs.id", index=False)
    sample_id: Mapped[uuid.UUID] = fk("samples.id")
    finding_type: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[E.Severity] = mapped_column(_enum(E.Severity, "severity"), nullable=False)
    measured_value: Mapped[float | None] = mapped_column(Float)
    threshold: Mapped[float | None] = mapped_column(Float)
    comparator: Mapped[str] = mapped_column(String(8), nullable=False, default="<")
    rule_version: Mapped[str] = mapped_column(String(40), nullable=False)
    deterministic: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)


class EmbeddingMetadata(Base):
    __tablename__ = "embeddings_metadata"
    id: Mapped[uuid.UUID] = pk()
    audit_run_id: Mapped[uuid.UUID] = fk("audit_runs.id")
    dataset_version_id: Mapped[uuid.UUID] = fk("dataset_versions.id")
    model_name: Mapped[str] = mapped_column(String(80), nullable=False)
    model_version: Mapped[str] = mapped_column(String(80), nullable=False)
    preprocessing_version: Mapped[str] = mapped_column(String(40), nullable=False)
    dim: Mapped[int] = mapped_column(Integer, nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = created()


class SimilarityEdge(Base):
    __tablename__ = "similarity_edges"
    id: Mapped[uuid.UUID] = pk()
    audit_run_id: Mapped[uuid.UUID] = fk("audit_runs.id")
    sample_a_id: Mapped[uuid.UUID] = fk("samples.id", index=False)
    sample_b_id: Mapped[uuid.UUID] = fk("samples.id", index=False)
    cosine: Mapped[float] = mapped_column(Float, nullable=False)
    phash_distance: Mapped[int] = mapped_column(Integer, nullable=False)
    relation: Mapped[E.Relation] = mapped_column(_enum(E.Relation, "edge_relation"), nullable=False)
    family_id: Mapped[uuid.UUID | None] = fk("duplicate_families.id", nullable=True)


class DuplicateFamily(Base):
    __tablename__ = "duplicate_families"
    __table_args__ = (UniqueConstraint("audit_run_id", "family_number"),)
    id: Mapped[uuid.UUID] = pk()
    audit_run_id: Mapped[uuid.UUID] = fk("audit_runs.id")
    family_number: Mapped[int] = mapped_column(Integer, nullable=False)
    root_sample_id: Mapped[uuid.UUID] = fk("samples.id", index=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    splits: Mapped[list[Any]] = jcol(nullable=False)
    labels: Mapped[list[Any]] = jcol(nullable=False)
    crosses_splits: Mapped[bool] = mapped_column(Boolean, nullable=False)
    label_conflict: Mapped[bool] = mapped_column(Boolean, nullable=False)
    max_similarity: Mapped[float] = mapped_column(Float, nullable=False)


class DuplicateFamilyMember(Base):
    __tablename__ = "duplicate_family_members"
    __table_args__ = (UniqueConstraint("family_id", "sample_id"),)
    id: Mapped[uuid.UUID] = pk()
    family_id: Mapped[uuid.UUID] = fk("duplicate_families.id")
    sample_id: Mapped[uuid.UUID] = fk("samples.id")
    parent_sample_id: Mapped[uuid.UUID | None] = fk("samples.id", nullable=True, index=False)
    relation: Mapped[str | None] = mapped_column(String(40))
    similarity: Mapped[float | None] = mapped_column(Float)
    phash_distance: Mapped[int | None] = mapped_column(Integer)
    depth: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    evidence: Mapped[dict[str, Any]] = jcol(default=dict, nullable=False)


class LeakageFinding(Base):
    __tablename__ = "leakage_findings"
    id: Mapped[uuid.UUID] = pk()
    audit_run_id: Mapped[uuid.UUID] = fk("audit_runs.id")
    family_id: Mapped[uuid.UUID | None] = fk("duplicate_families.id", nullable=True)
    kind: Mapped[E.LeakageKind] = mapped_column(_enum(E.LeakageKind, "leakage_kind"), nullable=False)
    risk: Mapped[E.Risk] = mapped_column(_enum(E.Risk, "leakage_risk"), nullable=False)
    splits_crossed: Mapped[list[Any]] = jcol(nullable=False)
    sample_ids: Mapped[list[Any]] = jcol(nullable=False)
    max_similarity: Mapped[float] = mapped_column(Float, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict[str, Any]] = jcol(default=dict, nullable=False)


class SplitIntegrityMetrics(Base):
    __tablename__ = "split_integrity_metrics"
    id: Mapped[uuid.UUID] = pk()
    audit_run_id: Mapped[uuid.UUID] = fk("audit_runs.id")
    metrics: Mapped[dict[str, Any]] = jcol(nullable=False)


# ---------------------------------------------------------------------------
# Model evidence
# ---------------------------------------------------------------------------


class BaselineModelRun(Base):
    __tablename__ = "baseline_model_runs"
    id: Mapped[uuid.UUID] = pk()
    audit_run_id: Mapped[uuid.UUID | None] = fk("audit_runs.id", nullable=True)
    what_if_run_id: Mapped[uuid.UUID | None] = fk("what_if_runs.id", nullable=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    config: Mapped[dict[str, Any]] = jcol(nullable=False)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    metrics: Mapped[dict[str, Any]] = jcol(default=dict, nullable=False)
    model_object_key: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = created()


class ModelPrediction(Base):
    __tablename__ = "model_predictions"
    __table_args__ = (UniqueConstraint("baseline_run_id", "sample_id"),)
    id: Mapped[uuid.UUID] = pk()
    audit_run_id: Mapped[uuid.UUID] = fk("audit_runs.id")
    baseline_run_id: Mapped[uuid.UUID] = fk("baseline_model_runs.id")
    sample_id: Mapped[uuid.UUID] = fk("samples.id")
    predicted_class_id: Mapped[uuid.UUID] = fk("classes.id", index=False)
    prob_given: Mapped[float] = mapped_column(Float, nullable=False)
    prob_predicted: Mapped[float] = mapped_column(Float, nullable=False)
    margin: Mapped[float] = mapped_column(Float, nullable=False)
    entropy: Mapped[float] = mapped_column(Float, nullable=False)
    top_probs: Mapped[list[Any]] = jcol(nullable=False)
    is_out_of_sample: Mapped[bool] = mapped_column(Boolean, nullable=False)
    correct: Mapped[bool] = mapped_column(Boolean, nullable=False)


class TrainingDynamics(Base):
    __tablename__ = "training_dynamics"
    __table_args__ = (UniqueConstraint("audit_run_id", "sample_id"),)
    id: Mapped[uuid.UUID] = pk()
    audit_run_id: Mapped[uuid.UUID] = fk("audit_runs.id")
    sample_id: Mapped[uuid.UUID] = fk("samples.id")
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    variability: Mapped[float] = mapped_column(Float, nullable=False)
    correctness: Mapped[float] = mapped_column(Float, nullable=False)
    forgetting_events: Mapped[int] = mapped_column(Integer, nullable=False)
    first_learned_epoch: Mapped[int | None] = mapped_column(Integer)
    trajectory: Mapped[list[Any]] = jcol(nullable=False)
    category: Mapped[E.DynamicsCategory] = mapped_column(
        _enum(E.DynamicsCategory, "dynamics_category"), nullable=False
    )


class LabelFinding(Base):
    __tablename__ = "label_findings"
    __table_args__ = (UniqueConstraint("audit_run_id", "sample_id"),)
    id: Mapped[uuid.UUID] = pk()
    audit_run_id: Mapped[uuid.UUID] = fk("audit_runs.id")
    sample_id: Mapped[uuid.UUID] = fk("samples.id")
    suspicion_score: Mapped[float] = mapped_column(Float, nullable=False)
    model_disagreement: Mapped[float] = mapped_column(Float, nullable=False)
    neighbor_agreement: Mapped[float] = mapped_column(Float, nullable=False)
    centroid_ratio: Mapped[float] = mapped_column(Float, nullable=False)
    predicted_class_id: Mapped[uuid.UUID | None] = fk("classes.id", nullable=True, index=False)
    neighbor_majority_class_id: Mapped[uuid.UUID | None] = fk("classes.id", nullable=True, index=False)
    recommended_action: Mapped[E.LabelAction] = mapped_column(
        _enum(E.LabelAction, "label_action"), nullable=False
    )
    evidence: Mapped[dict[str, Any]] = jcol(nullable=False)


class RareWrongFinding(Base):
    __tablename__ = "rare_wrong_findings"
    __table_args__ = (UniqueConstraint("audit_run_id", "sample_id"),)
    id: Mapped[uuid.UUID] = pk()
    audit_run_id: Mapped[uuid.UUID] = fk("audit_runs.id")
    sample_id: Mapped[uuid.UUID] = fk("samples.id")
    hypothesis: Mapped[E.Hypothesis] = mapped_column(_enum(E.Hypothesis, "hypothesis"), nullable=False)
    scores: Mapped[dict[str, Any]] = jcol(nullable=False)
    reasons: Mapped[list[Any]] = jcol(nullable=False)
    valuable_reasons: Mapped[list[Any]] = jcol(default=list, nullable=False)


class ShortcutFinding(Base):
    __tablename__ = "shortcut_findings"
    id: Mapped[uuid.UUID] = pk()
    audit_run_id: Mapped[uuid.UUID] = fk("audit_runs.id")
    cue: Mapped[str] = mapped_column(String(60), nullable=False)
    cue_value: Mapped[str | None] = mapped_column(String(120))
    class_id: Mapped[uuid.UUID | None] = fk("classes.id", nullable=True, index=False)
    strength: Mapped[float] = mapped_column(Float, nullable=False)
    strength_label: Mapped[str] = mapped_column(String(20), nullable=False)
    association: Mapped[dict[str, Any]] = jcol(nullable=False)
    affected_sample_ids: Mapped[list[Any]] = jcol(nullable=False)
    consequence: Mapped[str] = mapped_column(Text, nullable=False)
    recommendation: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict[str, Any]] = jcol(default=dict, nullable=False)


class CoverageCluster(Base):
    __tablename__ = "coverage_clusters"
    __table_args__ = (UniqueConstraint("audit_run_id", "cluster_index"),)
    id: Mapped[uuid.UUID] = pk()
    audit_run_id: Mapped[uuid.UUID] = fk("audit_runs.id")
    cluster_index: Mapped[int] = mapped_column(Integer, nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    centroid_2d: Mapped[list[Any]] = jcol(nullable=False)
    class_composition: Mapped[dict[str, Any]] = jcol(nullable=False)
    split_composition: Mapped[dict[str, Any]] = jcol(nullable=False)
    purity: Mapped[float] = mapped_column(Float, nullable=False)
    density: Mapped[float] = mapped_column(Float, nullable=False)
    is_sparse: Mapped[bool] = mapped_column(Boolean, nullable=False)
    dominant_class_id: Mapped[uuid.UUID | None] = fk("classes.id", nullable=True, index=False)
    attributes: Mapped[dict[str, Any]] = jcol(default=dict, nullable=False)


class CoverageGap(Base):
    __tablename__ = "coverage_gaps"
    id: Mapped[uuid.UUID] = pk()
    audit_run_id: Mapped[uuid.UUID] = fk("audit_runs.id")
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    class_id: Mapped[uuid.UUID | None] = fk("classes.id", nullable=True, index=False)
    cluster_index: Mapped[int | None] = mapped_column(Integer)
    condition: Mapped[dict[str, Any]] = jcol(default=dict, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    priority_score: Mapped[float] = mapped_column(Float, nullable=False)
    priority: Mapped[str] = mapped_column(String(20), nullable=False)
    suggested_quantity: Mapped[str] = mapped_column(String(40), nullable=False)
    anchors: Mapped[list[Any]] = jcol(default=list, nullable=False)
    evidence: Mapped[dict[str, Any]] = jcol(default=dict, nullable=False)


class CollectionTask(Base):
    __tablename__ = "collection_tasks"
    id: Mapped[uuid.UUID] = pk()
    org_id: Mapped[uuid.UUID] = fk("organizations.id")
    dataset_id: Mapped[uuid.UUID] = fk("datasets.id")
    gap_id: Mapped[uuid.UUID | None] = fk("coverage_gaps.id", nullable=True, ondelete="SET NULL")
    target_class: Mapped[str] = mapped_column(String(255), nullable=False)
    target_condition: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[str] = mapped_column(String(40), nullable=False)
    priority: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    anchors: Mapped[list[Any]] = jcol(default=list, nullable=False)
    status: Mapped[E.CollectionStatus] = mapped_column(
        _enum(E.CollectionStatus, "collection_status"), nullable=False
    )
    created_by: Mapped[uuid.UUID | None] = fk("users.id", nullable=True, ondelete="SET NULL")
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = created()


class InfluenceFinding(Base):
    __tablename__ = "influence_findings"
    __table_args__ = (UniqueConstraint("audit_run_id", "sample_id"),)
    id: Mapped[uuid.UUID] = pk()
    audit_run_id: Mapped[uuid.UUID] = fk("audit_runs.id")
    sample_id: Mapped[uuid.UUID] = fk("samples.id")
    self_influence: Mapped[float] = mapped_column(Float, nullable=False)
    self_influence_pct: Mapped[float] = mapped_column(Float, nullable=False)
    harmful_influence: Mapped[float] = mapped_column(Float, nullable=False)
    helpful_influence: Mapped[float] = mapped_column(Float, nullable=False)
    failures_harmed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    affected_classes: Mapped[list[Any]] = jcol(default=list, nullable=False)


class FailureEvent(Base):
    __tablename__ = "failure_events"
    __table_args__ = (UniqueConstraint("audit_run_id", "sample_id"),)
    id: Mapped[uuid.UUID] = pk()
    audit_run_id: Mapped[uuid.UUID] = fk("audit_runs.id")
    sample_id: Mapped[uuid.UUID] = fk("samples.id")
    split: Mapped[str] = mapped_column(String(10), nullable=False)
    actual_class_id: Mapped[uuid.UUID] = fk("classes.id", index=False)
    predicted_class_id: Mapped[uuid.UUID] = fk("classes.id", index=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)


class FailureDataLink(Base):
    __tablename__ = "failure_data_links"
    id: Mapped[uuid.UUID] = pk()
    failure_event_id: Mapped[uuid.UUID] = fk("failure_events.id")
    train_sample_id: Mapped[uuid.UUID] = fk("samples.id")
    link_type: Mapped[E.LinkType] = mapped_column(_enum(E.LinkType, "link_type"), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)


class PrivacyFinding(Base):
    __tablename__ = "privacy_findings"
    id: Mapped[uuid.UUID] = pk()
    audit_run_id: Mapped[uuid.UUID] = fk("audit_runs.id")
    sample_id: Mapped[uuid.UUID] = fk("samples.id")
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    boxes: Mapped[list[Any]] = jcol(default=list, nullable=False)


# ---------------------------------------------------------------------------
# Court
# ---------------------------------------------------------------------------


class CourtCase(Base):
    __tablename__ = "court_cases"
    __table_args__ = (
        UniqueConstraint("audit_run_id", "case_number"),
        UniqueConstraint("audit_run_id", "sample_id"),
        Index("ix_cases_audit_verdict", "audit_run_id", "verdict"),
        Index("ix_cases_audit_priority", "audit_run_id", "priority_score"),
    )
    id: Mapped[uuid.UUID] = pk()
    org_id: Mapped[uuid.UUID] = fk("organizations.id")
    audit_run_id: Mapped[uuid.UUID] = fk("audit_runs.id", index=False)
    dataset_version_id: Mapped[uuid.UUID] = fk("dataset_versions.id")
    case_number: Mapped[int] = mapped_column(Integer, nullable=False)
    sample_id: Mapped[uuid.UUID] = fk("samples.id")
    verdict: Mapped[E.Verdict] = mapped_column(_enum(E.Verdict, "verdict"), nullable=False)
    priority_score: Mapped[float] = mapped_column(Float, nullable=False)
    impact_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    strength_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    uncertainty: Mapped[E.Uncertainty] = mapped_column(_enum(E.Uncertainty, "uncertainty"), nullable=False)
    reason_codes: Mapped[list[Any]] = jcol(nullable=False)
    categories: Mapped[list[Any]] = jcol(default=list, nullable=False)
    primary_concern: Mapped[str] = mapped_column(String(60), nullable=False)
    family_id: Mapped[uuid.UUID | None] = fk("duplicate_families.id", nullable=True, ondelete="SET NULL")
    est_review_minutes: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    status: Mapped[E.CaseStatus] = mapped_column(_enum(E.CaseStatus, "case_status"), nullable=False)
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = created()


class CaseEvidence(Base):
    __tablename__ = "case_evidence"
    id: Mapped[uuid.UUID] = pk()
    case_id: Mapped[uuid.UUID] = fk("court_cases.id")
    witness: Mapped[E.Witness] = mapped_column(_enum(E.Witness, "witness"), nullable=False)
    stance: Mapped[E.Stance] = mapped_column(_enum(E.Stance, "stance"), nullable=False)
    evidence_kind: Mapped[E.EvidenceKind] = mapped_column(
        _enum(E.EvidenceKind, "evidence_kind"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(60), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[dict[str, Any]] = jcol(default=dict, nullable=False)
    weight: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)


class CaseVerdict(Base):
    __tablename__ = "case_verdicts"
    id: Mapped[uuid.UUID] = pk()
    case_id: Mapped[uuid.UUID] = fk("court_cases.id")
    jury_version: Mapped[str] = mapped_column(String(40), nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    verdict: Mapped[E.Verdict] = mapped_column(_enum(E.Verdict, "case_verdict"), nullable=False)
    reason_codes: Mapped[list[Any]] = jcol(nullable=False)
    rule_trace: Mapped[list[Any]] = jcol(nullable=False)
    scores: Mapped[dict[str, Any]] = jcol(nullable=False)
    created_at: Mapped[datetime] = created()


class CaseExplanation(Base):
    __tablename__ = "case_explanations"
    id: Mapped[uuid.UUID] = pk()
    case_id: Mapped[uuid.UUID] = fk("court_cases.id")
    source: Mapped[E.ExplanationSource] = mapped_column(
        _enum(E.ExplanationSource, "explanation_source"), nullable=False
    )
    model: Mapped[str | None] = mapped_column(String(80))
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[dict[str, Any]] = jcol(nullable=False)
    created_by: Mapped[uuid.UUID | None] = fk("users.id", nullable=True, ondelete="SET NULL")
    created_at: Mapped[datetime] = created()


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------


class ReviewSession(Base):
    __tablename__ = "review_sessions"
    id: Mapped[uuid.UUID] = pk()
    org_id: Mapped[uuid.UUID] = fk("organizations.id")
    dataset_version_id: Mapped[uuid.UUID] = fk("dataset_versions.id")
    audit_run_id: Mapped[uuid.UUID] = fk("audit_runs.id")
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    params: Mapped[dict[str, Any]] = jcol(default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    created_by: Mapped[uuid.UUID | None] = fk("users.id", nullable=True, ondelete="SET NULL")
    created_at: Mapped[datetime] = created()


class ReviewQueueItem(Base):
    __tablename__ = "review_queue_items"
    __table_args__ = (UniqueConstraint("session_id", "case_id"),)
    id: Mapped[uuid.UUID] = pk()
    session_id: Mapped[uuid.UUID] = fk("review_sessions.id")
    case_id: Mapped[uuid.UUID] = fk("court_cases.id")
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    est_minutes: Mapped[float] = mapped_column(Float, nullable=False)
    value_score: Mapped[float] = mapped_column(Float, nullable=False)


class HumanDecision(Base):
    __tablename__ = "human_decisions"
    __table_args__ = (Index("ix_decisions_case_created", "case_id", "created_at"),)
    id: Mapped[uuid.UUID] = pk()
    case_id: Mapped[uuid.UUID] = fk("court_cases.id", index=False)
    dataset_version_id: Mapped[uuid.UUID] = fk("dataset_versions.id")
    reviewer_id: Mapped[uuid.UUID] = fk("users.id")
    action: Mapped[E.DecisionAction] = mapped_column(
        _enum(E.DecisionAction, "decision_action"), nullable=False
    )
    target_class_id: Mapped[uuid.UUID | None] = fk("classes.id", nullable=True, index=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    evidence_snapshot: Mapped[dict[str, Any]] = jcol(nullable=False)
    previous_decision_id: Mapped[uuid.UUID | None] = fk(
        "human_decisions.id", nullable=True, ondelete="SET NULL", index=False
    )
    is_adjudication: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = created()
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    undone_by: Mapped[uuid.UUID | None] = fk("users.id", nullable=True, ondelete="SET NULL", index=False)


class ReviewerNote(Base):
    __tablename__ = "reviewer_notes"
    id: Mapped[uuid.UUID] = pk()
    case_id: Mapped[uuid.UUID] = fk("court_cases.id")
    user_id: Mapped[uuid.UUID] = fk("users.id")
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = created()


class ReviewBudgetRun(Base):
    __tablename__ = "review_budget_runs"
    id: Mapped[uuid.UUID] = pk()
    dataset_version_id: Mapped[uuid.UUID] = fk("dataset_versions.id")
    audit_run_id: Mapped[uuid.UUID] = fk("audit_runs.id")
    session_id: Mapped[uuid.UUID | None] = fk("review_sessions.id", nullable=True, ondelete="SET NULL")
    params: Mapped[dict[str, Any]] = jcol(nullable=False)
    result: Mapped[dict[str, Any]] = jcol(nullable=False)
    created_by: Mapped[uuid.UUID | None] = fk("users.id", nullable=True, ondelete="SET NULL")
    created_at: Mapped[datetime] = created()


# ---------------------------------------------------------------------------
# What-if
# ---------------------------------------------------------------------------


class WhatIfRun(Base):
    __tablename__ = "what_if_runs"
    id: Mapped[uuid.UUID] = pk()
    org_id: Mapped[uuid.UUID] = fk("organizations.id")
    dataset_version_id: Mapped[uuid.UUID] = fk("dataset_versions.id")
    audit_run_id: Mapped[uuid.UUID] = fk("audit_runs.id")
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    source: Mapped[str] = mapped_column(String(30), default="lab", nullable=False)
    failure_event_id: Mapped[uuid.UUID | None] = fk("failure_events.id", nullable=True, ondelete="SET NULL")
    status: Mapped[E.RunStatus] = mapped_column(_enum(E.RunStatus, "what_if_status"), nullable=False)
    config: Mapped[dict[str, Any]] = jcol(default=dict, nullable=False)
    summary: Mapped[dict[str, Any]] = jcol(default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_by: Mapped[uuid.UUID | None] = fk("users.id", nullable=True, ondelete="SET NULL")
    created_at: Mapped[datetime] = created()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WhatIfAction(Base):
    __tablename__ = "what_if_actions"
    id: Mapped[uuid.UUID] = pk()
    run_id: Mapped[uuid.UUID] = fk("what_if_runs.id")
    action_type: Mapped[str] = mapped_column(String(40), nullable=False)
    params: Mapped[dict[str, Any]] = jcol(default=dict, nullable=False)
    affected_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class WhatIfResult(Base):
    __tablename__ = "what_if_results"
    __table_args__ = (UniqueConstraint("run_id", "variant", "eval_set"),)
    id: Mapped[uuid.UUID] = pk()
    run_id: Mapped[uuid.UUID] = fk("what_if_runs.id")
    variant: Mapped[str] = mapped_column(String(20), nullable=False)
    eval_set: Mapped[str] = mapped_column(String(40), nullable=False)
    metrics: Mapped[dict[str, Any]] = jcol(nullable=False)


# ---------------------------------------------------------------------------
# Governance
# ---------------------------------------------------------------------------


class DatasetDebtSnapshot(Base):
    __tablename__ = "dataset_debt_snapshots"
    id: Mapped[uuid.UUID] = pk()
    dataset_version_id: Mapped[uuid.UUID] = fk("dataset_versions.id")
    audit_run_id: Mapped[uuid.UUID] = fk("audit_runs.id")
    formula_version: Mapped[str] = mapped_column(String(40), nullable=False)
    trigger: Mapped[str] = mapped_column(String(20), nullable=False)
    overall: Mapped[E.DebtLevel] = mapped_column(_enum(E.DebtLevel, "debt_level"), nullable=False)
    dimensions: Mapped[dict[str, Any]] = jcol(nullable=False)
    created_at: Mapped[datetime] = created()


class PreflightResult(Base):
    __tablename__ = "preflight_results"
    id: Mapped[uuid.UUID] = pk()
    audit_run_id: Mapped[uuid.UUID] = fk("audit_runs.id")
    dataset_version_id: Mapped[uuid.UUID] = fk("dataset_versions.id")
    rules_version: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[E.PreflightStatus] = mapped_column(
        _enum(E.PreflightStatus, "preflight_status"), nullable=False
    )
    checks: Mapped[list[Any]] = jcol(nullable=False)
    override_status: Mapped[str | None] = mapped_column(String(30))
    override_reason: Mapped[str | None] = mapped_column(Text)
    override_by: Mapped[uuid.UUID | None] = fk("users.id", nullable=True, ondelete="SET NULL", index=False)
    override_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created()


class ContractEvaluation(Base):
    __tablename__ = "contract_evaluations"
    id: Mapped[uuid.UUID] = pk()
    contract_id: Mapped[uuid.UUID] = fk("data_contracts.id")
    dataset_version_id: Mapped[uuid.UUID] = fk("dataset_versions.id")
    audit_run_id: Mapped[uuid.UUID] = fk("audit_runs.id")
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    results: Mapped[list[Any]] = jcol(nullable=False)
    created_at: Mapped[datetime] = created()


class DatasetDnaProfile(Base):
    __tablename__ = "dataset_dna_profiles"
    id: Mapped[uuid.UUID] = pk()
    audit_run_id: Mapped[uuid.UUID] = fk("audit_runs.id")
    dataset_version_id: Mapped[uuid.UUID] = fk("dataset_versions.id")
    dna_version: Mapped[str] = mapped_column(String(40), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    profile: Mapped[dict[str, Any]] = jcol(nullable=False)
    created_at: Mapped[datetime] = created()


class DatasetVersionDiff(Base):
    __tablename__ = "dataset_version_diffs"
    __table_args__ = (UniqueConstraint("from_version_id", "to_version_id"),)
    id: Mapped[uuid.UUID] = pk()
    from_version_id: Mapped[uuid.UUID] = fk("dataset_versions.id")
    to_version_id: Mapped[uuid.UUID] = fk("dataset_versions.id")
    diff: Mapped[dict[str, Any]] = jcol(nullable=False)
    created_at: Mapped[datetime] = created()


class ContaminationCheck(Base):
    __tablename__ = "contamination_checks"
    id: Mapped[uuid.UUID] = pk()
    org_id: Mapped[uuid.UUID] = fk("organizations.id")
    source_version_id: Mapped[uuid.UUID] = fk("dataset_versions.id")
    reference_version_id: Mapped[uuid.UUID] = fk("dataset_versions.id")
    status: Mapped[E.RunStatus] = mapped_column(_enum(E.RunStatus, "contamination_status"), nullable=False)
    result: Mapped[dict[str, Any]] = jcol(default=dict, nullable=False)
    created_by: Mapped[uuid.UUID | None] = fk("users.id", nullable=True, ondelete="SET NULL")
    created_at: Mapped[datetime] = created()


class Export(Base):
    __tablename__ = "exports"
    id: Mapped[uuid.UUID] = pk()
    org_id: Mapped[uuid.UUID] = fk("organizations.id")
    dataset_version_id: Mapped[uuid.UUID] = fk("dataset_versions.id")
    audit_run_id: Mapped[uuid.UUID | None] = fk("audit_runs.id", nullable=True)
    status: Mapped[E.RunStatus] = mapped_column(_enum(E.RunStatus, "export_status"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    object_key: Mapped[str | None] = mapped_column(String(512))
    byte_size: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64))
    summary: Mapped[dict[str, Any]] = jcol(default=dict, nullable=False)
    new_version_id: Mapped[uuid.UUID | None] = fk(
        "dataset_versions.id", nullable=True, ondelete="SET NULL", index=False
    )
    error: Mapped[str | None] = mapped_column(Text)
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_by: Mapped[uuid.UUID | None] = fk("users.id", nullable=True, ondelete="SET NULL")
    created_at: Mapped[datetime] = created()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditReport(Base):
    __tablename__ = "audit_reports"
    id: Mapped[uuid.UUID] = pk()
    org_id: Mapped[uuid.UUID] = fk("organizations.id")
    audit_run_id: Mapped[uuid.UUID] = fk("audit_runs.id")
    status: Mapped[E.RunStatus] = mapped_column(_enum(E.RunStatus, "report_status"), nullable=False)
    html_object_key: Mapped[str | None] = mapped_column(String(512))
    json_object_key: Mapped[str | None] = mapped_column(String(512))
    error: Mapped[str | None] = mapped_column(Text)
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_by: Mapped[uuid.UUID | None] = fk("users.id", nullable=True, ondelete="SET NULL")
    created_at: Mapped[datetime] = created()


class EvidenceLedgerEntry(Base):
    """Append-only, hash-chained record of decisions (per organization)."""

    __tablename__ = "evidence_ledger"
    __table_args__ = (Index("ix_ledger_org_seq", "org_id", "seq"),)
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True, default=uuid.uuid4, nullable=False)
    org_id: Mapped[uuid.UUID] = fk("organizations.id", index=False)
    dataset_version_id: Mapped[uuid.UUID | None] = fk(
        "dataset_versions.id", nullable=True, ondelete="SET NULL"
    )
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = fk("users.id", nullable=True, ondelete="SET NULL", index=False)
    payload: Mapped[dict[str, Any]] = jcol(nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = created()


class SecurityAuditLog(Base):
    __tablename__ = "security_audit_log"
    id: Mapped[uuid.UUID] = pk()
    org_id: Mapped[uuid.UUID | None] = fk("organizations.id", nullable=True, ondelete="SET NULL")
    actor_id: Mapped[uuid.UUID | None] = fk("users.id", nullable=True, ondelete="SET NULL", index=False)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(40))
    target_id: Mapped[str | None] = mapped_column(String(64))
    meta: Mapped[dict[str, Any]] = jcol(default=dict, nullable=False)
    created_at: Mapped[datetime] = created()


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_claim", "status", "priority", "run_after"),
        UniqueConstraint("idempotency_key"),
    )
    id: Mapped[uuid.UUID] = pk()
    org_id: Mapped[uuid.UUID | None] = fk("organizations.id", nullable=True)
    type: Mapped[E.JobType] = mapped_column(_enum(E.JobType, "job_type"), nullable=False)
    payload: Mapped[dict[str, Any]] = jcol(nullable=False)
    status: Mapped[E.RunStatus] = mapped_column(_enum(E.RunStatus, "job_status"), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    run_after: Mapped[datetime] = created()
    locked_by: Mapped[str | None] = mapped_column(String(120))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    progress: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    stage: Mapped[str | None] = mapped_column(String(60))
    error_class: Mapped[str | None] = mapped_column(String(40))
    error_message: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any]] = jcol(default=dict, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(160))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Soft time limit; a worker only claims jobs that fit in its remaining time budget.
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=3600, nullable=False)
    # Execution bookkeeping: when a worker was last woken for this job, and where it ran.
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatch_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    dispatch_slot: Mapped[int | None] = mapped_column(Integer)
    dispatch_error: Mapped[str | None] = mapped_column(String(300))
    runner_url: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = created()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobEvent(Base):
    __tablename__ = "job_events"
    id: Mapped[uuid.UUID] = pk()
    job_id: Mapped[uuid.UUID] = fk("jobs.id")
    level: Mapped[str] = mapped_column(String(10), nullable=False)
    event: Mapped[str] = mapped_column(String(80), nullable=False)
    data: Mapped[dict[str, Any]] = jcol(default=dict, nullable=False)
    created_at: Mapped[datetime] = created()
