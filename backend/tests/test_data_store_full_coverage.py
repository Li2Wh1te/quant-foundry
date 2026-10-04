"""AR-02 acceptance using real isolated PostgreSQL, source scans and files."""
import json
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text

from app.data_store.adapters.registry import BY_ID, ENTRIES
from app.data_store.adapters.canonical import NativeInputError
from app.data_store.audit_export import export_audit
from app.data_store.coverage import check_coverage
from app.data_store.errors import DataStoreError
from app.data_store.local_sources import NativeSources, TABLES
from app.data_store.pipeline import PipelineOptions, read_entry_status, run_entry
from app.legacy_reset.catalog import ResetRefused
from app.legacy_reset.operations import finish_rebuild
from tests.test_data_store_kernel import database, limits, store, test_url
from tests.test_data_store_domain_samples import ready
from tests.test_data_store_api import api
from tests.test_data_store_audit_export import _request, _checks
from tests.test_legacy_reset import DDL

pytestmark = pytest.mark.skipif(os.getenv('POSTGRES_TEST_ENABLED') != '1',
                                reason='isolated PostgreSQL required')


def judgment(store, entry):
    with store.catalog.transaction() as c:
        return check_coverage(c, entry, read_entry_status(store, entry.id))


def empty_sources(store):
    # Keep the real native readers and schemas; remove synthetic fixture rows
    # only inside the random schema owned by this test.
    with store.catalog.engine.begin() as c:
        for name in {'tonghuashun_observations', 'tonghuashun_collection_states',
                     *(item[0] for item in TABLES.values())}:
            c.exec_driver_sql('DELETE FROM '+name)


def test_nested_ladder_native_comparison_preserves_files_and_blocks_new_values(ready):
    from copy import deepcopy
    from datetime import timedelta
    from uuid import uuid4
    from app.data_store.adapters.contracts import digest, native_json
    from app.data_store.verify_coverage import verify_existing
    from tests.test_data_store_domain_samples import sample

    entry = BY_ID['E35']
    source = sample('E35')
    content = deepcopy(source.content)
    boards = content['item'][0]['boards']
    boards['two_board'] = [dict(thscode='000001.SZ', board_num=2)]
    boards['four_board'] = [dict(thscode='000002.SZ', board_num=4)]
    with ready.catalog.engine.begin() as connection:
        connection.execute(text(
            'UPDATE tonghuashun_observations SET data_json=:data,content_hash=:hash '
            'WHERE dataset=:dataset'),
            dict(data=native_json(content), hash=digest(content), dataset=entry.native))
    native = NativeSources(ready.catalog.engine)
    assert run_entry(ready, entry, native)['qualified']
    before = all_files(ready, entry)

    # Publishing physically sorts the member rows. Verification must compare
    # the complete nested business object without requiring traversal order.
    matched = verify_existing(ready, entry, NativeSources(ready.catalog.engine), seconds=30)
    assert matched['complete'] and matched['mismatched_objects'] == 0
    assert all_files(ready, entry) == before
    assert judgment(ready, entry)['satisfied']

    changed = deepcopy(content)
    changed['item'][0]['boards']['four_board'][0]['board_num'] = 5
    with ready.catalog.engine.begin() as connection:
        connection.execute(text(
            'INSERT INTO tonghuashun_observations '
            '(id,dataset,subject,variant,observed_at,content_hash,data_json,request_json) '
            "VALUES (:id,:dataset,:subject,:variant,:observed,:hash,:data,'[]')"),
            dict(id=uuid4(), dataset=entry.native, subject=source.subject,
                 variant=source.variant, observed=source.observed_at + timedelta(seconds=1),
                 hash=digest(changed), data=native_json(changed)))
    mismatch = verify_existing(ready, entry, NativeSources(ready.catalog.engine), seconds=30)
    assert not mismatch['complete'] and mismatch['mismatched_objects'] == 1
    assert not judgment(ready, entry)['satisfied']
    assert all_files(ready, entry) == before
    assert run_entry(ready, entry, NativeSources(ready.catalog.engine))['qualified']
    assert verify_existing(ready, entry, NativeSources(ready.catalog.engine), seconds=30)['complete']


