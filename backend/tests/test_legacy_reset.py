"""LF-D03 C17/C18 on a disposable PostgreSQL schema with synthetic inputs."""
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from app.legacy_reset.catalog import ResetRefused
from app.legacy_reset.operations import (apply_database_group, apply_files, create_plan,
                                          enter, restore_tasks, status)
from app.legacy_reset.rescue import _hash_rows, export, verify
from app.data_store.local_sources import RescueSources
from app.data_store.adapters.canonical import NativeInputError
from app.data_store.adapters.registry import BY_ID
from tests.test_data_store_kernel import make_engine, test_url

pytestmark = pytest.mark.skipif(os.getenv('POSTGRES_TEST_ENABLED')!='1',
                                reason='disposable isolated PostgreSQL required')

DDL = (
    "CREATE TABLE data_store_legacy_maintenance (singleton integer PRIMARY KEY,phase text NOT NULL,plan_hash text,completed_json jsonb NOT NULL DEFAULT '[]'::jsonb,files_started boolean NOT NULL DEFAULT false,updated_at timestamptz DEFAULT clock_timestamp())",
    "CREATE TABLE data_store_legacy_task_state (task_id uuid PRIMARY KEY,task_type text,previous_state text,paused_version integer,captured_at timestamptz DEFAULT clock_timestamp())",
    "CREATE TABLE data_store_legacy_restrictions (origin_key text PRIMARY KEY,dataset text,scope_key text,target_key text,fields_json text,reason text,payload_json text,located boolean,captured_at timestamptz DEFAULT clock_timestamp())",
    "CREATE TABLE scheduled_tasks (id uuid PRIMARY KEY,task_type text,state text,version integer,updated_at timestamptz DEFAULT clock_timestamp())",
    "CREATE TABLE task_runs (id uuid PRIMARY KEY,task_id uuid,task_type text,status text,finished_at timestamptz,error_type text,error_message text)",
    "CREATE TABLE tonghuashun_observations (id uuid PRIMARY KEY,data_json text)",
    "CREATE TABLE etf_daily_bars (source text NOT NULL,ts_code text NOT NULL,trade_date date NOT NULL,close numeric,PRIMARY KEY(source,ts_code,trade_date))",
    "CREATE TABLE foundation_artifacts (id uuid PRIMARY KEY)",
    "CREATE TABLE foundation_issue_revisions (issue_id uuid,revision integer,scope_key text,instrument_id uuid,start date,\"end\" date,state text,fields_json text,reason text)",
    "CREATE TABLE foundation_record_issues (issue_id uuid,revision integer,scope_key text,target_key text,state text,fields_json text,reason text)",
    "CREATE TABLE foundation_work (id uuid PRIMARY KEY,status text,lease_until timestamptz,scope_key text,parameters_json text)",
    "CREATE TABLE foundation_baselines (id uuid PRIMARY KEY,source text,dataset text,scope_json text,observed_at timestamptz,row_count integer,content_hash text)",
    "CREATE TABLE foundation_baseline_blocks (baseline_id uuid REFERENCES foundation_baselines(id),ordinal integer,payload_json text,content_hash text,row_count integer,PRIMARY KEY(baseline_id,ordinal))",
    "CREATE TABLE foundation_table_changes (id bigint PRIMARY KEY,dataset text,before_json text,after_json text,observed_at timestamptz,key_json text)",
    "CREATE TABLE foundation_runtime_archives (archive_key text PRIMARY KEY,archive_hash text,byte_count integer)",
    "CREATE FUNCTION foundation_guard_observation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$",
    "CREATE TRIGGER foundation_observation_guard BEFORE UPDATE ON tonghuashun_observations FOR EACH ROW EXECUTE FUNCTION foundation_guard_observation()",
)


@pytest.fixture
def isolated():
    schema = 'lfd03_test_'+uuid4().hex
    admin = create_engine(test_url())
    with admin.begin() as c:
        c.execute(text(f'CREATE SCHEMA {schema}'))
        c.execute(text(f'SET LOCAL search_path={schema}'))
        for statement in DDL:
            c.execute(text(statement))
    engine = make_engine(schema)
    yield engine,schema
    engine.dispose()
    with admin.begin() as c:
        c.execute(text(f'DROP SCHEMA {schema} CASCADE'))
    admin.dispose()


def name():
    return 'lfd01_test'


def _task(engine, task_type='foundation.formalize_local_updates', state='active'):
    identity = str(uuid4())
    with engine.begin() as c:
        c.execute(text('INSERT INTO scheduled_tasks(id,task_type,state,version) VALUES '
                       '(:id,:type,:state,1)'), {'id':identity,'type':task_type,'state':state})
    return identity


