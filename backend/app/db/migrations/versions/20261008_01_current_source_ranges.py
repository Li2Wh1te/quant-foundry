"""Frozen v1 native range capture; additive DDL, no payload copies/reset."""
from alembic import op

revision = '20261008_01'
down_revision = '20261007_01'
branch_labels = None
depends_on = None

from sqlalchemy import text

TABLES = ('tonghuashun_observations', 'tonghuashun_collection_states',
          'etf_daily_bars', 'etf_adjustment_factors')
DDL = r'''
CREATE TABLE data_store_source_ranges (
 source text NOT NULL, dataset text NOT NULL, subject text NOT NULL,
 variant text NOT NULL, range_key text NOT NULL,
 pending boolean NOT NULL DEFAULT true,
 bootstrap_pending boolean NOT NULL DEFAULT false,
 enqueued_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 lower_at timestamptz NOT NULL DEFAULT '-infinity',
 active jsonb,
 PRIMARY KEY(source,dataset,subject,variant,range_key)
);
CREATE INDEX ix_data_store_ranges_work ON data_store_source_ranges
 (source,dataset,enqueued_at,range_key,subject,variant);
CREATE INDEX ix_data_store_ranges_active ON data_store_source_ranges(source,dataset) WHERE active IS NOT NULL;
CREATE TABLE data_store_capture_version (
 singleton integer PRIMARY KEY CHECK(singleton=1), version integer NOT NULL
);
INSERT INTO data_store_capture_version VALUES(1,1);
CREATE FUNCTION data_store_mark_source_range() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v jsonb; src text; ds text; sub text; vr text; rk text; stamp timestamptz;
BEGIN
 FOR v IN SELECT x FROM (VALUES
   (CASE WHEN TG_OP <> 'INSERT' THEN to_jsonb(OLD) END),
   (CASE WHEN TG_OP <> 'DELETE' THEN to_jsonb(NEW) END)) t(x) WHERE x IS NOT NULL LOOP
  IF TG_TABLE_NAME LIKE 'tonghuashun_%' THEN
   IF v->>'dataset' NOT IN ('stock_daily','etf_daily','index_daily') THEN CONTINUE; END IF;
   src := 'tonghuashun'; ds := v->>'dataset'; sub := v->>'subject'; vr := v->>'variant'; rk := '*';
   stamp := CASE WHEN TG_TABLE_NAME='tonghuashun_observations'
                 THEN (v->>'observed_at')::timestamptz ELSE 'infinity'::timestamptz END;
  ELSE
   IF v->>'source' <> 'tushare' THEN CONTINUE; END IF;
   src := 'tushare'; ds := CASE WHEN TG_TABLE_NAME='etf_daily_bars' THEN 'etf_daily' ELSE 'etf_adjustment_factors' END;
   sub := v->>'ts_code'; vr := 'default'; rk := left(v->>'trade_date',7); stamp := '-infinity';
  END IF;
  INSERT INTO data_store_source_ranges AS q(source,dataset,subject,variant,range_key,lower_at)
   VALUES(src,ds,sub,vr,rk,stamp)
   ON CONFLICT(source,dataset,subject,variant,range_key) DO UPDATE
   SET lower_at=CASE WHEN q.pending THEN least(q.lower_at,EXCLUDED.lower_at) ELSE EXCLUDED.lower_at END,
       enqueued_at=CASE WHEN q.pending THEN q.enqueued_at ELSE transaction_timestamp() END,
       pending=true;
 END LOOP;
 RETURN NULL;
END $$;
CREATE FUNCTION data_store_no_source_truncate() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Use DELETE for transactionally captured source ranges'; END $$;
'''


def install(connection):
    """Called by the additive migration; all source DDL and seeding are atomic."""
    connection.execute(text("SET LOCAL lock_timeout='5s'"))
    # Fixed order prevents bootstrap/capture gaps, including transactions that
    # allocated an observation UUID before the migration obtained its locks.
    for table in TABLES:
        connection.execute(text(f'LOCK TABLE {table} IN SHARE ROW EXCLUSIVE MODE'))
    # exec_driver_sql accepts this frozen SQL, including PL/pgSQL semicolons.
    connection.execute(text(DDL))
    for table in TABLES:
        connection.execute(text(f'CREATE TRIGGER data_store_capture_range AFTER INSERT OR UPDATE OR DELETE ON {table} '
                                'FOR EACH ROW EXECUTE FUNCTION data_store_mark_source_range()'))
        connection.execute(text(f'ALTER TABLE {table} ENABLE ALWAYS TRIGGER data_store_capture_range'))
        connection.execute(text(f'CREATE TRIGGER data_store_capture_no_truncate BEFORE TRUNCATE ON {table} '
                                'EXECUTE FUNCTION data_store_no_source_truncate()'))
    connection.execute(text('CREATE INDEX ix_ths_incremental_order ON tonghuashun_observations '
                            '(dataset,subject,variant,observed_at,id)'))
    seed(connection)


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


def upgrade():
    install(op.get_bind())


def downgrade():
    # Capture removal requires an explicit application rollback; silently
    # dropping pending or active work could hide unconsumed native inputs.
    if op.get_bind().execute(text('SELECT EXISTS(SELECT 1 FROM data_store_source_ranges)')).scalar_one():
        raise RuntimeError('Pending native ranges require a compatible application rollback')
    for table in TABLES:
        op.execute(f'DROP TRIGGER data_store_capture_range ON {table}')
        op.execute(f'DROP TRIGGER data_store_capture_no_truncate ON {table}')
    op.execute('DROP INDEX ix_ths_incremental_order')
    op.execute('DROP FUNCTION data_store_mark_source_range()')
    op.execute('DROP FUNCTION data_store_no_source_truncate()')
    op.drop_table('data_store_source_ranges')
    op.drop_table('data_store_capture_version')