def prepare_finish(store):
    with store.catalog.engine.begin() as c:
        for ddl in DDL[:3]:
            c.exec_driver_sql(ddl)
        c.execute(text("INSERT INTO data_store_legacy_maintenance(singleton,phase) VALUES (1,'reset_done')"))


def finish(store):
    return finish_rebuild(store.catalog.engine, expect_database=test_url().database)


def test_F01_F02_F03_F04_full_range_survives_partial_retry_and_finishes(ready, tmp_path):
    empty_sources(ready)
    entry=BY_ID['E68']
    native=NativeSources(ready.catalog.engine)
    # Fifty-nine true empty scans establish coverage; no hand-written flags.
    for other in ENTRIES:
        if other.business and other.id != entry.id:
            assert run_entry(ready,other,native)['complete']
            assert judgment(ready,other)['satisfied']
    assert sum(e.business for e in ENTRIES)==60
    with ready.catalog.engine.begin() as c:
        c.execute(text("INSERT INTO trading_calendar_days(exchange,calendar_date,is_open) VALUES ('SSE',:day,true)"),
                  [{'day':f'{2000+i//12}-{1+i%12:02}-01'} for i in range(300)])
    first=run_entry(ready,entry,native)
    assert first['committed_partitions']==256 and not first['complete']
    before={r['path']:r['content_hash'] for r in all_files(ready,entry)}
    part=first['last_partition']
    retry=run_entry(ready,entry,native,options=PipelineOptions(mode='retry',partitions=(part,)))
    assert retry['complete'] and retry['qualified']
    assert retry['full_coverage']['remaining']==44
    assert not judgment(ready,entry)['satisfied']
    prepare_finish(ready)
    with pytest.raises(ResetRefused,match='全部业务入口'):finish(ready)
    empty=run_entry(ready,entry,native,options=PipelineOptions(mode='retry',partitions=('absent',)))
    assert empty['complete'] and empty['committed_partitions']==0
    assert empty['full_coverage']['remaining']==44
    with pytest.raises(ResetRefused):finish(ready)
    export_audit(ready.catalog.engine,ready.files.root,tmp_path/'incomplete',entries=(entry,))
    checks=_checks(tmp_path/'incomplete')
    assert any(c['check']=='full_file_scan' and c['status']=='pass' for c in checks)
    assert any(c['check']=='full_range_coverage' and c['code']=='FULL_RANGE_PENDING' for c in checks)
    last=run_entry(ready,entry,native)
    assert last['resumed'] and last['source_rows']==0 and last['committed_partitions']==44
    after={r['path']:r['content_hash'] for r in all_files(ready,entry)}
    assert len(after)==300 and before.items()<=after.items()
    assert judgment(ready,entry)['satisfied']
    assert finish(ready)['qualified_business_entries']==60
    assert not ready.budget.pending_keys()


def all_files(store,entry):
    with store.catalog.transaction() as c:
        return c.execute(text('SELECT * FROM data_store_files WHERE dataset=:d'),
                         {'d':entry.spec.name}).mappings().all()


