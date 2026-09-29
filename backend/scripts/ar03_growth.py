"""Reproducible AR-03 native growth experiment; isolated test PostgreSQL only.

Run inside the supported-filesystem test container. Each update is a fresh
process using run_local (the default CLI/scheduler dispatcher), never a caller-
supplied Range or partition. JSON separates transfer/decode work from EXPLAIN
buffer evidence; source objects and reconstructed business rows are distinct.
"""
from __future__ import annotations
import argparse
import hashlib
from dataclasses import asdict, replace
from datetime import datetime,timezone,timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import time
import threading
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine,text
from sqlalchemy.orm import Session
from tests.test_data_store_kernel import test_url,make_engine,KEY
from tests.test_data_store_domain_samples import sample
from app.data_store.catalog import DDL
from app.data_store.tables import entry_status
from app.data_store.storage import CurrentStore
from app.data_store.pipeline import run_local,PipelineOptions
from app.data_store.local_sources import NativeSources
from app.data_store.adapters.registry import BY_ID
from app.data_store.adapters.contracts import native_json
from app.data_store.limits import StoreLimits,MiB
from app.data_store.change_capture import install
from app.data_ingestion.models.tonghuashun import TonghuashunObservation as O,TonghuashunCollectionState as S
from app.data_ingestion.models.etf_daily import EtfDailyBar as B
from app.data_ingestion.models.etf_adjustment import EtfAdjustmentFactor as F
from app.data_ingestion.tonghuashun.repository import CollectionRepository

POLICY=replace(StoreLimits(),pipeline_spill_bytes=64*MiB,duckdb_memory_bytes=512*MiB,
               file_rows=10000,orphan_grace_seconds=1)
