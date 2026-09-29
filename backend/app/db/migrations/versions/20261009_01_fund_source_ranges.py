"""Capture large fund series through the existing bounded range consumer."""
from importlib import import_module
from alembic import op
from sqlalchemy import text

revision='20261009_01'
down_revision='20261008_01'
branch_labels=None
depends_on=None


def install(connection):
    # Replace only the frozen capture function's fixed dataset allowlist. Source
    # rows, current files and existing active range boundaries remain untouched.
    previous=import_module('app.db.migrations.versions.20261008_01_current_source_ranges')
    ddl=previous.DDL.split('CREATE FUNCTION data_store_mark_source_range()',1)[1].split('CREATE FUNCTION data_store_no_source_truncate()',1)[0]
    ddl=('CREATE OR REPLACE FUNCTION data_store_mark_source_range()'+ddl).replace(
        "('stock_daily','etf_daily','index_daily')",
        "('stock_daily','etf_daily','index_daily','fund_manager_performance','fund_nav')")
    connection.execute(text("SET LOCAL lock_timeout='5s'"))
    for table in ('tonghuashun_observations','tonghuashun_collection_states'):
        connection.execute(text(f'LOCK TABLE {table} IN SHARE ROW EXCLUSIVE MODE'))
    connection.execute(text(ddl))
    # Seed only newly supported range identities, never re-enqueue other domains.
    for table in ('tonghuashun_observations','tonghuashun_collection_states'):
        connection.execute(text(f"""INSERT INTO data_store_source_ranges
          (source,dataset,subject,variant,range_key,lower_at,bootstrap_pending)
          SELECT 'tonghuashun',dataset,subject,variant,'*','-infinity'::timestamptz,true
          FROM {table} WHERE dataset IN ('fund_manager_performance','fund_nav')
          GROUP BY dataset,subject,variant
          ON CONFLICT(source,dataset,subject,variant,range_key) DO NOTHING"""))
    connection.execute(text('UPDATE data_store_capture_version SET version=2 WHERE singleton=1'))


def upgrade():install(op.get_bind())


def downgrade():
    c=op.get_bind()
    if c.execute(text("SELECT EXISTS(SELECT 1 FROM data_store_source_ranges WHERE dataset IN ('fund_manager_performance','fund_nav'))")).scalar_one():
        raise RuntimeError('Pending fund ranges require compatible rollback')
    previous=import_module('app.db.migrations.versions.20261008_01_current_source_ranges')
    ddl=previous.DDL.split('CREATE FUNCTION data_store_mark_source_range()',1)[1].split('CREATE FUNCTION data_store_no_source_truncate()',1)[0]
    c.execute(text('CREATE OR REPLACE FUNCTION data_store_mark_source_range()'+ddl))
    c.execute(text('UPDATE data_store_capture_version SET version=1 WHERE singleton=1'))
