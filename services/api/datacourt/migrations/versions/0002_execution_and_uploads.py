"""execution bookkeeping, multipart uploads, purge jobs

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-25 00:00:00
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_JOB_TYPES_V1 = [
    "ingest_version",
    "run_audit",
    "what_if",
    "export",
    "report",
    "version_diff",
    "contamination",
]
_JOB_TYPES_V2 = _JOB_TYPES_V1 + ["purge_prefix"]


def _job_type_check(values: list[str]) -> None:
    op.drop_constraint("job_type", "jobs", type_="check")
    allowed = ", ".join(f"'{v}'" for v in values)
    op.create_check_constraint("job_type", "jobs", f"type IN ({allowed})")


def upgrade() -> None:
    op.add_column("jobs", sa.Column("timeout_seconds", sa.Integer(), server_default="3600", nullable=False))
    op.add_column("jobs", sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("jobs", sa.Column("dispatch_attempts", sa.Integer(), server_default="0", nullable=False))
    op.add_column("jobs", sa.Column("dispatch_slot", sa.Integer(), nullable=True))
    op.add_column("jobs", sa.Column("dispatch_error", sa.String(length=300), nullable=True))
    op.add_column("jobs", sa.Column("runner_url", sa.String(length=300), nullable=True))
    _job_type_check(_JOB_TYPES_V2)
    op.add_column("dataset_versions", sa.Column("upload_id", sa.String(length=300), nullable=True))
    op.add_column(
        "dataset_versions",
        sa.Column(
            "upload_meta",
            postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite"),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("dataset_versions", "upload_meta")
    op.drop_column("dataset_versions", "upload_id")
    op.execute("DELETE FROM jobs WHERE type = 'purge_prefix'")
    _job_type_check(_JOB_TYPES_V1)
    op.drop_column("jobs", "runner_url")
    op.drop_column("jobs", "dispatch_error")
    op.drop_column("jobs", "dispatch_slot")
    op.drop_column("jobs", "dispatch_attempts")
    op.drop_column("jobs", "dispatched_at")
    op.drop_column("jobs", "timeout_seconds")
