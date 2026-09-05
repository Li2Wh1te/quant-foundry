"""Preserve quantity-company-action facts independently of cash accounting."""
from alembic import op
import sqlalchemy as sa
revision = "20260911_01"
down_revision = "20260910_01"
branch_labels = None
depends_on = None


def upgrade():
    for name in ("quantity_ratio", "quantity_delta"):
        op.add_column("corporate_action_facts", sa.Column(name, sa.Numeric(28, 12), nullable=True))


def downgrade():
    for name in ("quantity_delta", "quantity_ratio"):
        op.drop_column("corporate_action_facts", name)
