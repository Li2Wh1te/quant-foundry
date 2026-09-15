"""Persist collection progress and resumable request units."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '20260921_01'
down_revision = '20260920_01'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('task_runs', sa.Column('collection_progress', JSONB(), nullable=True))
    op.create_table('tonghuashun_work_units',
        sa.Column('scope', sa.String(64), primary_key=True),
        sa.Column('request_key', sa.String(64), primary_key=True),
        sa.Column('data_json', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))


def downgrade():
    op.drop_table('tonghuashun_work_units')
    op.drop_column('task_runs', 'collection_progress')
