"""Required D17 integration suite, explicitly run by S3 Rust contracts CI.

Actual process inspection must be available: GitHub's nondumpable same-UID
listener deliberately blocks unprivileged cleanup. The dedicated CI command
runs this unchanged refusal/apply contract with host inspection permissions;
ordinary Validate retains its full existing backend/frontend checks.
"""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app.backtest_service.history.storage_models import BacktestRunRecord
from app.core.config import get_settings

REPO = Path(__file__).resolve().parents[2]
loader = importlib.util.spec_from_file_location('d17_cleanup', REPO/'scripts/backtest_cleanup.py')
cleanup = importlib.util.module_from_spec(loader)
loader.loader.exec_module(cleanup)
BLOCKER = '''
import importlib.abc,sys
class NoRetired(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if any(fullname==n or fullname.startswith(n+'.') for n in ('app.backtesting','app.strategy_protocol')):
            raise ImportError('retired execution import blocked: '+fullname)
sys.meta_path.insert(0,NoRetired())
'''


def test_physical_source_retirement_and_blocked_product_startup():
    assert not (REPO/'backend/app/backtesting').exists()
    assert not (REPO/'backend/app/strategy_protocol').exists()
    code = BLOCKER + '''
import app.models,app.main,app.runner.__main__,app.strategies.validation
import app.data_ingestion.services.trade_calendar,app.data_ingestion.services.etf_daily
import app.data_ingestion.services.etf_adjustment,app.data_ingestion.scheduler_tasks.corporate_action
from app.main import create_app
from app.core.config import get_settings
app=create_app(get_settings())
assert '/api/admin/backtests' in app.openapi()['paths']
from app.strategies.validation import validate_s3_source
from app.calendars import facts,sql
assert not hasattr(facts,'resolve_calendar_axis')
assert not hasattr(facts,'register_calendar_axis_policy')
assert not hasattr(sql.SqlCalendarFactProvider,'open_calendar_snapshot')
assert validate_s3_source('def initialize(context): pass').valid
assert not validate_s3_source('def run(context,parameters): pass').valid
assert not any(n.startswith(('app.backtesting','app.strategy_protocol')) for n in sys.modules)
'''
    result = subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr


def test_no_active_static_or_dynamic_retired_entrypoints():
    loader = importlib.util.spec_from_file_location('d17_inventory',REPO/'scripts/scan_legacy_dependencies.py')
    inventory = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(inventory)
    result = inventory.scan(REPO,max_bytes=32*1024**2)
    assert result['complete'],result['skipped']
    assert result['candidates']==[]
    active = ('backend/app/','frontend/src/','examples/','.github/','compose.yaml','Makefile')
    imports = [r for r in result['references'] if r['path'].startswith(active)
               and r['kind'] in ('python_import','python_module_string','legacy_module_reference')]
    assert imports==[]
    # Tombstones/history GETs remain valid; frontend writes must use S3 v2.
    for p in (REPO/'frontend/src').rglob('*'):
        if p.suffix in {'.ts','.tsx'} and not p.name.endswith(('.test.ts','.test.tsx')):
            body=p.read_text()
            assert '/api/admin/internal/backtest' not in body
            assert '/api/internal/backtests' not in body
            assert 'app.strategy_protocol' not in body
    for filename in ('backend/pyproject.toml','sdk/python/pyproject.toml','compose.yaml','Makefile'):
        body=(REPO/filename).read_text()
        assert 'app.backtesting' not in body and 'app.strategy_protocol' not in body


@pytest.fixture(scope='module')
def database():
    if os.environ.get('POSTGRES_TEST_ENABLED')!='1':
        pytest.skip('private PostgreSQL enabled in required CI')
    settings=get_settings()
    assert settings.environment=='test' and 'test' in settings.database_name
    name='qf_d17_test_'+uuid4().hex
    admin=create_engine(settings.database_url,isolation_level='AUTOCOMMIT')
    with admin.connect() as c:
        c.execute(text('CREATE DATABASE "'+name+'"'))
    engine=create_engine(settings.database_url.set(database=name))
    try:
        env=dict(os.environ,QF_DATABASE_NAME=name)
        result=subprocess.run([sys.executable,'-c',BLOCKER+"from alembic.config import main; main(['upgrade','head'])"],
                              cwd=REPO/'backend',env=env,capture_output=True,text=True,timeout=120)
        assert result.returncode==0,result.stderr
        with Session(engine) as s:
            run=BacktestRunRecord(id=uuid4(),run_kind='backtest_run',profile='formal@1',status='succeeded',
                terminal_status='succeeded',idempotency_key=uuid4().hex,config_hash='a'*64,
                result_summary={'original':'中文旧错误原文','equity':'123456789.123456789012345678'},
                finished_at=__import__('datetime').datetime.now(__import__('datetime').timezone.utc),runner_exit_code=0)
            s.add(run);s.commit()
            identity=run.id
        yield engine,name,identity
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text('DROP DATABASE "'+name+'"'))
        admin.dispose()


