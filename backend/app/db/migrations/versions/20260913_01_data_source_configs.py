"""Add provider-owned configuration and a stable scheduler serialization row."""

from alembic import op
import sqlalchemy as sa

revision = "20260913_01"
down_revision = "20260912_01"
branch_labels = None
depends_on = None


def upgrade():
    table = op.create_table(
        "data_source_configs",
        sa.Column("key", sa.String(80), primary_key=True),
        sa.Column("initialized", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("values", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("encrypted_secrets", sa.Text()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("checked_at", sa.DateTime(timezone=True)),
        sa.Column("check_status", sa.String(32), nullable=False, server_default="not_checked"),
        sa.Column("check_message", sa.Text()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    # Read no environment credentials during schema migrations. Initialization
    # happens once under this row lock before the application scheduler starts.
    op.bulk_insert(table, [{"key": "tushare"}])


def downgrade():
    op.drop_table("data_source_configs")