def _run_groups(engine,plan,rescue=None):
    for group in ('hooks','derived','originals','functions'):
        apply_database_group(engine,plan=plan,group=group,rescue_manifest=rescue)
    apply_files(engine,plan=plan,archive_root=None)


def test_C17_old_schema_plan_apply_resume_and_protected_source(isolated):
    engine,_ = isolated
    legacy = _task(engine)
    shared = _task(engine,'data.ths.collect','active')
    with engine.begin() as c:
        c.execute(text("INSERT INTO task_runs(id,task_id,task_type,status) VALUES (:id,:task,:type,'queued')"),
                  {'id':str(uuid4()),'task':legacy,'type':'foundation.formalize_local_updates'})
        c.execute(text("INSERT INTO tonghuashun_observations(id,data_json) VALUES (:id,'{}')"),
                  {'id':str(uuid4())})
        c.execute(text("""INSERT INTO foundation_issue_revisions(issue_id,revision,scope_key,instrument_id,start,\"end\",state,fields_json,reason)
            VALUES (:id,1,'scope',:instrument,'2026-01-01','2026-01-02','confirmed','[\"close\"]','bad raw value')"""),
            {'id':str(uuid4()),'instrument':str(uuid4())})
        c.execute(text("""INSERT INTO foundation_work(id,status,scope_key,parameters_json)
            VALUES (:id,'succeeded','scope',:parameters)"""),
            {'id':str(uuid4()),'parameters':json.dumps({'dataset':'market.bar.daily'})})
    assert enter(engine,expect_database=name(),pause_task_ids=(shared,))['paused']==2
    with engine.begin() as c:
        assert c.execute(text("SELECT status FROM task_runs WHERE task_id=:id"),{'id':legacy}).scalar_one()=='skipped'
    plan = create_plan(engine,expect_database=name())
    assert not plan['blockers']
    assert plan['database']['schema'].startswith('lfd03_test_')
    assert any(row['name']=='foundation_artifacts' for row in plan['tables'])
    apply_database_group(engine,plan=plan,group='hooks')
    # A new process can resume from the durable group marker.
    assert apply_database_group(engine,plan=plan,group='hooks')['status']=='already_complete'
    for group in ('derived','originals','functions'):
        apply_database_group(engine,plan=plan,group=group)
    apply_files(engine,plan=plan,archive_root=None)
    assert status(engine,expect_database=name())['phase']=='reset_done'
    with engine.begin() as c:
        assert c.execute(text('SELECT count(*) FROM tonghuashun_observations')).scalar_one()==1
        assert c.execute(text("SELECT state FROM scheduled_tasks WHERE id=:id"),{'id':legacy}).scalar_one()=='paused'
        assert c.execute(text("SELECT count(*) FROM task_runs WHERE task_id=:id"),{'id':legacy}).scalar_one()==1
        assert c.execute(text('SELECT dataset FROM data_store_legacy_restrictions WHERE located=false')).scalar_one()=='market.bar.daily'
    assert restore_tasks(engine,expect_database=name())['restored']==1
    with engine.begin() as c:
        assert c.execute(text('SELECT state FROM scheduled_tasks WHERE id=:id'),{'id':shared}).scalar_one()=='active'
    with pytest.raises(ResetRefused):
        restore_tasks(engine,expect_database=name(),include_legacy=True)


def test_C18_wrong_database_unknown_object_fk_and_active_writer(isolated):
    engine,_ = isolated
    with pytest.raises(ResetRefused) as wrong:
        create_plan(engine,expect_database='not_the_test_database')
    assert wrong.value.code=='WRONG_DATABASE'
    with engine.begin() as c:
        c.execute(text('CREATE TABLE foundation_surprise (id integer)'))
    assert any(v['code']=='UNKNOWN_LEGACY_OBJECT' for v in create_plan(engine,expect_database=name())['blockers'])
    with engine.begin() as c:
        c.execute(text('DROP TABLE foundation_surprise'))
        c.execute(text('CREATE TABLE protected_child (id uuid REFERENCES foundation_artifacts(id))'))
    assert any(v['code']=='EXTERNAL_FK' for v in create_plan(engine,expect_database=name())['blockers'])
    with engine.begin() as c:
        c.execute(text('DROP TABLE protected_child'))
        c.execute(text("INSERT INTO foundation_work(id,status) VALUES (:id,'running')"),{'id':str(uuid4())})
    with pytest.raises(ResetRefused) as active:
        enter(engine,expect_database=name())
    assert active.value.code=='LEGACY_WRITER_ACTIVE'


