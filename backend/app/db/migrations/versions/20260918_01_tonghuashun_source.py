"""Seed the independently configured Tonghuashun data source."""

from alembic import op
import sqlalchemy as sa

revision = "20260918_01"
down_revision = "20260917_01"
branch_labels = None
depends_on = None


def upgrade():
    # Schema migration never reads credentials or changes existing providers.
    # The normal startup transaction installs public defaults exactly once.
    table = sa.table("data_source_configs", sa.column("key", sa.String(80)))
    op.bulk_insert(table, [{"key": "tonghuashun"}])


def downgrade():
    table = sa.table("data_source_configs", sa.column("key", sa.String(80)))
    op.execute(table.delete().where(table.c.key == "tonghuashun"))
