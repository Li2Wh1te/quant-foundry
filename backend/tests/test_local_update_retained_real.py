"""Private retained provider observations; no generated business records.

Set QF_RETAINED_SCHEDULER_INPUT to the bounded read-only export. Payloads and
operational receipts stay outside Git; the committed test contains only the
reproduction procedure and runs against a disposable database and file root.
"""
from dataclasses import replace
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
from uuid import UUID, uuid4

from pydantic import SecretStr
import pytest
from sqlalchemy import DateTime, Uuid, select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.data_ingestion.models.tonghuashun import TonghuashunObservation, TonghuashunCollectionState
from app.data_store.adapters.registry import BY_ID
from app.data_store.catalog import Issue
from app.data_store.coverage import check_coverage
from app.data_store.incremental import FundBatchSources
from app.data_store.limits import MiB, StoreLimits
from app.data_store.local_sources import NativeSources
from app.data_store.pipeline import PipelineOptions, read_entry_status, run_entry, run_local
from app.data_store import scheduler_tasks as tasks
from app.scheduling import runtime as scheduling_runtime
from app.scheduling.models import ScheduledTask, TaskRun
from app.scheduling.registry import TaskDefinition, TaskRegistry
from app.scheduling.schemas import TriggerType
from app.scheduling.service import SchedulerService
from tests.test_data_store_kernel import database, store, KEY
from tests.test_data_store_incremental import ready
from tests.test_data_store_local_update_admission import install_admission

pytestmark = pytest.mark.skipif(os.getenv('POSTGRES_TEST_ENABLED') != '1' or
    not os.getenv('QF_RETAINED_SCHEDULER_INPUT'), reason='requires private retained input and disposable PostgreSQL')


@pytest.fixture
def limits():
    return replace(StoreLimits(), pipeline_spill_bytes=64*MiB,
                   duckdb_memory_bytes=64*MiB, log_bytes=4096)


@pytest.fixture
def retained(ready):
    fixture=json.loads(Path(os.environ['QF_RETAINED_SCHEDULER_INPUT']).read_text())
    assert fixture['format']=='qf-r01-retained-real-input-v1'
    assert fixture['synthetic_business_payloads'] is False and fixture['production_writes']==0

    def decoded(table,row):
        value={key:row[key] for key in table.c.keys() if key in row}
        for column in table.c:
            if value.get(column.name) is None:continue
            if isinstance(column.type,DateTime):
                value[column.name]=datetime.fromisoformat(value[column.name].replace('Z','+00:00'))
            elif isinstance(column.type,Uuid):value[column.name]=UUID(value[column.name])
        return value

    with ready.catalog.engine.begin() as c:
        pending=dict(fixture['observations']);copied=set()
        while pending:
            batch=[(key,row) for key,row in pending.items()
                   if not row.get('base_observation_id') or row['base_observation_id'] in copied]
            assert batch,'retained input dependency is missing'
            c.execute(TonghuashunObservation.__table__.insert(),
                      [decoded(TonghuashunObservation.__table__,row) for _,row in batch])
            for key,_ in batch:copied.add(key);pending.pop(key)
        c.execute(TonghuashunCollectionState.__table__.insert(),
                  [decoded(TonghuashunCollectionState.__table__,row) for row in fixture['states']])
    return fixture


def seed_restriction(current,record):
    current.register(BY_ID['E50' if record['dataset']=='market.stock_daily.reported' else 'E17'].spec)
    issue=Issue(record['issue_key'],record['scope_key'],record['reason'],record['evidence_token'],
                json.loads(record['target_json']),json.loads(record['resolution_json']))
    with current.catalog.transaction() as c:
        current.catalog.change_issues(c,record['dataset'],(issue,),{})
    return current.catalog.issues(record['dataset'],limit=100)


def gate(current,eid):
    with current.catalog.transaction() as c:
        return check_coverage(c,BY_ID[eid],read_entry_status(current,eid))


