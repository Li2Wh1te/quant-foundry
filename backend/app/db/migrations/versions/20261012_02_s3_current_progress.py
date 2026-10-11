"""One current S3 progress value; no event ledger or business-data rewrite."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261012_02"
down_revision = "20261012_01"
branch_labels = depends_on = None


def upgrade():
    op.add_column(
        "s3_runs",
        sa.Column(
            "progress",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
    )


def downgrade():
    # Never remove recorded runtime state from retained business runs.
    if (
        op.get_bind()
        .execute(
            sa.text("SELECT EXISTS(SELECT 1 FROM s3_runs WHERE progress IS NOT NULL)")
        )
        .scalar()
    ):
        raise RuntimeError("S3 progress exists; stop admission and repair forward")
    op.drop_column("s3_runs", "progress")
