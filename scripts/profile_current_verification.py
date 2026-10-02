#!/usr/bin/env python3
"""Profile full native fingerprint verification with bounded synthetic inputs.

The database must be local, explicitly marked as test, and named *_test. Each
run owns one disposable random schema and one temporary current-store root.
There are no supplier calls or production originals. Empty-current mode reports
every expected object missing. Matched-current mode first builds real Parquet
partitions through the ordinary complete-scan pipeline, then verifies both full
scans under the unchanged 300-second deadline. Neither mode certifies production.
"""
from __future__ import annotations

import argparse
import cProfile
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import pstats
import time
from zoneinfo import ZoneInfo

from sqlalchemy import URL, text

from benchmark_current_store import isolated
from app.data_store.adapters.contracts import LocalInput, digest
from app.data_store.adapters.registry import BY_ID
from app.data_store.limits import StoreLimits
from app.data_store.local_sources import NativeSources, SourceLimits
from app.data_store.errors import DataStoreError
from app.data_store.adapters.canonical import NativeInputError
from app.data_ingestion.models.etf_adjustment import EtfAdjustmentFactor
from app.data_store.tables import entry_status
from app.data_store.pipeline import PipelineOptions, run_entry
from app.data_store.source_range_tables import metadata as capture_metadata
from app.data_store.verify_coverage import verify_existing


class SyntheticSources:
    """Generate only one scalar row or one finite historical window at a time."""

    def __init__(self, rows):
        self.rows = rows
        self.summary = {}

    def iter_entry(self, entry):
        self.summary = {'complete': False, 'input_kind': 'synthetic'}
        observed = datetime(2026, 1, 1, tzinfo=timezone.utc)
        if entry.id == 'E69':
            for index in range(self.rows):
                symbol = f'{index // 2500:06}.SH'
                day = date(2010, 1, 1) + timedelta(days=index % 2500)
                row = {'source': 'tushare', 'ts_code': symbol,
                       'trade_date': day.isoformat(), 'adj_factor': Decimal('1.001')}
                yield LocalInput('tushare', entry.native, symbol, 'default', observed,
                                 row, digest(row), order_kind='current_table_snapshot',
                                 representation='native_table')
        else:
            for offset in range(0, self.rows, 2500):
                symbol = f'{offset // 2500:06}.SZ'
                items = []
                for index in range(min(2500, self.rows - offset)):
                    # Native daily timestamps identify Shanghai midnight, not
                    # UTC midnight. Keep the same date contract as real readers.
                    day = datetime(2010, 1, 1, tzinfo=ZoneInfo('Asia/Shanghai')) + timedelta(days=index)
                    items.append({'thscode': symbol, 'date_ms': int(day.timestamp() * 1000),
                                  'interval': '1d', 'adjust': 'none',
                                  'open_price': Decimal('10'), 'high_price': Decimal('11'),
                                  'low_price': Decimal('9'), 'close_price': Decimal('10.5'),
                                  'volume': 100, 'turnover': Decimal('1000')})
                body = {'thscode': symbol, 'coverage': 'observed_rows_only',
                        'adjust': 'none', 'item': items}
                yield LocalInput('tonghuashun', entry.native, symbol, 'default', observed,
                                 body, digest(body))
        self.summary['complete'] = True


class FixtureSources(NativeSources):
    """Use the formal complete-scan builder for this disposable native fixture.

    The isolated schema has one physical native table, not a migrated production
    capture subsystem. This explicit subclass follows the existing full-scan
    route, preserving real decoding, normalization, partitioning and commits.
    Verification itself uses unmodified NativeSources and its revision fence.
    """


