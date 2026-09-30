"""Fence captured source ranges and index conservative current restrictions."""
from alembic import op
from sqlalchemy import text
revision='20261010_01'
down_revision='20261009_01'
branch_labels=None
depends_on=None
DDL="CREATE INDEX IF NOT EXISTS ix_data_store_issues_global ON data_store_issues(dataset,issue_key)\nWHERE target_json::jsonb->>'partition' IS NULL OR target_json::jsonb->>'scope_version' IS NULL\nOR target_json::jsonb->>'scope_version' NOT IN ('object-key-v1','object-key-set-v1')"

def install(c):
    from importlib import import_module
    c.execute(text("SET LOCAL lock_timeout='5s'"))
    previous=import_module('app.db.migrations.versions.20261008_01_current_source_ranges')
    for table in previous.TABLES:c.execute(text(f'LOCK TABLE {table} IN SHARE ROW EXCLUSIVE MODE'))
    c.execute(text('ALTER TABLE data_store_source_ranges ADD COLUMN IF NOT EXISTS change_revision bigint NOT NULL DEFAULT 0 CHECK(change_revision>=0)'))
    # Derive only this function from the frozen additive migration, then
    # retain fund coverage while advancing every captured producer revision.
    ddl=previous.DDL.split('CREATE FUNCTION data_store_mark_source_range()',1)[1].split('CREATE FUNCTION data_store_no_source_truncate()',1)[0]
    ddl=('CREATE OR REPLACE FUNCTION data_store_mark_source_range()'+ddl).replace(
        "('stock_daily','etf_daily','index_daily')",
        "('stock_daily','etf_daily','index_daily','fund_manager_performance','fund_nav')")
    ddl=ddl.replace('(source,dataset,subject,variant,range_key,lower_at)',
                    '(source,dataset,subject,variant,range_key,lower_at,change_revision)')
    ddl=ddl.replace('VALUES(src,ds,sub,vr,rk,stamp)', 'VALUES(src,ds,sub,vr,rk,stamp,1)')
    ddl=ddl.replace('pending=true;', 'pending=true,change_revision=q.change_revision+1;')
    c.execute(text(ddl));c.execute(text(DDL))
    c.execute(text('UPDATE data_store_capture_version SET version=3 WHERE singleton=1'))


def upgrade():install(op.get_bind())


def downgrade():
    # Consumers may already depend on the captured revision fence. An unsafe
    # rollback must not quietly erase this concurrency evidence.
    raise RuntimeError('Current source revision fence requires compatible rollback')
