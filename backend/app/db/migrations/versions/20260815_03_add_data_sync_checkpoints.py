"""Add generic data synchronization checkpoints.

Revision ID: 20260815_03
Revises: 20260815_02
Create Date: 2026-08-15
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260815_03"
down_revision: str | None = "20260815_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "data_sync_checkpoints",
        sa.Column("sync_key", sa.String(length=128), nullable=False, comment="Stable logical data synchronization key."),
        sa.Column("scope_key", sa.String(length=256), nullable=False, comment="Stable synchronization partition key."),
        sa.Column("cursor", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False, comment="Versioned position of the last committed synchronization."),
        sa.Column("cursor_version", sa.SmallInteger(), server_default="1", nullable=False, comment="Schema version of the cursor object."),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False, comment="Optimistic locking version for cursor advancement."),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, comment="Time at which the checkpoint was first committed."),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, comment="Time at which the checkpoint was last advanced."),
        sa.CheckConstraint("length(btrim(sync_key)) > 0", name="sync_key_not_blank"),
        sa.CheckConstraint("length(btrim(scope_key)) > 0", name="scope_key_not_blank"),
        sa.CheckConstraint("jsonb_typeof(cursor) = 'object'", name="cursor_object"),
        sa.CheckConstraint("cursor_version > 0", name="cursor_version_positive"),
        sa.CheckConstraint("version > 0", name="version_positive"),
        sa.CheckConstraint("updated_at >= created_at", name="updated_after_created"),
        sa.PrimaryKeyConstraint("sync_key", "scope_key", name="pk_data_sync_checkpoints"),
        comment="Stores reusable committed positions for incremental data synchronizations.",
    )


def downgrade() -> None:
    op.drop_table("data_sync_checkpoints")
