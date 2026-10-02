#!/usr/bin/env python3
"""Profile full native fingerprint verification with bounded synthetic inputs.

The database must be local, explicitly marked as test, and named *_test. Each
run owns one disposable random schema and one temporary current-store root.
There are no supplier calls or production originals. The deliberately empty
current dataset makes the full expected-key scan report missing objects; this
profiles the native_snapshot phase without pretending to certify acceptance.
"""
from __future__ import annotations

import argparse
import cProfile
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
from app.data_store.tables import entry_status
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--entry', choices=('E50', 'E69'), required=True)
    parser.add_argument('--rows', type=int, default=20000)
    args = parser.parse_args()
    host = os.getenv('QF_DATABASE_HOST', '127.0.0.1')
    database = os.getenv('QF_DATABASE_NAME', 'quant_foundry_test')
    if (os.getenv('QF_ENVIRONMENT') != 'test'
            or host not in ('127.0.0.1', 'localhost', 'postgres')
            or not database.endswith('_test')):
        parser.error('Only an explicit test environment and local *_test database are permitted')
    if not 1 <= args.rows <= 250000 or not args.root.is_dir() or args.output.exists():
        parser.error('Use 1–250000 synthetic objects, an existing root and a new output')
    url = URL.create('postgresql+psycopg', host=host,
                     port=int(os.getenv('QF_DATABASE_PORT', '5432')), database=database,
                     username=os.getenv('QF_DATABASE_USER', 'postgres'),
                     password=os.getenv('QF_DATABASE_PASSWORD', ''))
    entry = BY_ID[args.entry]
    profile = cProfile.Profile()
    with isolated(args.root, url, StoreLimits(), False) as store:
        with store.catalog.engine.begin() as connection:
            entry_status.create(connection)
            # Include the real capture schema so Tushare verification exercises
            # its ordinary pre-snapshot revision fence, with no fabricated queue.
            capture_metadata.create_all(connection)
            connection.execute(text('INSERT INTO data_store_capture_version VALUES (1,3)'))
        store.register(entry.spec)
        started = time.monotonic()
        result = profile.runcall(verify_existing, store, entry, SyntheticSources(args.rows), seconds=300)
        elapsed = time.monotonic() - started
        if result['expected_objects'] != args.rows or result['missing_objects'] != args.rows:
            raise AssertionError('Synthetic scope was not fully verified')
        report = io.StringIO()
        pstats.Stats(profile, stream=report).strip_dirs().sort_stats('cumulative').print_stats(25)
        output = {'input_kind': 'synthetic', 'production_acceptance': False,
                  'entry_id': entry.id, 'objects': args.rows, 'seconds': elapsed,
                  'result': result, 'profile': report.getvalue(),
                  'scratch_released': not store.budget.pending_keys()}
    with args.output.open('x', encoding='utf-8') as handle:
        json.dump(output, handle, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in output.items() if k != 'profile'}))


if __name__ == '__main__':
    main()
