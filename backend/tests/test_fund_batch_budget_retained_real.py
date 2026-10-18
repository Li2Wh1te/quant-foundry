"""Reproduce the actual E23 working-file boundary with retained originals.

The optional private fixture contains complete immutable source objects and
their real captured range metadata. It is never committed with this test, and
all writes below belong to a disposable PostgreSQL schema and store root.
"""
from dataclasses import replace
from datetime import datetime
import json
import os
from pathlib import Path
import time
from uuid import UUID

import pytest
from sqlalchemy import DateTime, Uuid, text

from app.data_ingestion.models.tonghuashun import TonghuashunObservation, TonghuashunCollectionState
from app.data_store.adapters.registry import BY_ID
from app.data_store.adapters.contracts import digest
from app.data_store.errors import DataStoreError
from app.data_store.incremental import FundBatchSources, next_observation
from app.data_store.limits import MiB, StoreLimits
from app.data_store.local_sources import NativeSources
from app.data_store.pipeline import PipelineOptions, read_entry_status, run_entry, _status
from tests.test_data_store_kernel import database, store
from tests.test_data_store_incremental import ready

pytestmark = pytest.mark.skipif(os.getenv('POSTGRES_TEST_ENABLED') != '1' or
    not os.getenv('QF_RETAINED_E23_BUDGET_INPUT'),
    reason='requires actual private E23 originals and disposable PostgreSQL')


@pytest.fixture
def limits():
    return replace(StoreLimits(), pipeline_spill_bytes=256*MiB,
                   duckdb_memory_bytes=64*MiB, log_bytes=4096)


@pytest.fixture
def captured(ready):
    fixture=json.loads(Path(os.environ['QF_RETAINED_E23_BUDGET_INPUT']).read_text())
    assert fixture['format']=='qf-r01-retained-e23-budget-v1'
    assert fixture['synthetic_business_payloads'] is False and fixture['production_writes']==0

    def decoded(table,row):
        value={key:row[key] for key in table.c.keys() if key in row}
        for column in table.c:
            if value.get(column.name) is None:continue
            if isinstance(column.type,DateTime):
                value[column.name]=datetime.fromisoformat(value[column.name].replace('Z','+00:00'))
            elif isinstance(column.type,Uuid):value[column.name]=UUID(value[column.name])
        return value

    with ready.catalog.engine.begin() as connection:
        pending=dict(fixture['observations']);copied=set()
        while pending:
            batch=[(key,row) for key,row in pending.items()
                   if not row.get('base_observation_id') or row['base_observation_id'] in copied]
            assert batch,'missing retained original dependency'
            connection.execute(TonghuashunObservation.__table__.insert(),
                [decoded(TonghuashunObservation.__table__,row) for _,row in batch])
            for key,_ in batch:copied.add(key);pending.pop(key)
        connection.execute(TonghuashunCollectionState.__table__.insert(),
            [decoded(TonghuashunCollectionState.__table__,row) for row in fixture['states']])
        connection.execute(text("DELETE FROM data_store_source_ranges WHERE dataset='fund_manager_performance'"))
        for pair in fixture['pairs']:_insert_range(connection,pair['job'])
    # Reproduce an existing production consumer rather than bootstrapping a
    # new discovery sweep, which would intentionally dirty all copied ranges.
    entry=BY_ID['E23'];ready.register(entry.spec)
    _status(ready,entry,{'entry_id':entry.id,'state':'incomplete','complete':False,'qualified':False,
        'incremental':{'version':1,'contract':digest(entry.spec.descriptor())}})
    return fixture


def _insert_range(connection,job):
    connection.execute(text('INSERT INTO data_store_source_ranges '
        '(source,dataset,subject,variant,range_key,pending,bootstrap_pending,active,lower_at,enqueued_at) '
        'VALUES (:source,:dataset,:subject,:variant,:range_key,:pending,:bootstrap_pending,'
        'CAST(:active_json AS jsonb),CAST(:lower_text AS timestamptz),CAST(:enqueued_at AS timestamptz))'),
        dict(job,active_json=json.dumps(job['active'])))


def _active(current,job):
    with current.catalog.engine.connect() as connection:
        return connection.scalar(text('SELECT active FROM data_store_source_ranges '
            'WHERE source=:source AND dataset=:dataset AND subject=:subject AND variant=:variant AND range_key=:range_key'),job)


