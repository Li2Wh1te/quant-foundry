"""AR-03 v1 transactional, coalescing source ranges (no payload/event journal).

The migration locks source writers before installing triggers and seeding only
range metadata. A range has an active claim plus a separately coalesced pending
lower bound. Producers never overwrite the consumer's active cursor. Claiming
and acknowledging use row locks, not sequence IDs or wall-clock high watermarks.
"""
from sqlalchemy import text

TABLES = ('tonghuashun_observations', 'tonghuashun_collection_states',
          'etf_daily_bars', 'etf_adjustment_factors')

def install(connection):
    """Test/bootstrap helper runs the same frozen additive migration DDL."""
    from importlib import import_module
    migration=import_module('app.db.migrations.versions.20261008_01_current_source_ranges')
    migration.install(connection)


def seed(connection, source=None, dataset=None):
    """Explicit reconciliation re-enqueues ranges, never copies business data.

    Ordinary updates never call this discovery scan. It is used once during
    migration and by an explicit full reconciliation/rebuild request.
    """
    params={'source':source,'dataset':dataset}
    for table in ('tonghuashun_observations','tonghuashun_collection_states'):
        connection.execute(text(f'''INSERT INTO data_store_source_ranges AS q
          (source,dataset,subject,variant,range_key,lower_at,bootstrap_pending)
          SELECT 'tonghuashun',dataset,subject,variant,'*','-infinity'::timestamptz,true FROM {table}
          WHERE dataset IN ('stock_daily','etf_daily','index_daily')
           AND (CAST(:source AS text) IS NULL OR :source='tonghuashun')
           AND (CAST(:dataset AS text) IS NULL OR dataset=:dataset)
          GROUP BY dataset,subject,variant
          ON CONFLICT(source,dataset,subject,variant,range_key) DO UPDATE
          SET pending=true,bootstrap_pending=true,lower_at='-infinity' '''),params)
    for table,ds in (('etf_daily_bars','etf_daily'),('etf_adjustment_factors','etf_adjustment_factors')):
        if source not in (None,'tushare') or dataset not in (None,ds):continue
        connection.execute(text(f'''INSERT INTO data_store_source_ranges AS q
          (source,dataset,subject,variant,range_key,bootstrap_pending)
          SELECT 'tushare','{ds}',ts_code,'default',left(trade_date::text,7),true FROM {table}
          WHERE source='tushare' GROUP BY ts_code,left(trade_date::text,7)
          ON CONFLICT(source,dataset,subject,variant,range_key) DO UPDATE
          SET pending=true,bootstrap_pending=true,lower_at='-infinity' '''))