def test_C18_unique_original_must_be_exported_and_reader_can_load_it(isolated,tmp_path):
    engine,_ = isolated
    row={'source':'tushare','ts_code':'510300.SH','trade_date':'2026-01-02','close':42}
    baseline = str(uuid4())
    with engine.begin() as c:
        c.execute(text("""INSERT INTO foundation_baselines(id,source,dataset,scope_json,observed_at,row_count,content_hash)
            VALUES (:id,'tushare','etf_daily',:scope,:when,1,:hash)"""),
            {'id':baseline,'scope':json.dumps({'table':'etf_daily_bars'}),
             'when':datetime(2026,1,3,tzinfo=timezone.utc),'hash':_hash_rows([row])})
        c.execute(text("""INSERT INTO foundation_baseline_blocks
            (baseline_id,ordinal,payload_json,content_hash,row_count)
            VALUES (:id,0,:payload,:hash,1)"""),
            {'id':baseline,'payload':json.dumps([row]),'hash':_hash_rows([row])})
    enter(engine,expect_database=name())
    plan = create_plan(engine,expect_database=name())
    with pytest.raises(ResetRefused) as missing:
        apply_database_group(engine,plan=plan,group='derived')
    assert missing.value.code=='GROUP_ORDER'
    apply_database_group(engine,plan=plan,group='hooks')
    with pytest.raises(ResetRefused) as missing:
        apply_database_group(engine,plan=plan,group='derived')
    assert missing.value.code=='ORIGINALS_NOT_PRESERVED'
    rescue = tmp_path/'rescued.jsonl.gz'
    manifest = tmp_path/'rescue-manifest.json'
    export(engine,plan=plan,output=rescue,manifest_path=manifest)
    checked = verify(engine,plan=plan,rescue_path=rescue,manifest_path=manifest)
    assert checked['record_count']==1 and checked['verified']
    values = list(RescueSources([rescue]).iter_entry(BY_ID['E70']))
    assert len(values)==1 and values[0].content['close']==42
    _run_groups(engine,plan,checked)
    with engine.begin() as c:
        assert c.execute(text('SELECT count(*) FROM etf_daily_bars')).scalar_one()==0
    assert status(engine,expect_database=name())['phase']=='reset_done'


def test_C18_archive_symlink_and_dynamic_function_dependency_refuse(isolated,tmp_path):
    engine,_ = isolated
    root = tmp_path/'old-archives';root.mkdir()
    outside = tmp_path/'outside';outside.write_bytes(b'unrelated')
    link = root/('a'*64+'.tar');link.symlink_to(outside)
    with engine.begin() as c:
        c.execute(text('INSERT INTO foundation_runtime_archives(archive_key,archive_hash,byte_count) '
                       'VALUES (:key,:hash,1)'), {'key':link.name,'hash':'a'*64})
        c.execute(text("""CREATE FUNCTION other_reader() RETURNS integer LANGUAGE plpgsql AS $$
            BEGIN RETURN (SELECT count(*) FROM foundation_artifacts); END $$"""))
    enter(engine,expect_database=name())
    plan = create_plan(engine,expect_database=name(),archive_root=root)
    assert {v['code'] for v in plan['blockers']} >= {'ARCHIVE_PATH_UNSAFE','EXTERNAL_FUNCTION_BODY'}
    with pytest.raises(ResetRefused):
        apply_database_group(engine,plan=plan,group='hooks',archive_root=root)


