"""Add bounded root audit projection."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
revision='20260912_01'; down_revision='20260911_01'; branch_labels=None; depends_on=None
def upgrade():
    op.add_column('backtest_runs', sa.Column('audit_projection', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")))
def downgrade(): op.drop_column('backtest_runs','audit_projection')