def current_manifest(store, entry):
    """Bounded file metadata proves verification did not rewrite fixture rows."""
    with store.catalog.transaction() as connection:
        refs = connection.execute(text(
            'SELECT partition_key,path,content_hash,row_count,byte_count '
            'FROM data_store_files WHERE dataset=:d ORDER BY partition_key,path'),
            {'d':entry.spec.name}).mappings().all()
    return {'generation':store.catalog.dataset(entry.spec.name)['generation'],
            'files':len(refs),'partitions':len({r['partition_key'] for r in refs}),
            'rows':sum(r['row_count'] for r in refs),
            'bytes':sum(r['byte_count'] for r in refs),
            'file_set_digest':digest([dict(r) for r in refs])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--entry', choices=('E50', 'E69'), required=True)
    parser.add_argument('--rows', type=int, default=20000)
    parser.add_argument('--input', choices=('generated', 'postgres'), default='generated',
                        help='postgres exercises the real E69 server cursor and native row decoding')
    parser.add_argument('--no-profile', action='store_true',
                        help='Measure wall time with the same complete scan and no profiler overhead')
    parser.add_argument('--current', choices=('empty','matched'), default='empty',
                        help='matched builds actual current partitions before the 300-second verification')
    args = parser.parse_args()
    host = os.getenv('QF_DATABASE_HOST', '127.0.0.1')
    database = os.getenv('QF_DATABASE_NAME', 'quant_foundry_test')
    if (os.getenv('QF_ENVIRONMENT') != 'test'
            or host not in ('127.0.0.1', 'localhost', 'postgres')
            or not database.endswith('_test')):
        parser.error('Only an explicit test environment and local *_test database are permitted')
    maximum = 4000000 if args.input == 'postgres' else 250000
    if args.input == 'postgres' and args.entry != 'E69':
        parser.error('The bounded physical-table fixture supports E69 only')
    if args.current == 'matched' and args.input != 'postgres':
        parser.error('Matched-current fixtures require the reviewed physical PostgreSQL input')
    if not 1 <= args.rows <= maximum or not args.root.is_dir() or args.output.exists():
        parser.error(f'Use 1–{maximum} synthetic objects, an existing root and a new output')
    url = URL.create('postgresql+psycopg', host=host,
                     port=int(os.getenv('QF_DATABASE_PORT', '5432')), database=database,
                     username=os.getenv('QF_DATABASE_USER', 'postgres'),
                     password=os.getenv('QF_DATABASE_PASSWORD', ''))
    entry = BY_ID[args.entry]
    profile = cProfile.Profile()
    # Match the deployed finite disk policy, never increase its memory bound.
    # These reservations belong only to the disposable local test store.
    limits = replace(StoreLimits(), scratch_bytes=32*1024**3,
                     pipeline_spill_bytes=16*1024**3)
    with isolated(args.root, url, limits, False) as store:
        fixture_started = time.monotonic()
        with store.catalog.engine.begin() as connection:
            entry_status.create(connection)
            # Include the real capture schema so Tushare verification exercises
            # its ordinary pre-snapshot revision fence, with no fabricated queue.
            capture_metadata.create_all(connection)
            connection.execute(text('INSERT INTO data_store_capture_version VALUES (1,3)'))
            if args.input == 'postgres':
                EtfAdjustmentFactor.__table__.create(connection)
                connection.execute(text("SET LOCAL statement_timeout='120s'"))
                # PostgreSQL generates a finite unique native key set; Python
                # never holds the full fixture. Use the real table's NUMERIC,
                # date, timestamp and source columns, including its constraints.
                connection.execute(text("""
                    INSERT INTO etf_adjustment_factors
                    (source,ts_code,trade_date,adj_factor,created_at,updated_at)
                    SELECT 'tushare',lpad((i/2500)::text,6,'0')||'.SH',
                           DATE '2010-01-01'+(i%2500)::integer,
                           (1+(i%3)::numeric/1000)::numeric(24,12),
                           TIMESTAMPTZ '2026-01-01 00:00:00+00',
                           TIMESTAMPTZ '2026-01-01 00:00:00+00'
                    FROM generate_series(0,CAST(:rows AS bigint)-1) AS fixture(i)
                """), {'rows': args.rows})
        store.register(entry.spec)
        seed_seconds = time.monotonic()-fixture_started
        fixture = {'seed_seconds':seed_seconds,'current_mode':args.current}
        if args.current == 'matched':
            build_started = time.monotonic()
            # The measured 20k/250k tiers project roughly 880 seconds for a
            # 3.3M-row fixture. Preparation has a finite 1200-second ceiling,
            # independent of the UNCHANGED 300-second verification deadline.
            # It is never
            # counted as a relaxed verification deadline or retried on failure.
            try:
                fixture['build_result'] = run_entry(
                    store,entry,FixtureSources(store.catalog.engine,
                                               limits=SourceLimits(pass_seconds=1200)),
                    options=PipelineOptions(mode='rebuild',partitions_per_pass=8,
                                            maximum_passes=4096,pass_seconds=1200),
                    cancelled=lambda:time.monotonic()-build_started>=1200)
            except (DataStoreError,NativeInputError) as error:
                fixture['build_result'] = getattr(error,'entry_result',
                    {'complete':False,'reason':error.code})
            fixture['build_seconds'] = time.monotonic()-build_started
        fixture['before'] = current_manifest(store,entry)
        sources = (NativeSources(store.catalog.engine, limits=SourceLimits(pass_seconds=300))
                   if args.input == 'postgres' else SyntheticSources(args.rows))
        started = time.monotonic()
        if args.current == 'matched' and not fixture['build_result'].get('complete'):
            result = {'entry_id':entry.id,'complete':False,'phase':'fixture_build',
                      'reason':fixture['build_result'].get('reason','FIXTURE_BUILD_INCOMPLETE')}
        else:
            if args.current == 'matched' and fixture['before']['rows'] != args.rows:
                raise AssertionError('Fixture construction did not commit every declared current row')
            try:
                call = verify_existing if args.no_profile else lambda *a, **kw: profile.runcall(verify_existing, *a, **kw)
                result = call(store, entry, sources, seconds=300)
            except (DataStoreError, NativeInputError) as error:
                # A stopped scan is useful diagnosis, never a zero-difference proof.
                # Retain the same incomplete phase/counters as the formal CLI.
                result = {'entry_id': entry.id, 'complete': False, 'reason': error.code,
                          **getattr(error, 'verification', {})}
        elapsed = time.monotonic() - started
        fixture['after'] = current_manifest(store,entry)
        if fixture['before'] != fixture['after']:
            raise AssertionError('Verification rewrote the disposable current fixture')
        snapshot_complete = result.get('expected_objects') == args.rows
        if snapshot_complete and args.current == 'empty' and result['missing_objects'] != args.rows:
            raise AssertionError('Empty synthetic scope was not fully verified')
        if result.get('complete') and (result['current_objects'] != args.rows
                or any(result[k] for k in ('missing_objects','mismatched_objects','unexpected_objects'))):
            raise AssertionError('Matched synthetic scope was not exactly compared')
        if not 0 <= result.get('scanned_objects', 0) <= args.rows:
            raise AssertionError('Synthetic scope exceeded its declared row cap')
        report = io.StringIO()
        if not args.no_profile:
            pstats.Stats(profile, stream=report).strip_dirs().sort_stats('cumulative').print_stats(40)
        output = {'input_kind': 'synthetic', 'production_acceptance': False,
                  'source_path': args.input, 'profile_enabled': not args.no_profile,
                  'entry_id': entry.id, 'objects': args.rows, 'seconds': elapsed,
                  'fixture':fixture,
                  'snapshot_complete': snapshot_complete, 'source_summary': sources.summary,
                  'result': result, 'profile': report.getvalue(),
                  'scratch_released': not store.budget.pending_keys()}
    output['isolated_schema_and_store_removed'] = True
    with args.output.open('x', encoding='utf-8') as handle:
        json.dump(output, handle, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in output.items() if k != 'profile'}))


if __name__ == '__main__':
    main()
