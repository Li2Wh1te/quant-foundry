"""Keep exact transactional issue capacity without a full scan per commit."""
from alembic import op
from sqlalchemy import text

revision='20261011_01'
down_revision='20261010_01'
branch_labels=None
depends_on=None

def upgrade():
    from app.data_store.issue_accounting import install
    c=op.get_bind()
    c.execute(text("SET LOCAL lock_timeout='5s'"))
    install(c)

def downgrade():
    # The derived inventory is fully recoverable from the untouched issues.
    # This rollback never downgrades member or source revision interpretation.
    c=op.get_bind()
    c.execute(text("SET LOCAL lock_timeout='5s'"))
    c.execute(text('LOCK TABLE data_store_issues IN SHARE ROW EXCLUSIVE MODE'))
    c.execute(text('DROP TRIGGER data_store_issue_records_truncate ON data_store_issues'))
    c.execute(text('DROP TRIGGER data_store_issue_records_row ON data_store_issues'))
    c.execute(text('DROP FUNCTION data_store_count_issue_records()'))
    c.execute(text('DROP INDEX ix_data_store_issues_scope_page'))
    c.execute(text('DROP TABLE data_store_issue_totals'))
