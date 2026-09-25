#!/usr/bin/env python3
"""LF-D01 B01–B04, generated bars only, isolated PostgreSQL and local files.

Run with PYTHONPATH=backend and QF_ENVIRONMENT=test. Refuses remote hosts and
non-test databases. Creates/drops ONLY its own random schema and temporary root.
No production/vendor connections and no application runtime registration.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import date, timedelta, datetime, timezone
from decimal import Decimal
import json
import hashlib
import os
from pathlib import Path
import platform
import tempfile
import time
from uuid import uuid4
from unittest.mock import patch

import duckdb
import pyarrow as pa
from sqlalchemy import create_engine, text, URL

from app.data_store import locking
from app.data_store.catalog import DDL, SourceUpdate
from app.data_store.limits import StoreLimits, MiB
from app.data_store.readers import Query
from app.data_store.schema import DatasetSpec, fingerprint
from app.data_store.storage import CurrentStore

TABLES = ('data_store_runtime', 'data_store_datasets', 'data_store_files',
          'data_store_scopes', 'data_store_issues', 'data_store_garbage')
PARTITION_ROWS = 1_000_000
DAYS = 2500
ORIGIN = date(2010, 1, 1)
KEY = b'synthetic-benchmark-only-cursor-secret'


def key(index):
    return f'i{index//DAYS:07}', ORIGIN + timedelta(days=index % DAYS)


def contract(name):
    return DatasetSpec(name, pa.schema([
        pa.field('instrument_id', pa.string(), False, {b'max_utf8_bytes':b'8'}), pa.field('trade_date', pa.date32(), False),
        *[pa.field(n, pa.decimal128(38, 8), False, {b'unit': b'CNY'})
          for n in ('open', 'high', 'low', 'close')],
        pa.field('volume', pa.int64(), False, {b'unit': b'share'}),
    ]), ('instrument_id', 'trade_date'), 'synthetic_r1',
        {'unit': 'CNY', 'representation': 'unadjusted', 'input': 'synthetic-not-market-data'},
        partitioning='synthetic_instruments_400',
        partitioner=lambda k: f'p{int(k[0][1:])//400:04}')


def generated(spec, start, count, *, correction=0, size=16384):
    """Constant-size batches; never constructs the whole requested input."""
    for begin in range(start, start+count, size):
        end = min(start+count, begin+size)
        ids = range(begin, end)
        instruments = pa.array([f'i{i//DAYS:07}' for i in ids], type=pa.string())
        days = pa.array([(ORIGIN-date(1970,1,1)).days + i%DAYS for i in ids], type=pa.date32())
        price = pa.array([Decimal(100000 + i % 10000 + correction).scaleb(-4) for i in ids],
                         type=pa.decimal128(38,8))
        volume = pa.array([100+i%100000 for i in ids], type=pa.int64())
        yield pa.RecordBatch.from_arrays([instruments, days, price, price, price, price, volume],
                                         schema=spec.schema)


def io_sample():
    values = {}
    try:
        for line in Path('/proc/self/io').read_text().splitlines():
            k, v = line.split(':')
            values[k] = int(v)
    except OSError:
        pass
    return values


def elapsed_call(call):
    before = io_sample()
    begin = time.monotonic()
    result = call()
    end = io_sample()
    return result, {'seconds': round(time.monotonic()-begin, 6),
                    'process_io_delta': {k: end[k]-before.get(k,0) for k in end}}


def inspect(st, name):
    with st.catalog.transaction() as c:
        counts = {t: c.execute(text(f'SELECT count(*) FROM {t}')).scalar_one() for t in TABLES}
        # Measurement only, not a production hot path. Catalog contains metadata,
        # never per-business-row normal processing facts.
        relations = {t: c.execute(text('SELECT pg_total_relation_size(to_regclass(:t))'),
                                 {'t': t}).scalar_one() for t in TABLES}
        files = c.execute(text('SELECT path,content_hash,row_count,byte_count FROM data_store_files '
                               'WHERE dataset=:d ORDER BY path'), {'d': name}).mappings().all()
    digest = hashlib.sha256()
    for f in files:
        digest.update((f['path']+' '+f['content_hash']+'\n').encode('ascii'))
    return {'catalog_rows': counts, 'catalog_relation_bytes': relations,
            'rows': st.describe_capability(contract(name))['row_count'],
            'file_count': len(files), 'content_bytes': sum(f['byte_count'] for f in files),
            'current_file_set_hash': digest.hexdigest(),
            'log_bytes': sum(p.stat().st_size for p in (st.files.root/'.logs').iterdir()),
            'scratch_bytes': sum(p.stat().st_size for p in (st.files.root/'.scratch').rglob('*') if p.is_file())}


def put(st, spec, start, count, *, label, correction=0, scope=None):
    partition = f'p{start//PARTITION_ROWS:04}'
    assert count and (start+count-1)//PARTITION_ROWS == start//PARTITION_ROWS
    scope = scope or partition
    previous = st.source_state(spec.name, scope)
    source = SourceUpdate(scope, fingerprint([label,start,count,correction]), fingerprint('synthetic_context'),
                          previous['revision'] if previous else 0,
                          confirmation={'kind': 'synthetic', 'label': label},
                          checkpoint={'begin': start, 'end': start+count})
    return st.upsert(spec, partition, generated(spec,start,count,correction=correction), source,
                     lower=key(start), upper=key(start+count), source_check=lambda _: True)


def metric_summary(results):
    samples = [asdict(r.metrics) for r in results]
    return {k: max(s[k] for s in samples) if k.endswith('_peak_bytes') else sum(s[k] for s in samples)
            for k in samples[0]} if samples else {}


@contextmanager
def isolated(parent, url, limits, inject):
    schema = 'lfd01_bench_' + uuid4().hex
    admin = create_engine(url, connect_args={'connect_timeout': 3})
    with admin.begin() as c:
        c.execute(text(f'CREATE SCHEMA {schema}'))
        c.execute(text(f'SET LOCAL search_path={schema}'))
        for statement in DDL.split(';'):
            if statement.strip():
                c.exec_driver_sql(statement)
    engine = create_engine(url, connect_args={'options': f'-csearch_path={schema}', 'connect_timeout': 3})
    try:
        with tempfile.TemporaryDirectory(prefix='lfd01-bench-', dir=parent) as root:
            ctx = patch.object(locking, '_filesystem_name', return_value='ext4') if inject else _nothing()
            with ctx, CurrentStore(engine, root, cursor_key=KEY, limits=limits, initialize=True) as st:
                yield st
    finally:
        engine.dispose()
        with admin.begin() as c:
            # Only this invocation's generated schema, no broad public reset.
            c.execute(text(f'DROP SCHEMA {schema} CASCADE'))
        admin.dispose()


@contextmanager
def _nothing():
    yield


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path, help='Existing trusted LOCAL synthetic-test directory')
    parser.add_argument('--output', required=True, type=Path, help='New result JSON; refuses overwrite')
    parser.add_argument('--allow-overlay-test', action='store_true', help='Explicit unsupported-FS development mechanics only')
    args = parser.parse_args()
    host, db = os.getenv('QF_DATABASE_HOST','127.0.0.1'), os.getenv('QF_DATABASE_NAME','quant_foundry_test')
    if os.getenv('QF_ENVIRONMENT') != 'test' or host not in ('127.0.0.1','localhost','postgres') or not db.endswith('_test'):
        parser.error('Only explicit test environment and a local *_test database are permitted')
    if not args.root.is_dir() or args.output.exists():
        parser.error('Require existing local root and a new output path')
    cpus = sorted(os.sched_getaffinity(0))[:4]
    os.sched_setaffinity(0, cpus)
    url = URL.create('postgresql+psycopg', username=os.getenv('QF_DATABASE_USER','postgres'),
                     password=os.getenv('QF_DATABASE_PASSWORD',''), host=host,
                     port=int(os.getenv('QF_DATABASE_PORT','5432')), database=db)
    limits = replace(StoreLimits(), batch_rows=16384, batch_bytes=2*MiB)
    fd = os.open(args.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        actual_fs = locking._filesystem_name(fd)
    finally:
        os.close(fd)
    report = {'task': 'LF-D01-v1.1', 'synthetic': True, 'started_at': datetime.now(timezone.utc).isoformat(),
              'environment': {'python': platform.python_version(), 'arrow': pa.__version__, 'duckdb': duckdb.__version__,
                              'cpu_affinity': cpus, 'filesystem': actual_fs,
                              'filesystem_probe_injected': args.allow_overlay_test,
                              'cgroup_memory_max': Path('/sys/fs/cgroup/memory.max').read_text().strip()
                              if Path('/sys/fs/cgroup/memory.max').exists() else 'unknown',
                              'postgres_in_same_isolated_runtime': True},
              'limits': asdict(limits), 'results': [], 'complete': False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    handle = args.output.open('x', encoding='utf8')

    def save():
        handle.seek(0)
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.truncate(); handle.flush(); os.fsync(handle.fileno())

    def record(value):
        report['results'].append(value); save()
        print(json.dumps({'case':value['case'], 'size':value.get('size'), 'seconds':value.get('seconds'),
                          'passed':value['passed']}, ensure_ascii=False), flush=True)

    try:
        for n in (100_000,1_000_000,10_000_000):
            with isolated(args.root,url,limits,args.allow_overlay_test) as st:
                spec = contract('bars')
                st.register(spec)
                begin = time.monotonic(); initial_io = io_sample(); results = []
                for start in range(0,n,PARTITION_ROWS):
                    results.append(put(st,spec,start,min(n-start,PARTITION_ROWS),label='baseline'))
                snap = inspect(st,'bars'); current_io = io_sample()
                assert snap['rows'] == n and snap['catalog_rows']['data_store_issues'] == 0
                assert snap['catalog_rows']['data_store_scopes'] == (n+PARTITION_ROWS-1)//PARTITION_ROWS
                assert snap['catalog_rows']['data_store_garbage'] == 0
                assert snap['file_count'] <= (n+limits.file_rows-1)//limits.file_rows + 10
                record({'case':'B01', 'size':n, 'passed':True, 'seconds':round(time.monotonic()-begin,6),
                        'metrics':metric_summary(results), 'state':snap,
                        'process_io_delta':{k:current_io[k]-initial_io[k] for k in current_io}})
                if n == 1_000_000:
                    baseline = snap
                    for count in (1,10,100):
                        results = []; begin = time.monotonic()
                        for _ in range(count):
                            r = put(st,spec,0,n,label='baseline')
                            assert r.idempotent and r.metrics.input_bytes == 0 and r.metrics.written_bytes == 0
                            results.append(r)
                            st.cleanup('bars')
                        now = inspect(st,'bars')
                        assert all(now[k] == baseline[k] for k in ('rows','file_count','current_file_set_hash','catalog_rows'))
                        assert now['log_bytes'] <= limits.log_bytes
                        record({'case':'B02','size':count,'passed':True,'seconds':round(time.monotonic()-begin,6),
                                'metrics':metric_summary(results),'state':now})
                    for kind,start,amount,correction in [('append',n,n//100,0), ('correct',0,n//1000,100)]:
                        r, timing = elapsed_call(lambda:put(st,spec,start,amount,label=kind,correction=correction,
                                                           scope=kind))
                        assert r.metrics.files_read <= 1 and r.metrics.files_written <= 1
                        assert r.metrics.current_read_bytes < baseline['content_bytes']
                        now = inspect(st,'bars')
                        assert now['rows'] == n+n//100
                        record({'case':'B03','size':amount,'kind':kind,'passed':True,**timing,
                                'metrics':asdict(r.metrics),'state':now})
        with isolated(args.root,url,limits,args.allow_overlay_test) as st:
            spec = contract('bars'); st.register(spec)
            put(st,spec,0,100,label='target')
            last = 0; sizes = []
            for backgrounds in (10,100,1000):
                for p in range(last+1,backgrounds+1):
                    put(st,spec,p*PARTITION_ROWS,100,label='background')
                r,timing = elapsed_call(lambda:put(st,spec,0,1,label='fixed-update',correction=999,scope='target-update'))
                assert r.metrics.files_read == 1 and r.metrics.files_written == 1
                sizes.append((r.metrics.current_read_bytes,r.metrics.written_bytes))
                record({'case':'B04','size':backgrounds,'passed':True,**timing,
                        'metrics':asdict(r.metrics),'state':inspect(st,'bars')})
                # Reset the same target state between sizes; background count is
                # the only independent variable in the recorded update above.
                put(st,spec,0,1,label='reset-target',correction=0,scope='target-update')
                last = backgrounds
            assert all(s == sizes[0] for s in sizes), sizes
        report['complete'] = True
    except BaseException as error:
        report['error'] = {'type':type(error).__name__, 'code':getattr(error,'code','BENCHMARK_FAILED')}
        raise
    finally:
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        save(); handle.close()


if __name__ == '__main__':
    main()
