#!/usr/bin/env python3
"""LF-D02 B05 synthetic adapter+pipeline exercise, NOT a supplier integration.

Default: ten million streamed events with duplicate nanosecond timestamps and
independent sequence identities. One source range contains <=100,000 events;
source ranges, not individual events, enter the ordinary current-store commit.
All IO targets a newly allocated schema and directory on an isolated local DB.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine,text,URL
from app.data_store.adapters.contracts import LocalInput,digest
from app.data_store.adapters.normalize import normalize
from app.data_store.adapters.registry import SYNTHETIC
from app.data_store.catalog import DDL
from app.data_store.limits import StoreLimits
from app.data_store.pipeline import PipelineOptions,run_entry
from app.data_store.readers import Query
from app.data_store.storage import CurrentStore
from app.data_store.tables import entry_status
from app.data_store import locking

STAMP=datetime(2026,1,1,tzinfo=timezone.utc)
PRICE=Decimal('12345678901234567890.123456789012345678')
START_NS=1767225600000000123
ENTRY=SYNTHETIC[0]

def catalog_files(store):
    with store.catalog.transaction() as c:
        records=c.execute(text('SELECT path,content_hash,partition_key FROM data_store_files WHERE dataset=:d ORDER BY path LIMIT 10001'),{'d':ENTRY.spec.name}).mappings().all()
    if len(records)>10000:raise RuntimeError('Benchmark file-count budget exceeded')
    return records


def event(sequence,*,subject='SYN.SH',changed=False):
    body=dict(source_code=subject,trading_date='2026-01-01',session='continuous',channel='A',
              sequence=sequence,event_ns=START_NS+sequence//2,price=PRICE,quantity=2 if changed else 1,
              currency='CNY',price_basis='raw')
    observed=STAMP.replace(second=1 if changed else 0)
    return LocalInput('synthetic','tick',subject,'default',observed,body,digest([body,observed]))


class Range:
    def __init__(self,start,end,*,subject='SYN.SH',changed=False):
        self.start,self.end,self.subject,self.changed=start,end,subject,changed
        self.summary={}
    def iter_entry(self,entry):
        if entry.id!='B05':raise ValueError('Synthetic source is not a production adapter')
        self.summary={'complete':False,'scope':'explicit_synthetic_sequence_range'}
        for n in range(self.start,self.end):yield event(n,subject=self.subject,changed=self.changed)
        self.summary.update(complete=True,source_rows=self.end-self.start)


def execute(store,total,cold_count):
    result={'case':'B05','requested_rows':total,'complete':False,'full_B05_executed':total==10000000,
            'supplier_connected':False,'production_executed':False,'atomic_ranges':0,'metrics':{}}
    def process(source):
        raw=event(source.start,subject=source.subject,changed=source.changed)
        unit=next(normalize(ENTRY,raw));partition=ENTRY.spec.partitioner((*unit.key,'root'))
        outcome=run_entry(store,ENTRY,source,options=PipelineOptions(partitions=(partition,),pass_seconds=300))
        if not outcome.get('complete') or not outcome.get('qualified'):
            raise RuntimeError('Synthetic range incomplete or restricted')
        result['atomic_ranges']+=1
        for k,v in outcome['metrics'].items():
            result['metrics'][k]=max(result['metrics'].get(k,0),v) if k.endswith('peak_bytes') else result['metrics'].get(k,0)+v
        return partition
    for n in range(cold_count):process(Range(0,1,subject=f'COLD{n:04}.SH'))
    cold={r['path']:r['content_hash'] for r in catalog_files(store)}
    started=time.monotonic()
    for first in range(0,total,100000):
        process(Range(first,min(first+100000,total)))
    result['stream_seconds']=time.monotonic()-started
    # Check exact large Decimal and duplicate timestamp/distinct event identities.
    first=next(normalize(ENTRY,event(0)))
    p=ENTRY.spec.partitioner((*first.key,'root'))
    sample=store.read(ENTRY.spec,Query(partitions=(p,),lower=first.key,page_size=2)).rows
    assert len(sample)==min(total,2)
    assert all(r['f0_price']==str(PRICE) for r in sample)
    assert all(str(r['f0_event_ns'])==str(START_NS) for r in sample)
    if len(sample)==2:assert sample[0]['f0_sequence']!=sample[1]['f0_sequence']
    hot=(total-1)//100000*100000
    hot_part=process(Range(hot,min(total,hot+100),changed=True))
    after={r['path']:r['content_hash'] for r in catalog_files(store)}
    # Cold test inputs may hash to the same bucket but are never in the changed
    # sequence partition when hot>0. Compare unaffected actual catalog paths.
    unchanged={r['path']:r['content_hash'] for r in catalog_files(store) if r['partition_key']!=hot_part}
    if hot>0:assert all(after.get(k)==v for k,v in cold.items())
    with store.catalog.transaction() as c:
        count=c.execute(text('SELECT coalesce(sum(row_count),0) FROM data_store_files WHERE dataset=:d'),{'d':ENTRY.spec.name}).scalar_one()
    assert count==total+cold_count
    result.update(complete=True,current_rows=count,current_files=len(after),cold_fixture_files=len(cold),
                  unchanged_partition_files=len(unchanged),exact_event_samples=True,
                  limits=asdict(store.limits))
    return result


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--rows',type=int,default=10000000)
    p.add_argument('--cold-partitions',type=int,default=10)
    p.add_argument('--work-dir',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args(argv)
    host=os.environ.get('QF_DATABASE_HOST','127.0.0.1')
    if os.environ.get('QF_ENVIRONMENT')!='test' or host not in ('127.0.0.1','localhost','postgres'):
        p.error('Only explicitly configured isolated local test databases are accepted')
    if not 2<=args.rows<=10000000 or not 0<=args.cold_partitions<=100:p.error('Finite fixture bounds required')
    if not args.work_dir.is_absolute() or args.output.exists():p.error('Absolute work directory and new output required')
    url=URL.create('postgresql+psycopg',username=os.getenv('QF_DATABASE_USER','postgres'),
        password=os.getenv('QF_DATABASE_PASSWORD',''),host=host,port=int(os.getenv('QF_DATABASE_PORT','5432')),
        database=os.getenv('QF_DATABASE_NAME','quant_foundry_test'))
    schema='lfd02_b05_'+uuid4().hex
    root=args.work_dir/schema;root.mkdir(parents=True,mode=0o700)
    admin=create_engine(url);engine=None;created=False
    result={'case':'B05','complete':False,'production_executed':False,'full_B05_executed':False}
    try:
        with admin.begin() as c:
            c.execute(text(f'CREATE SCHEMA {schema}'));created=True
            c.execute(text(f'SET LOCAL search_path={schema}'))
            for statement in DDL.split(';'):
                if statement.strip():c.exec_driver_sql(statement)
            entry_status.create(c)
        engine=create_engine(url,connect_args={'options':f'-csearch_path={schema}','connect_timeout':3})
        # Real supported mount required. This harness contains no probe injection.
        limits=replace(StoreLimits(),duckdb_threads=1)
        with CurrentStore(engine,root,cursor_key=b'isolated-b05-key-not-production-32chars',initialize=True,limits=limits) as store:
            result=execute(store,args.rows,args.cold_partitions)
            result['filesystem']=locking._filesystem_name(store.files.fd)
            result['filesystem_probe_injected']=False
    except Exception as error:
        result.update(complete=False,reason=getattr(error,'code',type(error).__name__))
    finally:
        if engine:engine.dispose()
        if created:
            with admin.begin() as c:c.execute(text(f'DROP SCHEMA {schema} CASCADE'))
        admin.dispose()
    with args.output.open('x',encoding='utf-8') as handle:json.dump(result,handle,ensure_ascii=False,indent=2)
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result['complete'] else 2


if __name__=='__main__':sys.exit(main())