def test_C18_original_changed_after_verify_refuses_before_derived_drop(isolated,tmp_path):
    engine,_ = isolated
    row={'source':'tushare','ts_code':'510300.SH','trade_date':'2026-01-02','close':42}
    first=str(uuid4())
    with engine.begin() as c:
        c.execute(text("""INSERT INTO foundation_baselines
            (id,source,dataset,scope_json,observed_at,row_count,content_hash)
            VALUES (:id,'tushare','etf_daily','{}','2026-01-03',1,:hash)"""),
            {'id':first,'hash':_hash_rows([row])})
        c.execute(text("""INSERT INTO foundation_baseline_blocks
            (baseline_id,ordinal,payload_json,content_hash,row_count)
            VALUES (:id,0,:payload,:hash,1)"""),
            {'id':first,'payload':json.dumps([row]),'hash':_hash_rows([row])})
    enter(engine,expect_database=name())
    plan=create_plan(engine,expect_database=name())
    rescue=tmp_path/'rescue.jsonl.gz'; manifest=tmp_path/'rescue.json'
    export(engine,plan=plan,output=rescue,manifest_path=manifest)
    checked=verify(engine,plan=plan,rescue_path=rescue,manifest_path=manifest)
    apply_database_group(engine,plan=plan,group='hooks')
    second=str(uuid4())
    with engine.begin() as c:
        c.execute(text("""INSERT INTO foundation_baselines
            (id,source,dataset,scope_json,observed_at,row_count,content_hash)
            VALUES (:id,'tushare','etf_daily','{}','2026-01-04',1,:hash)"""),
            {'id':second,'hash':_hash_rows([row])})
        c.execute(text("""INSERT INTO foundation_baseline_blocks
            (baseline_id,ordinal,payload_json,content_hash,row_count)
            VALUES (:id,0,:payload,:hash,1)"""),
            {'id':second,'payload':json.dumps([row]),'hash':_hash_rows([row])})
    with pytest.raises(ResetRefused) as changed:
        apply_database_group(engine,plan=plan,group='derived',rescue_manifest=checked)
    assert changed.value.code=='ORIGINALS_CHANGED'
    with engine.begin() as c:
        assert c.execute(text("SELECT to_regclass('foundation_artifacts') IS NOT NULL")).scalar_one()
        assert c.execute(text('SELECT completed_json FROM data_store_legacy_maintenance')).scalar_one()==['hooks']


def test_C17_owned_sequence_archive_and_repeated_apply(isolated,tmp_path):
    engine,_ = isolated
    root=tmp_path/'foundation-runtime-archives';root.mkdir()
    payload=b'owned archive'
    digest=hashlib.sha256(payload).hexdigest()
    archive=root/(digest+'.tar');archive.write_bytes(payload)
    with engine.begin() as c:
        c.execute(text('ALTER TABLE foundation_artifacts ADD COLUMN serial_no bigint GENERATED BY DEFAULT AS IDENTITY'))
        c.execute(text("""INSERT INTO foundation_runtime_archives(archive_key,archive_hash,byte_count)
            VALUES (:key,:hash,:size)"""),
            {'key':archive.name,'hash':digest,'size':len(payload)})
    enter(engine,expect_database=name())
    plan=create_plan(engine,expect_database=name(),archive_root=root)
    assert not plan['blockers']
    assert any(row['name']=='foundation_artifacts_serial_no_seq' for row in plan['sequences'])
    for group in ('hooks','derived','originals','functions'):
        apply_database_group(engine,plan=plan,group=group,archive_root=root)
    apply_files(engine,plan=plan,archive_root=root)
    assert not archive.exists()
    for group in ('hooks','derived','originals','functions'):
        assert apply_database_group(engine,plan=plan,group=group,archive_root=root)['status']=='already_complete'
    assert apply_files(engine,plan=plan,archive_root=root)['status']=='already_complete'
    with engine.begin() as c:
        c.execute(text('CREATE TABLE foundation_surprise (id integer)'))
    with pytest.raises(ResetRefused) as changed:
        apply_database_group(engine,plan=plan,group='hooks',archive_root=root)
    assert changed.value.code=='CATALOG_CHANGED'


def test_C18_rescued_staged_dump_requires_original_collection_time(tmp_path):
    when='2026-01-02T00:00:00+00:00'
    record={'format':'qf-local-rescue-v1','kind':'staged_dump','entry_id':'E50',
            'observed_at':'2026-01-03T00:00:00+00:00','record':{
                'format':'local-staged-dump-v1','subject':'600000.SH','native_dataset':'stock_daily',
                'content':{'item':[],'bulk_source':{'sha256':'a'*64,'collected_at':when}}}}
    rescue=tmp_path/'stage.jsonl.gz'
    with gzip.open(rescue,'wt',encoding='utf-8') as stream:
        stream.write(json.dumps(record)+'\n')
    values=list(RescueSources([rescue]).iter_entry(BY_ID['E50']))
    assert len(values)==1 and values[0].observed_at==when
    record['record']['content']['bulk_source']['collected_at']='2026-01-04T00:00:00+00:00'
    with gzip.open(rescue,'wt',encoding='utf-8') as stream:
        stream.write(json.dumps(record)+'\n')
    with pytest.raises(NativeInputError) as late:
        list(RescueSources([rescue]).iter_entry(BY_ID['E50']))
    assert late.value.code=='SOURCE_ORDER_UNPROVEN'