def test_real_source_runs_continue_automatically_while_old_quality_gate_stays_closed(ready,retained,monkeypatch):
    old=retained['unrelated_stock_issue']
    assert '002054.SZ' not in old['target_json']
    before=seed_restriction(ready,old)
    install_admission(ready.catalog.engine)
    with ready.catalog.engine.begin() as c:
        c.exec_driver_sql('DROP TABLE task_runs');c.exec_driver_sql('DROP TABLE scheduled_tasks')
        ScheduledTask.__table__.create(c);TaskRun.__table__.create(c)
    engine=ready.catalog.engine
    monkeypatch.setattr(scheduling_runtime,'get_engine',lambda:engine)
    monkeypatch.setattr(tasks,'get_engine',lambda:engine)
    monkeypatch.setattr(tasks,'get_settings',lambda:SimpleNamespace(
        data_store_root=ready.files.root,cursor_signing_key=SecretStr(KEY.decode())))
    registry=TaskRegistry();registry.register(TaskDefinition(key=tasks.TASK_KEY,name='本地数据更新',
        english_name='Local Data Update',parameters_model=tasks.LocalUpdateParameters,handler=tasks.update_local))
    settings=get_settings().model_copy(update={'scheduler_enabled':False,'scheduler_max_workers':2})
    runtime=scheduling_runtime.SchedulerRuntime(settings,registry)
    with Session(engine) as session:
        task=ScheduledTask(name='保留真实输入自动续作验证',task_type=tasks.TASK_KEY,
            parameters={'datasets':['market.stock_daily.reported'],'maximum_passes':16,
                        'pass_seconds':180,'pipeline_spill_bytes':64*MiB},
            parameter_version=1,state='active',priority=100,
            schedule={'type':'interval','seconds':600,'start_at':datetime.now(UTC).isoformat()})
        session.add(task);session.flush();identity=task.id
        initial=SchedulerService(session,registry).enqueue_run(identity,
            trigger_type=TriggerType.SCHEDULED,max_queued_runs=100)
        initial_id=initial.id;frozen_parameters=dict(task.parameters);session.commit()
    try:
        deadline=time.monotonic()+120
        while time.monotonic()<deadline:
            runtime.dispatch_queued_runs()
            with Session(engine) as session:
                running=session.scalar(text("SELECT count(*) FROM task_runs WHERE status IN ('queued','running')"))
                ranges=session.scalar(text("SELECT count(*) FROM data_store_source_ranges WHERE dataset='stock_daily'"))
            if not running and not ranges:break
            time.sleep(.1)
        else:pytest.fail('retained source continuation did not reach its finite terminal boundary')
    finally:
        runtime.executor.shutdown(wait=True,cancel_futures=True)
    with Session(engine) as session:
        runs=list(session.scalars(select(TaskRun).where(TaskRun.task_id==identity).order_by(TaskRun.created_at)))
        assert len(runs)>2 and runs[0].id==initial_id
        assert all(r.trigger_type=='scheduled' and r.parameters==frozen_parameters for r in runs)
        assert sum((r.result or {}).get('source_rows',0) for r in runs)==35
        assert all((r.result or {}).get('budget_units',0)<=16 for r in runs)
        assert any(r.status=='failed' and r.result['committed_partitions']>0 for r in runs)
        assert any(r.status=='succeeded' and r.result['complete'] is False for r in runs)
        assert all(r.result['complete'] is False for r in runs)
    assert ready.catalog.dataset(BY_ID['E50'].spec.name)['row_count']==2444
    after=ready.catalog.issues(BY_ID['E50'].spec.name,limit=100)
    assert after==before
    assert gate(ready,'E50')['satisfied'] is False


def test_real_fund_seal_resumes_without_redecoding_or_claiming_whole_history(ready,retained):
    entry=BY_ID['E44'];pairs=[(item['job'],item['observation']) for item in retained['fund_pairs']]
    source=FundBatchSources(NativeSources(ready.catalog.engine),entry,pairs)
    acquired=0
    for _ in range(200):
        result=run_entry(ready,entry,source,options=PipelineOptions(maximum_passes=3))
        acquired+=result['source_rows']
        assert result['passes']<=3 and result['committed_partitions']<=3
        assert result['input_failures']==0
        if result['complete']:break
    else:pytest.fail('real fund seal did not finish its bounded captured group')
    assert acquired==3 and result['qualified'] is True
    assert gate(ready,'E44')['satisfied'] is False


def test_real_later_valid_window_does_not_clear_whole_subject_restriction(ready,retained):
    issue=retained['issues'][0];before=seed_restriction(ready,issue)
    result=run_local(ready,NativeSources(ready.catalog.engine),entries=[BY_ID['E17']],
                     options=PipelineOptions(maximum_passes=256))[0]
    assert result['input_failures']==0 and result['qualified'] is False
    assert ready.catalog.issues(BY_ID['E17'].spec.name,limit=100)==before
    assert gate(ready,'E17')['satisfied'] is False


