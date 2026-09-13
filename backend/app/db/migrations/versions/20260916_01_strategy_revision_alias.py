"""Store immutable publication aliases; preserve unnamed historical revisions."""
from alembic import op
import sqlalchemy as sa

revision = "20260916_01"
down_revision = "20260915_01"
branch_labels = None
depends_on = None


def upgrade():
    # The existing unconditional update/delete trigger also protects this column.
    op.add_column("strategy_revisions", sa.Column("alias", sa.String(100), nullable=True))


def downgrade():
    op.drop_column("strategy_revisions", "alias")