def test_ordered_native_encoding_matches_original_json_order_and_budget(store,monkeypatch):
    from datetime import datetime, timezone
    from decimal import Decimal
    from uuid import UUID
    from app.data_store import local_sources
    from app.data_store.adapters.contracts import digest, native_json
    from app.data_ingestion.models.tonghuashun import TonghuashunObservation
    entry=BY_ID['E14']
    when=datetime(2026,1,1,tzinfo=timezone.utc)
    body={'item':[], 'nullable':None, 'label':'原生准确值',
          'number':Decimal('-0.0001234567890123456789')}
    # Equal native times are deliberately inserted in reverse UUID order.
    # The UUID only fixes iteration; it must not replace source-time authority.
    rows=[dict(id=UUID(int=index),dataset=entry.native,subject='same.SZ',variant='default',
               observed_at=when,request_json='[]',data_json=native_json(body),content_hash=digest(body),
               row_count=0,base_observation_id=None,chain_depth=0) for index in (4,2,3,1)]
    with store.catalog.engine.begin() as connection:
        TonghuashunObservation.__table__.create(connection)
        connection.execute(TonghuashunObservation.__table__.insert(),rows)
    order='ORDER BY t.subject,t.variant,t.observed_at,t.id'
    baseline=text('SELECT CASE WHEN octet_length(row_to_json(t)::text)<=:payload_budget '
                  'THEN row_to_json(t)::text ELSE NULL END FROM tonghuashun_observations t '
                  'WHERE t.dataset=:d '+order)
    native=NativeSources(store.catalog.engine)
    encoded=[];original_json=local_sources._json
    def captured(value,limit):
        encoded.append(value)
        return original_json(value,limit)
    with native._snapshot() as (connection,_):
        original=connection.execute(baseline,{'d':entry.native,'payload_budget':33554432}).scalars().all()
        assert len(original)<=16  # This assertion bounds the entire test input.
        monkeypatch.setattr(local_sources,'_json',captured)
        actual=list(native._rows(connection,'tonghuashun_observations','WHERE t.dataset=:d',
                                 {'d':entry.native},order))
        assert encoded==original  # Exact UTF-8 JSON, including native NULLs and column order.
        assert [digest(row) for row in actual]==[digest(original_json(row,33554432)) for row in original]
        selected=[row for row in actual if row['subject']=='same.SZ']
        assert [row['id'] for row in selected]==[str(UUID(int=index)) for index in (1,2,3,4)]
        assert all(row['base_observation_id'] is None for row in selected)
        assert all(row['data_json']==native_json(body) for row in selected)
        # Both projections return NULL for an over-budget payload. The formal
        # reader must preserve its existing SOURCE_BUDGET_EXCEEDED rejection,
        # never report that rejected native row as an empty successful source.
        assert all(row is None for row in connection.execute(baseline,
            {'d':entry.native,'payload_budget':1}).scalars())
        with pytest.raises(NativeInputError) as failed:
            list(native._rows(connection,'tonghuashun_observations','WHERE t.dataset=:d',
                              {'d':entry.native},order,payload_bytes=1))
        assert failed.value.code=='SOURCE_BUDGET_EXCEEDED'


def test_F02_last_entry_only_selected_never_qualifies(ready):
    empty_sources(ready)
    entry=BY_ID['E68'];native=NativeSources(ready.catalog.engine)
    for other in ENTRIES:
        if other.business and other!=entry:run_entry(ready,other,native)
    result=run_entry(ready,entry,native,options=PipelineOptions(mode='retry',partitions=('absent',)))
    assert result['complete'] and result['qualified']
    assert judgment(ready,entry)['reason']=='FULL_RANGE_UNPROVEN'
    prepare_finish(ready)
    with pytest.raises(ResetRefused):finish(ready)


@pytest.mark.parametrize('failure',['unavailable','timeout','interrupted'])
def test_F05_failed_scan_is_not_empty_and_preserves_last_proof(ready,failure):
    entry=BY_ID['E68'];native=NativeSources(ready.catalog.engine)
    run_entry(ready,entry,native)
    proof=read_entry_status(ready,entry.id)['full_coverage']
    class Failed(NativeSources):
        def iter_entry(self,entry):
            if failure=='interrupted':raise RuntimeError('isolated interruption')
            raise NativeInputError('LOCAL_SOURCE_UNAVAILABLE' if failure=='unavailable' else 'SOURCE_BUDGET_EXCEEDED','隔离读取失败。')
            yield
    with pytest.raises((NativeInputError,RuntimeError)):
        run_entry(ready,entry,Failed(ready.catalog.engine))
    assert read_entry_status(ready,entry.id)['full_coverage']==proof
    assert judgment(ready,entry)['reason']=='CURRENT_INPUT_PENDING'
    assert run_entry(ready,entry,native)['complete']
    assert judgment(ready,entry)['satisfied']


def test_F06_correction_preserves_coverage_and_unrelated_files(ready):
    entry=BY_ID['E68'];native=NativeSources(ready.catalog.engine)
    with ready.catalog.engine.begin() as c:
        c.execute(text("INSERT INTO trading_calendar_days(exchange,calendar_date,is_open) VALUES ('SSE','2025-02-01',true)"))
    run_entry(ready,entry,native)
    old=all_files(ready,entry)
    part=next(r['partition_key'] for r in old if '2025-02' in r['partition_key'])
    boundary=read_entry_status(ready,entry.id)['full_coverage']['boundary']
    with ready.catalog.engine.begin() as c:
        c.execute(text("UPDATE trading_calendar_days SET is_open=false WHERE calendar_date='2025-02-01'"))
    result=run_entry(ready,entry,native,options=PipelineOptions(mode='retry',partitions=(part,)))
    assert result['complete'] and result['metrics']['files_written']==1
    assert result['full_coverage']['boundary']==boundary
    assert judgment(ready,entry)['satisfied']
    assert {r['path'] for r in old if r['partition_key']!=part} <= {r['path'] for r in all_files(ready,entry)}