@pytest.fixture
def objects(database):
    engine,name,run=database
    with tempfile.TemporaryDirectory(prefix='qf-d17-private-') as tmp:
        root=Path(tmp)/'retired-work'
        root.mkdir(mode=0o700)
        target=root/'finished-attempt'
        target.mkdir(mode=0o700)
        (target/'scratch.bin').write_bytes(b'only this explicit temporary file')
        (target/'nested').mkdir(mode=0o700)
        (target/'nested/trace.tmp').write_text('temporary diagnostic cache')
        root_id=str(uuid4())
        table='d17_disposable_'+uuid4().hex
        with engine.begin() as c:
            db_identity=cleanup._database(c,name)
            c.execute(text('CREATE TABLE public."'+table+'" (scratch integer)'))
            comment=json.dumps({'kind':cleanup.TABLE_KIND,'root_id':root_id})
            c.execute(text('COMMENT ON TABLE public."'+table+'" IS \''+comment.replace("'","''")+'\''))
        (root/cleanup.ROOT_MARKER).write_text(json.dumps({'kind':cleanup.ROOT_KIND,'root_id':root_id,'database':db_identity}))
        (target/cleanup.OBJECT_MARKER).write_text(json.dumps({'kind':cleanup.PATH_KIND,'root_id':root_id,
                                    'run_id':str(run),'files':['scratch.bin','nested/trace.tmp']}))
        try:
            yield engine,name,root,table,run
        finally:
            # Remove only the exact empty disposable table created here, never
            # a dependency or business row. The whole private DB is owned above.
            with engine.begin() as c:
                c.execute(text('DROP TABLE IF EXISTS public."'+table+'" RESTRICT'))


def _plan(objects,c):
    _,name,root,table,_=objects
    return cleanup.plan(c,root,name,['finished-attempt'],['public.'+table])


def _snapshot(engine):
    with engine.connect() as c:
        return [dict(r) for r in c.execute(text('SELECT * FROM public.backtest_runs ORDER BY id')).mappings()]