def test_real_E23_wide_failure_keeps_fresh_counts_and_single_object_fits_same_budget(ready,captured):
    entry=BY_ID['E23'];native=NativeSources(ready.catalog.engine)
    pairs=[(p['job'],p['observation']) for p in captured['pairs']]
    assert len(pairs)==59  # The exact group retained from the failed natural run.
    options=PipelineOptions(maximum_passes=1,pass_seconds=180,pipeline_spill_bytes=256*MiB)
    # Reproduce the original wide acquisition in isolation. A mapped SQLite
    # failure must carry its own counters and must publish no incomplete scan.
    with pytest.raises(DataStoreError) as caught:
        run_entry(ready,entry,FundBatchSources(native,entry,pairs),options=options)
    assert caught.value.code=='SCRATCH_BUDGET_EXCEEDED'
    failed=caught.value.entry_result
    assert failed['work_admitted'] and failed['source_rows']>0 and failed['normalized_units']>0
    assert failed['committed_partitions']==0 and not failed['complete']
    assert ready.catalog.dataset(entry.spec.name)['row_count']==0
    assert 'pipeline.E23' not in ready.budget.pending_keys()

    first=pairs[0];unselected={j['subject']:_active(ready,j) for j,_ in pairs[1:]}
    acquired=0;committed=0;deadline=time.monotonic()+180
    for _ in range(256):
        assert time.monotonic()<deadline,'single retained source did not reach its bounded acknowledgement'
        result=run_entry(ready,entry,NativeSources(ready.catalog.engine),options=options)
        acquired+=result['source_rows'];committed+=result['committed_partitions']
        assert result['budget_units']<=1 and result['committed_partitions']<=1
        if acquired:assert acquired==1  # Sealed continuation never decodes it again.
        active=_active(ready,first[0])
        if active and (active.get('after') or {}).get('id')==first[1]['id']:break
    else:pytest.fail('the retained single-object source was never acknowledged')
    assert acquired==1 and committed>0 and ready.catalog.dataset(entry.spec.name)['row_count']>0
    assert all(_active(ready,j)==unselected[j['subject']] for j,_ in pairs[1:])
    assert _active(ready,first[0])['stop']==first[0]['active']['stop']
    assert result['complete'] is False  # The other real histories remain pending.


def test_real_E23_existing_sealed_group_resumes_exact_selector_after_other_scope_arrives(ready,captured):
    entry=BY_ID['E23'];pairs=[(p['job'],p['observation']) for p in captured['pairs']]
    selected=pairs[1:3];arrival=pairs[0]
    with ready.catalog.engine.begin() as connection:
        connection.execute(text("DELETE FROM data_store_source_ranges WHERE dataset='fund_manager_performance'"))
        for job,_ in selected:_insert_range(connection,job)
    options=PipelineOptions(maximum_passes=1,pass_seconds=180,pipeline_spill_bytes=256*MiB)
    initial=run_entry(ready,entry,FundBatchSources(NativeSources(ready.catalog.engine),entry,selected),options=options)
    assert initial['source_rows']==2 and not initial['complete'] and 'pipeline.E23' in ready.budget.pending_keys()
    # This is the production marker for an already sealed legacy group. An
    # unrelated earlier-sorting active scope must not alter that exact group.
    marker=read_entry_status(ready,entry.id)
    marker['incremental']=dict(marker.get('incremental',{}),version=1,
        active_observation={'batch':[o for _,o in selected]})
    _status(ready,entry,marker)
    with ready.catalog.engine.begin() as connection:_insert_range(connection,arrival[0])
    unchanged=_active(ready,arrival[0]);deadline=time.monotonic()+180
    for _ in range(256):
        assert time.monotonic()<deadline,'legacy retained selector did not finish'
        result=run_entry(ready,entry,NativeSources(ready.catalog.engine),options=options)
        assert result['source_rows']==0 and result['budget_units']<=1
        if all((_active(ready,j).get('after') or {}).get('id')==o['id'] for j,o in selected):break
    else:pytest.fail('legacy captured group was not acknowledged')
    assert _active(ready,arrival[0])==unchanged
    assert next_observation(NativeSources(ready.catalog.engine),entry,arrival[0])==arrival[1]
