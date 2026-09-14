"""Add restartable staging for validated Tonghuashun Parquet imports."""
from alembic import op
import sqlalchemy as sa

revision = '20260920_01'
down_revision = '20260919_01'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('tonghuashun_dump_imports',
        sa.Column('dataset', sa.String(80), primary_key=True),
        sa.Column('generation', sa.Uuid(), nullable=False),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('lease_until', sa.DateTime(timezone=True), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True)),
        sa.Column('digest', sa.String(64)),
        sa.Column('metadata_json', sa.Text(), nullable=False),
        sa.Column('total_subjects', sa.Integer(), nullable=False),
        sa.Column('imported_subjects', sa.Integer(), nullable=False),
        sa.Column('superseded_subjects', sa.Integer(), nullable=False))
    op.create_table('tonghuashun_dump_stages',
        sa.Column('dataset', sa.String(80), sa.ForeignKey('tonghuashun_dump_imports.dataset'), primary_key=True),
        sa.Column('subject', sa.String(64), primary_key=True),
        sa.Column('data_json', sa.Text(), nullable=False))


def downgrade():
    op.drop_table('tonghuashun_dump_stages')
    op.drop_table('tonghuashun_dump_imports')
