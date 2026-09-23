"""Durable fair rotation for local formalization updates."""
from alembic import op
import sqlalchemy as sa

revision = '20261002_01'
down_revision = '20261001_01'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table('foundation_update_visits',
        sa.Column('observation_id', sa.Uuid(), sa.ForeignKey('tonghuashun_observations.id', ondelete='RESTRICT'), primary_key=True),
        sa.Column('execution_id', sa.Uuid(), sa.ForeignKey('foundation_execution_manifests.id', ondelete='RESTRICT'), primary_key=True),
        sa.Column('source_revision', sa.Integer(), primary_key=True),
        sa.Column('visited_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('visits', sa.Integer(), nullable=False),
        sa.Column('result_json', sa.Text(), nullable=False),
        sa.CheckConstraint('source_revision >= 0 AND visits > 0', name='update_visit_counts'))


def downgrade():
    if op.get_bind().scalar(sa.text('SELECT EXISTS (SELECT 1 FROM foundation_update_visits)')):
        raise RuntimeError('Update receipts exist; use a compatible application rollback')
    op.drop_table('foundation_update_visits')