def test_F07_different_sealed_range_cannot_destroy_pending_work(ready):
    entry=BY_ID['E68'];native=NativeSources(ready.catalog.engine)
    with ready.catalog.engine.begin() as c:
        c.execute(text("INSERT INTO trading_calendar_days(exchange,calendar_date,is_open) VALUES ('SSE','2025-02-01',true)"))
    first=run_entry(ready,entry,native,options=PipelineOptions(maximum_passes=1))
    with pytest.raises(DataStoreError,match='来源确认依据'):
        run_entry(ready,entry,native,options=PipelineOptions(mode='rebuild',maximum_passes=1))
    assert read_entry_status(ready,entry.id)['full_coverage']==first['full_coverage']
    assert run_entry(ready,entry,native)['resumed']
    with ready.catalog.engine.begin() as c:
        c.execute(text('UPDATE data_store_datasets SET generation=generation+1 WHERE name=:d'),{'d':entry.spec.name})
    assert judgment(ready,entry)['reason']=='FULL_RANGE_GENERATION_CHANGED'


def test_F08_F09_legacy_sealed_work_and_crash_recovery(ready,monkeypatch):
    from app.data_store import pipeline
    entry=BY_ID['E68'];native=NativeSources(ready.catalog.engine)
    with ready.catalog.engine.begin() as c:
        c.execute(text("INSERT INTO trading_calendar_days(exchange,calendar_date,is_open) VALUES ('SSE','2025-02-01',true)"))
    run_entry(ready,entry,native,options=PipelineOptions(maximum_passes=1))
    # Model an old R01 summary; only the actual sealed work/checkpoints may
    # re-establish a proof. Removing metadata does not remove current files.
    status=read_entry_status(ready,entry.id)
    status.pop('full_coverage');status.pop('coverage_pending')
    with ready.catalog.engine.begin() as c:
        c.execute(text('UPDATE data_store_entry_status SET summary_json=:j WHERE entry_id=:i'),
                  {'j':json.dumps(status),'i':entry.id})
    spool=next(ready.files.root.glob('.scratch/*/spill/*.sqlite'))
    with sqlite3.connect(spool) as c:
        progress=json.loads(c.execute('SELECT body FROM progress').fetchone()[0])
        progress.pop('boundary');progress.pop('source_selection')
        c.execute('UPDATE progress SET body=?',(json.dumps(progress),))
    assert judgment(ready,entry)['reason']=='FULL_RANGE_UNPROVEN'
    original=pipeline._commit_partition
    def crash(*args,**kwargs):
        original(*args,**kwargs)
        raise RuntimeError('after durable catalog commit')
    with monkeypatch.context() as patch:
        patch.setattr(pipeline,'_commit_partition',crash)
        with pytest.raises(RuntimeError):run_entry(ready,entry,native)
    assert not judgment(ready,entry)['satisfied']
    before=all_files(ready,entry)
    assert run_entry(ready,entry,native)['complete']
    assert before==all_files(ready,entry)
    assert judgment(ready,entry)['satisfied']