def test_real_missing_original_is_a_source_failure_and_never_a_valid_empty_result(ready,retained):
    result=run_local(ready,NativeSources(ready.catalog.engine),entries=[BY_ID['E24']],
                     options=PipelineOptions(maximum_passes=32))[0]
    assert result['input_failures']>0 and result['qualified'] is False
    issues=ready.catalog.issues(BY_ID['E24'].spec.name,limit=100)
    assert any(row['reason']=='SOURCE_REFRESH_FAILED' for row in issues)
    assert gate(ready,'E24')['satisfied'] is False


def test_real_sealed_input_survives_writer_wait_without_failure_clock_or_rescan(ready,retained):
    sources=NativeSources(ready.catalog.engine);options=PipelineOptions(maximum_passes=1)
    acquired=0
    with ready.budget.reserve('write'):
        for _ in range(3):
            result=run_local(ready,sources,entries=[BY_ID['E50']],options=options)[0]
            acquired+=result['source_rows']
            assert result['reason']=='WRITE_ADMISSION_DEFERRED'
            assert result['committed_partitions']==result['acknowledged_ranges']==0
            assert result['refresh']['consecutive_failures']==result['refresh']['stalled_failures']==0
            assert result['refresh'].get('continuation_aborted') is None
            assert 'pipeline.E50' in ready.budget.pending_keys()
    assert acquired==32
    resumed=run_local(ready,sources,entries=[BY_ID['E50']],options=options)[0]
    assert resumed['source_rows']==0 and resumed['committed_partitions']==1
    assert resumed['qualified'] is False


def test_real_already_consumed_source_eof_uses_one_budget_unit_and_no_payload_or_file_commit(ready,retained):
    entry=BY_ID['E50'];sources=NativeSources(ready.catalog.engine)
    first=run_entry(ready,entry,sources,options=PipelineOptions(maximum_passes=4096))
    assert first['complete'] and first['qualified']
    before=ready.catalog.dataset(entry.spec.name);stop=retained['stock002054']['captured_stop']
    active={'id':uuid4().hex,'bootstrap':False,'when':stop['at'],'lower':'-infinity',
            'after':stop,'stop':stop}
    with ready.catalog.engine.begin() as c:
        c.execute(text("INSERT INTO data_store_source_ranges(source,dataset,subject,variant,range_key,"
            "pending,bootstrap_pending,active) VALUES ('tonghuashun','stock_daily','002054.SZ','default','*',"
            "false,false,CAST(:active AS jsonb))"),{'active':json.dumps(active)})
    result=run_local(ready,sources,entries=[entry],options=PipelineOptions(maximum_passes=1))[0]
    assert result['acknowledged_ranges']==result['budget_units']==1
    assert result['source_rows']==result['committed_partitions']==0
    assert result['refresh']['stalled_failures']==0
    assert ready.catalog.dataset(entry.spec.name)==before


def test_real_repaired_history_does_not_block_a_later_unchanged_successful_run(ready,retained):
    entry=BY_ID['E50'];sources=NativeSources(ready.catalog.engine)
    options=PipelineOptions(maximum_passes=4096)
    first=run_local(ready,sources,entries=[entry],options=options)[0]
    assert first['complete'] and first['qualified']
    assert first['refresh'].get('blocked_input_revision') is None
    later=run_local(ready,sources,entries=[entry],options=options)[0]
    assert later['complete'] and later['qualified'] and later['source_rows']==0
    assert later['outcome']=='unchanged'


def test_real_entries_share_call_budget_and_old_deferral_gets_next_turn(ready,retained):
    seed_restriction(ready,retained['issues'][0])
    sources=NativeSources(ready.catalog.engine);options=PipelineOptions(maximum_passes=1)
    entries=[BY_ID['E17'],BY_ID['E24']]
    first=run_local(ready,sources,entries=entries,options=options)
    assert first[0]['entry_id']=='E17' and first[0]['committed_partitions']==1
    assert first[0]['budget_units']==first[0]['passes']==1
    assert first[1]['entry_id']=='E24' and first[1]['outcome']=='deferred'
    assert first[1]['source_rows']==0 and first[1]['attempted'] is False
    assert sum(row.get('budget_units',row.get('passes',0)) for row in first)<=1
    second=run_local(ready,sources,entries=entries,options=options)
    assert second[0]['entry_id']=='E24' and second[0]['input_failures']>0
    assert second[1]['entry_id']=='E17' and second[1]['outcome']=='deferred'
    assert second[1]['source_rows']==0 and second[1]['attempted'] is False
    assert sum(row.get('budget_units',row.get('passes',0)) for row in second)<=1
