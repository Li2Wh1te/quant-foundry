"""Internal helper for isolated LF-D01 multi-container tests, not a product CLI."""
from contextlib import ExitStack
from dataclasses import replace
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import time

import pyarrow as pa
from sqlalchemy import create_engine, text, URL

from app.data_store.catalog import DDL, SourceUpdate
from app.data_store.errors import DataStoreError
from app.data_store.limits import StoreLimits
from app.data_store.readers import Query
from app.data_store.schema import DatasetSpec, fingerprint
from app.data_store.storage import CurrentStore

assert os.getenv('QF_ENVIRONMENT') == 'test' and os.getenv('QF_DATABASE_NAME') == 'lfd01_test'
assert os.getenv('QF_DATABASE_HOST') == 'postgres'
ROOT = Path('/work/store')
COORD = Path('/work/coord')
KEY = b'isolated-container-coordination-test-key'
# Allow container startup jitter without changing production lock defaults.
LIMITS = replace(StoreLimits(), orphan_grace_seconds=1, lock_timeout_ms=15000)


def spec(name='bars'):
    return DatasetSpec(name, pa.schema([pa.field('id',pa.int64(),False),
                                       pa.field('price',pa.decimal128(38,8),False)]), ('id',), 'r1')


def put(st, contract, price):
    current = st.source_state(contract.name,'range')
    update = SourceUpdate('range',fingerprint(price),fingerprint('test'),current['revision'] if current else 0)
    batch = pa.RecordBatch.from_pydict({'id':[0], 'price':[Decimal(price)]}, schema=contract.schema)
    return st.upsert(contract,'default',[batch],update,lower=(0,),upper=(1,),source_check=lambda _:True)


def ready(name):
    with (COORD/(name+'.ready')).open('x') as f:
        f.write('ready')


def wait(name):
    end = time.monotonic()+25
    while not (COORD/(name+'.release')).exists():
        if time.monotonic()>end:
            raise RuntimeError('isolated coordinator timed out')
        time.sleep(.05)


def main():
    mode = sys.argv[1]
    engine = create_engine(URL.create('postgresql+psycopg',username='postgres',password='isolated-test-password',
                                     host='postgres',database='lfd01_test'),connect_args={'connect_timeout':3})
    if mode == 'init':
        ROOT.mkdir(mode=0o700); COORD.mkdir(mode=0o700)
        with engine.begin() as c:
            for stmt in DDL.split(';'):
                if stmt.strip(): c.exec_driver_sql(stmt)
    with CurrentStore(engine,ROOT,cursor_key=KEY,limits=LIMITS,initialize=mode=='init') as st:
        if mode == 'init':
            for name in ('bars','other'):
                st.register(spec(name)); put(st,spec(name),1)
        elif mode == 'hold_writer':
            with st.locks.writer('bars'):
                ready('writer'); wait('writer')
        elif mode == 'contend':
            try:
                with st.locks.writer('bars',timeout_ms=100):
                    raise AssertionError('cross-container exclusion failed')
            except DataStoreError as error:
                assert error.code=='LOCK_TIMEOUT'
            assert st.read(spec('other'),Query()).rows[0]['price']=='1'
        elif mode == 'after_kill':
            with st.locks.writer('bars',timeout_ms=1000):
                pass
        elif mode == 'hold_reader':
            with st.locks.read('bars'), ExitStack() as stack:
                refs=st.catalog.files('bars','default')
                fd=stack.enter_context(st.files.open_object(refs[0]['path']))
                ready('reader'); wait('reader')
                assert os.fstat(fd).st_size==refs[0]['byte_count']
                assert st.catalog.dataset('bars')['generation']==1
        elif mode == 'correct':
            put(st,spec(),2)
        elif mode == 'cleanup_contend':
            ready('cleanup')
            st.cleanup('bars')
            st.cleanup('other')
        elif mode == 'verify':
            assert st.read(spec(),Query()).rows[0]['price']=='2'
            assert st.recover('bars')['pending']==0
            assert st.recover('bars')['pending']==0
        elif mode == 'inspect':
            assert st.read(spec(),Query()).rows[0]['price']=='1'
        else:
            raise ValueError('unknown isolated test mode')
    engine.dispose()
    print(json.dumps({'mode':mode,'passed':True,'synthetic':True}),flush=True)

if __name__=='__main__': main()