def test_F08_finish_fences_concurrent_status_writes(ready,monkeypatch):
    from app.data_store import coverage
    empty_sources(ready)
    for entry in ENTRIES:
        if entry.business:run_entry(ready,entry,NativeSources(ready.catalog.engine))
    prepare_finish(ready)
    checked=threading.Event();release=threading.Event();writing=threading.Event()
    original=coverage.check_coverage
    def held(*args):
        checked.set()
        assert release.wait(5)
        return original(*args)
    def writer():
        with ready.catalog.engine.begin() as c:
            writing.set()
            c.execute(text("UPDATE data_store_entry_status SET summary_json='{}' WHERE entry_id='E68'"))
    with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=2) as pool:
        patch.setattr(coverage,'check_coverage',held)
        finishing=pool.submit(finish,ready)
        try:
            assert checked.wait(5)
            update=pool.submit(writer)
            assert writing.wait(5)
            # The real PostgreSQL write cannot cross the checked-to-ready gap.
            from concurrent.futures import TimeoutError
            with pytest.raises(TimeoutError):update.result(timeout=0.2)
        finally:
            release.set()
        assert finishing.result(timeout=5)['phase']=='ready'
        update.result(timeout=5)
    assert judgment(ready,BY_ID['E68'])['reason']=='FULL_RANGE_UNPROVEN'


def test_finish_drains_current_admission_before_locking_its_checkpoint_tables(ready):
    from concurrent.futures import TimeoutError
    from sqlalchemy.orm import Session
    from app.data_store.availability import require_operable
    empty_sources(ready)
    run_entry(ready, BY_ID['E68'], NativeSources(ready.catalog.engine))
    prepare_finish(ready)
    with ready.catalog.engine.begin() as connection:
        connection.exec_driver_sql(DDL[3])
        connection.exec_driver_sql(DDL[4])
        connection.execute(text("""UPDATE data_store_legacy_maintenance SET
            plan_hash=:hash, completed_json='["hooks","derived","originals","functions","files"]',
            files_started=true"""), {'hash': 'a' * 64})
    with Session(ready.catalog.engine) as session, ThreadPoolExecutor(max_workers=1) as pool:
        require_operable(session)
        pending_finish = pool.submit(finish, ready)
        with pytest.raises(TimeoutError):
            pending_finish.result(timeout=0.2)
        # An admitted updater must still be able to persist its checkpoint
        # while finish waits. Locking status first would deadlock this write.
        with ready.catalog.engine.begin() as connection:
            connection.execute(text("SET LOCAL lock_timeout='500ms'"))
            connection.execute(text("UPDATE data_store_entry_status SET updated_at=clock_timestamp() WHERE entry_id='E68'"))
        session.rollback()
        with pytest.raises(ResetRefused):
            pending_finish.result(timeout=5)
    # Neither safe admission nor finish waiting bypasses the other 59 domains.
    with ready.catalog.transaction() as connection:
        assert connection.execute(text('SELECT phase FROM data_store_legacy_maintenance')).scalar_one() == 'reset_done'


def test_F07_changed_input_selection_rejected_and_old_proof_not_borrowed(ready):
    entry=BY_ID['E68']
    class Selection(NativeSources):
        selection='original'
        def selection_key(self):return self.selection
    source=Selection(ready.catalog.engine)
    run_entry(ready,entry,source)
    source.selection='different'
    result=run_entry(ready,entry,source,options=PipelineOptions(mode='retry',partitions=('absent',)))
    assert result['complete']
    assert judgment(ready,entry)['reason']=='CURRENT_INPUT_PENDING'
    with ready.catalog.engine.begin() as c:
        c.execute(text("INSERT INTO trading_calendar_days(exchange,calendar_date,is_open) VALUES ('SSE','2025-02-01',true)"))
    first=run_entry(ready,entry,source,options=PipelineOptions(maximum_passes=1))
    source.selection='original'
    with pytest.raises(DataStoreError):run_entry(ready,entry,source)
    assert read_entry_status(ready,entry.id)['full_coverage']==first['full_coverage']
    source.selection='different'
    assert run_entry(ready,entry,source)['resumed']


def test_F06_quality_repair_and_F07_rule_change(ready):
    from dataclasses import replace
    from app.data_store.adapters.normalize import normalize
    from tests.test_data_store_local_pipeline import Inputs,company
    entry=BY_ID['E41'];raw=company('good',1)
    run_entry(ready,entry,Inputs(raw))
    unit=next(normalize(entry,raw));part=entry.spec.partitioner((*unit.key,'root'))
    options=PipelineOptions(mode='retry',partitions=(part,))
    run_entry(ready,entry,Inputs(company('bad',2,failure='DUPLICATE_BUSINESS_KEY')),options=options)
    assert judgment(ready,entry)['reason']=='CURRENT_QUALITY_UNRESOLVED'
    run_entry(ready,entry,Inputs(company('fixed',3)),options=options)
    assert judgment(ready,entry)['satisfied']
    revised=replace(entry)
    revised.__dict__['spec']=replace(entry.spec,rule='isolated-new-rule')
    with ready.catalog.transaction() as c:
        assert check_coverage(c,revised,read_entry_status(ready,entry.id))['reason']=='FULL_RANGE_CONTRACT_CHANGED'


