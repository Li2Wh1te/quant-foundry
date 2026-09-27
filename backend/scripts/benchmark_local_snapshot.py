#!/usr/bin/env python3
"""Isolated natural full-source benchmark; no caller-selected partitions.

Use a real local PostgreSQL calendar table and a supported filesystem. Each
invocation reopens CurrentStore so progress cannot depend on a Python cursor.
Only a randomly named test schema is created/dropped; suppliers are never used.
"""
import argparse
import json
import os
from pathlib import Path
import resource
import time
from uuid import uuid4

from sqlalchemy import URL, create_engine, text

from app.data_store.adapters.registry import BY_ID
from app.data_store.catalog import DDL
from app.data_store.local_sources import NativeSources
from app.data_store.pipeline import run_local
from app.data_store.storage import CurrentStore
from app.data_store.tables import entry_status


def execute(engine, root, partitions):
    entry=BY_ID['E68']
    runs=[]
    previous={}
    started=time.monotonic()
    peak_pending=0
    for number in range((partitions+255)//256):
        with CurrentStore(engine,root,cursor_key=b'isolated-natural-scan-key-32-characters',
                          initialize=number==0) as store:
            result=run_local(store,NativeSources(engine),entries=[entry])[0]
            with store.catalog.transaction() as c:
                files=dict(c.execute(text('SELECT path,content_hash FROM data_store_files')).all())
                rows=int(c.execute(text('SELECT coalesce(sum(row_count),0) FROM data_store_files')).scalar_one())
            assert previous.items() <= files.items(), 'A completed partition was rewritten'
            previous=files
            pending=sum(p.stat().st_size for p in root.glob('.scratch/*/spill/*') if p.is_file())
            peak_pending=max(peak_pending,pending)
            runs.append({k:result.get(k) for k in ('complete','resumed','source_rows','normalized_units',
                          'scan_source_rows','committed_partitions','last_partition','metrics')})
            if number:
                assert result['resumed'] and result['source_rows']==result['normalized_units']==0
                assert runs[-1]['last_partition'] > runs[-2]['last_partition']
    assert runs[-1]['complete']
    assert sum(r['source_rows'] for r in runs)==partitions*10
    assert sum(r['normalized_units'] for r in runs)==partitions*10
    assert sum(r['committed_partitions'] for r in runs)==partitions
    assert rows==partitions*10 and len(previous)==partitions
    assert not list(root.glob('.scratch/*/spill/*.sqlite'))
    return {'case':'LF-01-natural-full-source','complete':True,'production_executed':False,
            'partitions':partitions,'input_rows':partitions*10,'current_rows':rows,
            'current_files':len(previous),'invocations':runs,'manual_partition_selection':False,
            'committed_files_unchanged_on_resume':True,'pending_reclaimed':True,
            'peak_pending_bytes_at_invocation_boundary':peak_pending,
            'rss_peak_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
            'seconds':time.monotonic()-started,'supplier_connected':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--partitions',type=int,default=1000)
    parser.add_argument('--work-dir',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    host=os.getenv('QF_DATABASE_HOST','127.0.0.1')
    database=os.getenv('QF_DATABASE_NAME','quant_foundry_test')
    if (os.getenv('QF_ENVIRONMENT')!='test' or host not in ('127.0.0.1','localhost','postgres')
            or not database.endswith('_test')):
        parser.error('Require an isolated local test database')
    if not 257<=args.partitions<=1000 or not args.work_dir.is_absolute() or args.output.exists():
        parser.error('Require 257..1000 natural partitions, an absolute root and a new output')
    url=URL.create('postgresql+psycopg',username=os.getenv('QF_DATABASE_USER','postgres'),
                   password=os.getenv('QF_DATABASE_PASSWORD',''),host=host,
                   port=int(os.getenv('QF_DATABASE_PORT','5432')),database=database)
    schema='lf_review_scan_'+uuid4().hex
    root=args.work_dir/schema
    root.mkdir(parents=True,mode=0o700)
    admin=create_engine(url)
    engine=None
    created=False
    result={'case':'LF-01-natural-full-source','complete':False,'production_executed':False}
    try:
        with admin.begin() as c:
            c.execute(text(f'CREATE SCHEMA {schema}'))
            created=True
            c.execute(text(f'SET LOCAL search_path={schema}'))
            for statement in DDL.split(';'):
                if statement.strip():c.exec_driver_sql(statement)
            entry_status.create(c)
            c.execute(text('CREATE TABLE trading_calendar_days (exchange text,calendar_date date,is_open boolean,PRIMARY KEY(exchange,calendar_date))'))
            c.execute(text("INSERT INTO trading_calendar_days SELECT 'SSE', "
                           "(DATE '1900-01-01'+make_interval(months=>m))::date+d,true "
                           "FROM generate_series(0,:last) m CROSS JOIN generate_series(0,9) d"),
                      {'last':args.partitions-1})
        engine=create_engine(url,connect_args={'options':f'-csearch_path={schema}'})
        result=execute(engine,root,args.partitions)
    except Exception as error:
        result['reason']=getattr(error,'code',type(error).__name__)
        raise
    finally:
        if engine:engine.dispose()
        if created:
            with admin.begin() as c:c.execute(text(f'DROP SCHEMA {schema} CASCADE'))
        admin.dispose()
        with args.output.open('x') as handle:json.dump(result,handle,indent=2)
    print(json.dumps(result))


if __name__=='__main__':
    main()