ENTRIES=[BY_ID['E50'],BY_ID['E70']]
HOT='600001.SH'
NOW=datetime(2026,1,2,tzinfo=timezone.utc)
DAY=1767283200000
EARLY=int(datetime(2018,1,2,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()*1000)


def publish(engine,subject,days,*,second=0,close='10.5'):
    body=sample('E50').content
    body['thscode']=subject
    template=body['item'][0]
    body['item']=[dict(template,thscode=subject,date_ms=day,close_price=Decimal(close),high_price=Decimal('30')) for day in days]
    with Session(engine) as s,s.begin():
        repo=CollectionRepository(s);old=repo.read('stock_daily',subject,'default',with_data=False)
        repo.publish('stock_daily',subject,'default',expected=old.revision,data=body,
            requests=[{'parameters':{},'key_receipt':'actual_returned_keys_v1','returned_keys':{'date_ms':days}}],now=NOW+timedelta(seconds=second))


class ScratchSampler:
    """Sample the whole current process's scratch directory every 20 ms."""
    def __init__(self,root):
        self.root=root;self.peak=0;self.stop=threading.Event()
        self.thread=threading.Thread(target=self.sample,daemon=True);self.thread.start()
    def sample(self):
        while not self.stop.wait(.02):
            total=0
            for p in (self.root/'.scratch').rglob('*'):
                try:
                    if p.is_file():total+=p.stat().st_size
                except FileNotFoundError:pass
            self.peak=max(self.peak,total)
    def close(self):self.stop.set();self.thread.join()


def worker(schema,root):
    started=time.monotonic();engine=make_engine(schema);sampler=ScratchSampler(root)
    with CurrentStore(engine,root,cursor_key=KEY,initialize=True,limits=POLICY) as st:
        results=run_local(st,NativeSources(engine),entries=ENTRIES,options=PipelineOptions())
        for e in ENTRIES:st.cleanup(e.spec.name)
        with engine.connect() as c:
            state={
                'current_files':c.execute(text('SELECT count(*) FROM data_store_files')).scalar_one(),
                'current_rows':int(c.execute(text('SELECT coalesce(sum(row_count),0) FROM data_store_files')).scalar_one()),
                'pending_ranges':c.execute(text('SELECT count(*) FROM data_store_source_ranges')).scalar_one(),
                'control_bytes':c.execute(text('SELECT coalesce(sum(octet_length(summary_json)),0) FROM data_store_entry_status')).scalar_one(),
                'control_rows':c.execute(text('SELECT count(*) FROM data_store_entry_status')).scalar_one(),
                'pending_metadata_physical_bytes':c.execute(text("SELECT pg_total_relation_size('data_store_source_ranges')")).scalar_one(),
                'garbage_files':c.execute(text('SELECT count(*) FROM data_store_garbage')).scalar_one(),
                'generation':dict(c.execute(text('SELECT name,generation FROM data_store_datasets')).all()),
            }
        state['parquet_files_on_disk']=sum(1 for _ in root.rglob('*.parquet'))
        state['scratch_bytes_sampled']=sum(p.stat().st_size for p in (root/'.scratch').rglob('*') if p.is_file())
    engine.dispose();sampler.close()
    state['scratch_peak_bytes_sampled_20ms']=sampler.peak
    return {'entries':results,'state':state,'elapsed_seconds':time.monotonic()-started,
            'rss_peak_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024}


def invoke(schema,root):
    process=subprocess.run([sys.executable,__file__,'--worker',schema,'--root',str(root)],capture_output=True,text=True)
    if process.returncode:
        raise RuntimeError(process.stderr[-3000:]+process.stdout[-3000:])
    return json.loads(process.stdout.splitlines()[-1])


def drain(schema,root):
    passes=[]
    for number in range(1000):
        r=invoke(schema,root);passes.append(r)
        print(f'{schema} bootstrap pass {number+1}: pending={r["state"]["pending_ranges"]}, rows={r["state"]["current_rows"]}',flush=True)
        if all(e.get('complete') and e.get('qualified') for e in r['entries']):return passes
        if any(e.get('outcome')=='failed' for e in r['entries']):raise AssertionError(r)
    raise AssertionError('Native bootstrap did not converge')


def explain(engine):
    queries={
      'metadata_discovery':"SELECT source,dataset,subject,variant,range_key FROM data_store_source_ranges WHERE source='tushare' AND dataset='etf_daily' ORDER BY enqueued_at,range_key,subject,variant LIMIT 64",
      'tushare_range':f"SELECT * FROM etf_daily_bars WHERE source='tushare' AND ts_code='{HOT}' AND trade_date>='2026-01-01' AND trade_date<'2026-02-01' ORDER BY trade_date",
      'ths_observation_range':f"SELECT observed_at,id FROM tonghuashun_observations WHERE dataset='stock_daily' AND subject='{HOT}' AND variant='default' AND observed_at>='2026-01-02' ORDER BY observed_at,id LIMIT 1"}
    with engine.begin() as c:
        c.execute(text('ANALYZE'))
        return {key:c.execute(text('EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) '+query)).scalar_one() for key,query in queries.items()}


def seed(engine,rows):
    days=rows//100
    dates=[int((datetime(1990,1,1)+timedelta(days=n)).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()*1000) for n in range(days)]
    for subject in range(100):
        publish(engine,f'{subject+100:06}.SZ',dates)
    # Supported bulk SQL DML shares the same transactional trigger as the
    # production ETF repository. No hand-selected consumer partitions exist.
    with engine.begin() as c:
        c.execute(text("""INSERT INTO etf_daily_bars(source,ts_code,trade_date,open,high,low,close,vol,amount)
          SELECT 'tushare',lpad((s+100)::text,6,'0')||'.SZ',DATE '1990-01-01'+d,4,9,1,4.15,100,200
          FROM generate_series(0,99) s CROSS JOIN generate_series(0,:days-1) d"""),{'days':days})
        c.execute(text(f"INSERT INTO etf_daily_bars(source,ts_code,trade_date,open,high,low,close,vol,amount) VALUES ('tushare','{HOT}','2018-01-02',4,30,1,4.15,100,200),('tushare','{HOT}','2026-01-02',4,30,1,4.15,100,200)"))
    publish(engine,HOT,[EARLY,DAY])


def experiment(rows,root,rounds=100):
    schema='ar03_growth_'+uuid4().hex;admin=create_engine(test_url())
    with admin.begin() as c:
        c.execute(text(f'CREATE SCHEMA {schema}'));c.execute(text(f'SET LOCAL search_path={schema}'))
        for stmt in DDL.split(';'):
            if stmt.strip():c.exec_driver_sql(stmt)
        entry_status.create(c)
        for model in (O,S,B,F):model.__table__.create(c)
        install(c)
    engine=make_engine(schema);root.mkdir(mode=0o700,parents=True)
    try:
        seed(engine,rows)
        result={'cold_business_rows_per_path':rows,'policy':asdict(POLICY),'bootstrap':drain(schema,root)}
        assert result['bootstrap'][-1]['state']['current_rows']==rows*2+4
        zero=[]
        for _ in range(rounds):
            r=invoke(schema,root)
            assert all(e['source_metrics']['payload_rows']==e['source_metrics']['dependency_rows']==e['normalized_units']==e['metrics']['files_written']==0 for e in r['entries'])
            assert r['state']['generation']==result['bootstrap'][-1]['state']['generation']
            zero.append(r)
        result['G01_no_change_100']=zero
        with engine.begin() as c:c.execute(text(f"INSERT INTO etf_daily_bars(source,ts_code,trade_date,open,high,low,close,vol,amount) VALUES ('tushare','{HOT}','2026-01-03',4,30,1,4.15,100,200)"))
        publish(engine,HOT,[EARLY,DAY,DAY+86400000],second=1)
        result['G02_small_append']=invoke(schema,root)
        with engine.begin() as c:
            before=dict(c.execute(text("SELECT path,content_hash FROM data_store_files WHERE partition_key<'2018'")).all())
            c.execute(text(f"UPDATE etf_daily_bars SET close=6 WHERE ts_code='{HOT}' AND trade_date='2018-01-02'"))
            c.execute(text(f"INSERT INTO etf_daily_bars(source,ts_code,trade_date,open,high,low,close,vol,amount) VALUES ('tushare','{HOT}','2019-01-01',4,30,1,4.15,100,200)"))
            c.execute(text(f"DELETE FROM etf_daily_bars WHERE ts_code='{HOT}' AND trade_date='2026-01-03'"))
        late=int(datetime(2019,1,1,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()*1000)
        publish(engine,HOT,[EARLY,late,DAY],second=2,close='12')
        result['G03_correction_late_delete']=invoke(schema,root)
        with engine.connect() as c:
            assert dict(c.execute(text("SELECT path,content_hash FROM data_store_files WHERE partition_key<'2018'")).all())==before
        updates=[]
        for n in range(rounds):
            price=str(10+n%2)
            publish(engine,HOT,[EARLY,late,DAY],second=n+3,close=price)
            with engine.begin() as c:c.execute(text(f"UPDATE etf_daily_bars SET close=:v WHERE ts_code='{HOT}' AND trade_date='2018-01-02'"),{'v':Decimal(price)})
            r=invoke(schema,root);assert r['state']['pending_ranges']==0;updates.append(r)
        result['G05_updates_100']=updates;result['explain']=explain(engine)
        assert updates[-1]['state']['control_rows']==zero[0]['state']['control_rows']==2
        assert updates[-1]['state']['control_bytes']<32768
        return result
    finally:
        engine.dispose()
        with admin.begin() as c:c.execute(text(f'DROP SCHEMA {schema} CASCADE'))
        admin.dispose()


def validate_growth(result):
    """Compare stable entry identities; fair scheduling may reorder results.

    A saved report contains all per-process measurements, so a reporting-only
    correction can revalidate them without replacing the measured source digest.
    """
    scales=result['scales']
    assert len(scales)==2
    assert [s['cold_business_rows_per_path'] for s in scales]==[100000,1000000]
    assert scales[0]['policy']==scales[1]['policy']
    for scale in scales:
        baseline=scale['bootstrap'][-1]
        assert baseline['state']['current_rows']==2*scale['cold_business_rows_per_path']+4
        assert all(e['complete'] and e['qualified'] for e in baseline['entries'])
        for name in ('G01_no_change_100','G05_updates_100'):
            assert len(scale[name])==100
            for run in scale[name]:
                assert run['state']['pending_ranges']==0
                assert run['state']['control_rows']==2
                assert run['state']['control_bytes']<32768
                assert all(e['complete'] and e['qualified'] for e in run['entries'])
        for run in scale['G01_no_change_100']:
            assert run['state']['generation']==baseline['state']['generation']
            for entry in run['entries']:
                assert entry['normalized_units']==entry['metrics']['files_written']==0
                assert all(entry['source_metrics'][k]==0 for k in
                    ('payload_rows','payload_bytes','dependency_rows','dependency_bytes','decoded_rows','decode_calls'))
        final=scale['G05_updates_100'][-1]['state']
        assert final['garbage_files']==0
        assert final['parquet_files_on_disk']==final['current_files']
        for entry_id in ('E50','E70'):
            entries=[e for run in scale['bootstrap'] for e in run['entries'] if e['entry_id']==entry_id]
            assert sum(e['source_metrics']['decoded_rows'] for e in entries)==scale['cold_business_rows_per_path']+2
    assert len(scales[1]['bootstrap'])>1
    for name in ('G02_small_append','G03_correction_late_delete'):
        by_scale=[{e['entry_id']:e for e in scale[name]['entries']} for scale in scales]
        assert set(by_scale[0])==set(by_scale[1])=={'E50','E70'}
        for entry_id in sorted(by_scale[0]):
            x,y=(entries[entry_id] for entries in by_scale)
            assert x['complete'] and y['complete'] and x['qualified'] and y['qualified']
            assert x['source_metrics']==y['source_metrics']
            assert x['normalized_units']==y['normalized_units']
            assert x['committed_partitions']==y['committed_partitions']
    result['validation']={'passed':True,'comparison_key':'entry_id',
                          'validator_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--worker');parser.add_argument('--root',type=Path)
    parser.add_argument('--output',type=Path);parser.add_argument('--scales',default='100000,1000000');parser.add_argument('--rounds',type=int,default=100)
    parser.add_argument('--validate',type=Path,help='Revalidate a complete saved report without rerunning source processing.')
    args=parser.parse_args()
    if args.validate:
        result=json.loads(args.validate.read_text());validate_growth(result)
        if args.output:args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print('Growth acceptance passed (saved measurements, matched by entry_id)',flush=True);return
    if args.root is None:parser.error('--root is required for source processing')
    if args.worker:
        print(json.dumps(worker(args.worker,args.root),ensure_ascii=False));return
    if args.output is None:parser.error('--output is required for the growth experiment')
    code=hashlib.sha256(Path(__file__).read_bytes())
    for path in sorted((Path(__file__).resolve().parents[1]/'app').rglob('*.py')):
        code.update(str(path.relative_to(Path(__file__).resolve().parents[1])).encode());code.update(path.read_bytes())
    result={'source_code_sha256':code.hexdigest(),'format':'ar03-native-growth-v1','metrics_note':'payload_rows counts native records; decoded_rows and decode_calls count actual business JSON decoding (including repeated dependency validation); bytes are encoded transfer, not physical disk IO. Scratch peaks are sampled by the kernel; EXPLAIN reports buffers separately.', 'scales':[]}
    for rows in map(int,args.scales.split(',')):
        result['scales'].append(experiment(rows,args.root/str(rows),args.rounds))
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2))
    validate_growth(result)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print('Growth acceptance passed',flush=True)


if __name__=='__main__':main()