def test_F05_disabled_source_still_reads_local_history(ready):
    entry=BY_ID['E68']
    # A disabled provider is not an empty local boundary. NativeSources must
    # never consult this switch to discard retained observations/tables.
    with ready.catalog.engine.begin() as c:
        c.exec_driver_sql('CREATE TABLE data_source_configs(source text,enabled boolean)')
        c.exec_driver_sql("INSERT INTO data_source_configs VALUES ('tushare',false)")
    result=run_entry(ready,entry,NativeSources(ready.catalog.engine))
    assert result['source_rows']>0 and result['complete']
    assert all_files(ready,entry) and judgment(ready,entry)['satisfied']


def test_F09_old_complete_without_sealed_evidence_needs_readback(ready):
    entry=BY_ID['E68'];native=NativeSources(ready.catalog.engine)
    result=run_entry(ready,entry,native)
    before=all_files(ready,entry)
    for field in ('full_coverage','last_complete_coverage','coverage_pending'):
        result.pop(field,None)
    with ready.catalog.engine.begin() as c:
        c.execute(text('UPDATE data_store_entry_status SET summary_json=:j WHERE entry_id=:i'),
                  {'j':json.dumps(result),'i':entry.id})
    assert result['complete'] and result['qualified']
    assert judgment(ready,entry)['reason']=='FULL_RANGE_UNPROVEN'
    restored=run_entry(ready,entry,native)
    assert restored['metrics']['files_written']==0
    assert before==all_files(ready,entry) and judgment(ready,entry)['satisfied']


def test_F08_audit_detects_status_change_without_generation_change(api,tmp_path):
    client,current=api;entry=BY_ID['E68']
    run_entry(current,entry,NativeSources(current.catalog.engine))
    request=_request(client);changed=False
    def concurrent_request(*args):
        nonlocal changed
        response=request(*args)
        if not changed:
            changed=True
            status=read_entry_status(current,entry.id)
            status['coverage_pending']=['entry']
            with current.catalog.engine.begin() as c:
                c.execute(text('UPDATE data_store_entry_status SET summary_json=:j WHERE entry_id=:i'),
                          {'j':json.dumps(status),'i':entry.id})
        return response
    result=export_audit(current.catalog.engine,current.files.root,tmp_path/'concurrent',
                        entries=(entry,),api_request=concurrent_request)
    assert not result['complete']
    assert any(c['check']=='final_generation_stability' and c['code']=='DATA_CHANGED'
               for c in _checks(tmp_path/'concurrent'))


def test_F10_local_audit_cannot_claim_full_acceptance(api,tmp_path):
    client,current=api;entry=BY_ID['E68']
    run_entry(current,entry,NativeSources(current.catalog.engine),
                    options=PipelineOptions(mode='retry',partitions=('absent',)))
    # Obtain a real selected partition through the normal pipeline, without
    # making a full scan receipt: the source still gets fully read for retry.
    from app.data_store.adapters.normalize import normalize
    raw=next(NativeSources(current.catalog.engine).iter_entry(entry))
    unit=next(normalize(entry,raw));part=entry.spec.partitioner((*unit.key,'root'))
    run_entry(current,entry,NativeSources(current.catalog.engine),
              options=PipelineOptions(mode='retry',partitions=(part,)))
    local=export_audit(current.catalog.engine,current.files.root,tmp_path/'local',entries=(entry,),
                       api_request=_request(client),local_only=True)
    full=export_audit(current.catalog.engine,current.files.root,tmp_path/'full',entries=(entry,),
                      api_request=_request(client))
    assert local['complete'] and not local['global_acceptance']
    assert local['coverage_mode']=='local_only'
    assert not full['complete']
    assert any(c.get('code')=='FULL_RANGE_UNPROVEN' for c in _checks(tmp_path/'full'))