def test_default_dry_run_is_read_only_and_apply_removes_exact_isolated_objects(objects,monkeypatch,capsys):
    engine,name,root,table,run=objects
    before=_snapshot(engine)
    disk={str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    url=engine.url.render_as_string(hide_password=False)
    monkeypatch.setenv('QF_D17_CLEANUP_DATABASE_URL',url)
    plan_file=root.parent/'plan.json'
    redirected=root.parent/'output-link';redirected.symlink_to(root/'finished-attempt',target_is_directory=True)
    assert cleanup.main(['--database-name',name,'--root',str(root),'--path','finished-attempt',
                         '--table','public.'+table,'--output',str(redirected/'report.json')])==2
    assert json.loads(capsys.readouterr().err)['mode']=='refused'
    assert not (root/'finished-attempt/report.json').exists()
    assert cleanup.main(['--database-name',name,'--root',str(root),'--path','finished-attempt',
                         '--table','public.'+table,'--output',str(plan_file)])==0
    response=json.loads(capsys.readouterr().out)
    assert response['mode']=='dry-run' and response['plan']['deletion_authorized'] is False
    assert disk=={str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    assert _snapshot(engine)==before
    assert cleanup.main(['--apply-plan',str(plan_file)])==0
    applied=json.loads(capsys.readouterr().out)
    assert applied['removed_paths']==['finished-attempt'] and applied['removed_tables']==['public.'+table]
    assert not (root/'finished-attempt').exists()
    assert (root/cleanup.ROOT_MARKER).exists()
    assert not inspect(engine).has_table(table)
    assert _snapshot(engine)==before
    if os.environ.get('QF_D17_EVIDENCE'):
        evidence=Path(os.environ['QF_D17_EVIDENCE']);evidence.mkdir(parents=True,exist_ok=True)
        (evidence/'cleanup-isolated.json').write_text(json.dumps(dict(kind='isolated_cleanup_regression',production_executed=False,
            plan=response['plan'],apply=applied,protected_before=before,protected_after=_snapshot(engine),
            dry_run_files_unchanged=True),ensure_ascii=False,indent=2,default=str)+'\n')


@pytest.mark.parametrize('case',['wrong_database','wrong_root','soft_link','hard_link','unknown_file','shared_root','business_reference','active_run','indeterminate_run','table_nonempty','table_fk','table_trigger','unknown_table','unknown_namespace','table_view','protected_table'])
def test_cleanup_refuses_unsafe_or_unknown_objects(objects,case):
    engine,name,root,table,run=objects
    extra=None
    with engine.begin() as c:
        c.execute(text("SET LOCAL statement_timeout='5s'"))
        target=root/'finished-attempt'
        if case=='wrong_database':
            name='different_database'
        elif case=='wrong_root':
            root=root.parent
        elif case=='soft_link':
            (target/'scratch.bin').unlink();(target/'scratch.bin').symlink_to('/etc/hosts')
        elif case=='hard_link':
            os.link(target/'scratch.bin',root/'extra-link')
        elif case=='unknown_file':
            (target/'business-results.json').write_text('must keep')
        elif case=='shared_root':
            root=get_settings().s3_result_root
        elif case=='business_reference':
            c.execute(text('UPDATE backtest_runs SET result_summary=:v WHERE id=:id'),{'v':json.dumps({'path':str(target)}),'id':run})
        elif case in ('active_run','indeterminate_run'):
            status='running' if case=='active_run' else 'indeterminate'
            # Test the gate without modifying protected real fixture rows.
            c.execute(text("UPDATE backtest_runs SET status='cancel_requested',terminal_status=NULL,finished_at=NULL WHERE id=:id"),{'id':run}) if status=='running' else c.execute(text("UPDATE backtest_runs SET status='indeterminate',terminal_status='indeterminate',terminal_decision_reason='unknown' WHERE id=:id"),{'id':run})
        elif case=='table_nonempty':
            c.execute(text('INSERT INTO "'+table+'" VALUES(1)'))
        elif case=='table_fk':
            c.execute(text('ALTER TABLE "'+table+'" ADD PRIMARY KEY(scratch)'))
            c.execute(text('ALTER TABLE "'+table+'" ADD FOREIGN KEY(scratch) REFERENCES "'+table+'"(scratch)'))
        elif case=='table_trigger':
            c.execute(text('CREATE FUNCTION d17_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$'))
            c.execute(text('CREATE TRIGGER d17_trigger BEFORE INSERT ON "'+table+'" FOR EACH ROW EXECUTE FUNCTION d17_guard()'))
            extra='function'
        elif case=='unknown_table':
            c.execute(text('CREATE TABLE d17_unknown_object(scratch integer)'));extra='table'
        elif case=='unknown_namespace':
            c.execute(text('CREATE SCHEMA pgX_unknown'))
        elif case=='table_view':
            c.execute(text('CREATE VIEW d17_unknown_view AS SELECT * FROM "'+table+'"'));extra='view'
        elif case=='protected_table':
            table='backtest_runs'
        with pytest.raises((cleanup.Refused,OSError,ValueError)) as denied:
            cleanup.plan(c,root,name,['finished-attempt'],['public.'+table])
        if os.environ.get('QF_D17_EVIDENCE'):
            evidence=Path(os.environ['QF_D17_EVIDENCE']);evidence.mkdir(parents=True,exist_ok=True)
            with (evidence/'cleanup-refusals.jsonl').open('a') as output:
                output.write(json.dumps(dict(case=case,refused=True,code=getattr(denied.value,'code',type(denied.value).__name__)))+'\n')
        # Roll back fixture changes so no unsafe DROP/DELETE is needed.
        c.rollback()
    assert (objects[2]/'finished-attempt').exists()
    assert inspect(engine).has_table(objects[3])


@pytest.mark.parametrize('change',['file_replace','root_replace','table_replace','reference_insert'])
def test_apply_rechecks_same_identity_and_current_references(objects,change):
    engine,name,root,table,run=objects
    with engine.begin() as c:
        wanted=_plan(objects,c)
    if change=='file_replace':
        p=root/'finished-attempt/scratch.bin';value=p.read_bytes();p.unlink();p.write_bytes(value)
    elif change=='root_replace':
        root.rename(root.parent/'kept-original')
        import shutil
        shutil.copytree(root.parent/'kept-original',root)
    elif change=='table_replace':
        with engine.begin() as c:
            c.execute(text('DROP TABLE "'+table+'" RESTRICT'))
            c.execute(text('CREATE TABLE "'+table+'"(scratch integer)'))
            comment=json.dumps({'kind':cleanup.TABLE_KIND,'root_id':wanted['marker']['root_id']})
            c.execute(text('COMMENT ON TABLE "'+table+'" IS \''+comment.replace("'","''")+'\''))
    elif change=='reference_insert':
        with engine.begin() as c:
            c.execute(text('UPDATE backtest_runs SET result_summary=:v WHERE id=:id'),{'v':json.dumps({'path':str(root)}),'id':run})
    with engine.begin() as c:
        with pytest.raises(cleanup.Refused):cleanup.apply_plan(c,wanted)
    assert (root/'finished-attempt/scratch.bin').exists()
    assert inspect(engine).has_table(table)
    if change=='reference_insert':
        with engine.begin() as c:
            c.execute(text('UPDATE backtest_runs SET result_summary=:v WHERE id=:id'),{'v':json.dumps({'original':'中文旧错误原文','equity':'123456789.123456789012345678'}),'id':run})


def test_cleanup_rejects_real_active_worker_and_parallel_database_writer(objects):
    engine,_,root,table,_=objects
    process=subprocess.Popen([sys.executable,'-u','-c',"import time;print('ready',flush=True);time.sleep(30)"],
                             cwd=root/'finished-attempt',stdout=subprocess.PIPE,text=True)
    try:
        assert process.stdout.readline().strip()=='ready'
        with engine.begin() as c:
            with pytest.raises(cleanup.Refused,match='ACTIVE_PATH_USER'):_plan(objects,c)
    finally:
        process.terminate();process.wait(timeout=10)
        process.stdout.close()
    with engine.begin() as c:
        wanted=_plan(objects,c)
    other=engine.connect();transaction=other.begin()
    try:
        other.execute(text('LOCK TABLE "'+table+'" IN ROW EXCLUSIVE MODE'))
        with engine.begin() as c:
            with pytest.raises(Exception):cleanup.apply_plan(c,wanted)
        assert (root/'finished-attempt').exists()
    finally:
        transaction.rollback();other.close()


def test_cleanup_refuses_unknown_process_visibility(objects,monkeypatch):
    """An unreadable live process is never ignored to obtain a cleanup plan."""
    engine,_,root,_,_=objects
    before=_snapshot(engine)
    process=subprocess.Popen([sys.executable,'-u','-c',"import time;print('ready',flush=True);time.sleep(30)"],
                             stdout=subprocess.PIPE,text=True)
    try:
        assert process.stdout.readline().strip()=='ready'
        readlink=cleanup.os.readlink
        def denied(path,*args,**kwargs):
            if str(path)==f'/proc/{process.pid}/cwd':
                raise PermissionError('private unreadable process fixture')
            return readlink(path,*args,**kwargs)
        monkeypatch.setattr(cleanup.os,'readlink',denied)
        with engine.begin() as c:
            with pytest.raises(cleanup.Refused,match='PROCESS_ACTIVITY_UNKNOWN'):_plan(objects,c)
        assert (root/'finished-attempt/scratch.bin').exists()
        assert _snapshot(engine)==before
    finally:
        process.terminate();process.wait(timeout=10)
        process.stdout.close()


def test_cleanup_rejects_actual_stopped_container_mount(objects):
    """A stopped container still references its mount; no labels imply safety."""
    from app.backtest_service.process import DockerDaemon
    daemon= DockerDaemon()
    images=daemon.request('GET','/images/json')
    # The required CI/private PostgreSQL fixture uses postgres:17. Do not pull
    # or replace any images, and never touch a container not created here.
    postgres=[i for i in images if any(t.startswith('postgres:17') for t in i.get('RepoTags') or [])]
    assert postgres, 'required private PostgreSQL image is unavailable'
    root=objects[2]
    created=daemon.request('POST','/containers/create',{
        'Image':postgres[0]['Id'],'Cmd':['true'],
        'Labels':{'io.quantfoundry.s3.d17.private-test-root':str(root)},
        'HostConfig':{'NetworkMode':'none','Mounts':[{'Type':'bind','Source':str(root),'Target':'/qf-private-temp','ReadOnly':True}]}})
    container=created['Id']
    try:
        assert daemon.inspect(container)['State']['Running'] is False
        with objects[0].begin() as c:
            with pytest.raises(cleanup.Refused,match='CONTAINER_REFERENCE'):_plan(objects,c)
        assert (root/'finished-attempt/scratch.bin').exists()
    finally:
        # Remove only this exact private container and its anonymous fixture
        # volume; never enumerate/delete unrelated daemon resources.
        daemon.request('DELETE','/containers/'+container+'?v=1')
