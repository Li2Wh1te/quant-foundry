"""Retain fixed daily-bar applicability independently of provider decoders."""
from alembic import op

revision = '20260924_01'
down_revision = '20260923_01'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute('''CREATE TABLE foundation_work_coverage (
        work_id UUID PRIMARY KEY REFERENCES foundation_work(id) ON DELETE RESTRICT,
        assessment_id UUID NOT NULL REFERENCES foundation_assessments(id) ON DELETE RESTRICT
    )''')
    op.execute('''CREATE TRIGGER foundation_work_coverage_immutable
        BEFORE UPDATE OR DELETE ON foundation_work_coverage
        FOR EACH ROW EXECUTE FUNCTION foundation_immutable()''')


def downgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute('DROP TABLE foundation_work_coverage')
