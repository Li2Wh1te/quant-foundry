#!/usr/bin/env python3
"""One approved nine-task source window, with deployment-host recovery.

This operator runs on the existing Linux Docker host. It pauses only future
scheduled enqueue through the existing authenticated API; queued/running work
still uses the ordinary scheduler. A separate, detached watchdog is armed before
the first pause and owns the same private receipts after SSH/operator loss.
Source acquisition remains the existing pinned bounded_batch, once, unchanged.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time
import math
from uuid import UUID, uuid4

APP_HEAD = 'c205736dbcfc6488a6dfe172dec3da5cd8aff8e4'
APP_IMAGE = 'sha256:0846f2340d53650c564430eb4e39699895d0d03e36632abc5d78ad14e0ba6006'
PROJECT = 'quant-foundry-p01'
PROJECT_DIRECTORY = '/home/lemon/quant-foundry-p01-1da2116'
SOURCE_ROOT = '/app/data/logs/lf-r01-c205736-source-three-five-http-review-20261005'
SOURCE_SHA = 'd630c0f94d15a16fa2fdfe006a9970bb6b2da1539faf703da3295a1f223d7549'
SOURCE_CONTAINER = 'qfr01-source-three-five-c205736-attempt1'
ORIGINAL_SHA = 'da6574ddfcd8ef38cd5d37b41978de28e69613ed28eca8b4b17901b5247977e6'
CONFIG_SHA = '53fa02fdab80678ad03c500351566b6477b41ceb0482bf60b46f6b15a8a50744'
UPDATER = '54af16b2-3f21-4650-9364-d01adbfefde7'
FIELDS = ('id', 'name', 'description', 'task_type', 'parameters',
          'parameter_version', 'schedule', 'state', 'concurrency_limit',
          'overlap_policy', 'queue_limit', 'priority', 'version')
DEFINITION = tuple(k for k in FIELDS if k not in ('state', 'version'))
# These are the reviewed original task IDs, not a dynamic type-wide pause.
TARGETS = {
    'e218e3a0-05ff-4b1c-966d-909aedc65509': ('stock_daily', 'incremental', '3b6eab3fc92274b3685358d262f95eb0c56bb33ca378b96f1e97e736fa44d82f'),
    'b1b965e5-fbc7-4169-9522-7fa67e23e3e6': ('stock_daily', 'backfill', '538c39a95203dcacf1295a20b868a91b0a6209ec2a0b07361e852189d7e43c9a'),
    'ac75e269-1fd8-427f-9667-531a1b67ed2e': ('stock_daily', 'reconcile', '7befe56bb9c03c5e1100dec95367fd7ef6e3efc303c51d30dda520486d4b99e1'),
    '21728d4e-a88d-4ba1-bb3e-e11917ff59f4': ('fund_stock_history', 'incremental', 'a3fb6125d4887aa75a5ed852b2ddb2d09347eccc4d5245a029cc7466aa78b7ec'),
    '21d12ee0-771f-433f-bd08-8888cec84ce8': ('fund_stock_history', 'backfill', '011f6b4dc110aaaea1ab57296b1aaa177fa380f786626bbbf053cbfddb9da5a3'),
    '02d42c8b-5ee2-4130-a868-33f4cef6627f': ('fund_stock_history', 'reconcile', '92a8f7d4e1335704acc07e1cfdafb5f94b4f840eb1b5783380f95491f18be84d'),
    '3df7869a-d4e7-4135-9130-19c6186fd01e': ('fund_bond_history', 'incremental', 'a3fb6125d4887aa75a5ed852b2ddb2d09347eccc4d5245a029cc7466aa78b7ec'),
    '8b7e590b-53ef-4072-9c8c-fa785752ce4e': ('fund_bond_history', 'backfill', '011f6b4dc110aaaea1ab57296b1aaa177fa380f786626bbbf053cbfddb9da5a3'),
    '90b0a873-4fbd-40fe-8db1-3c47b2a68d10': ('fund_bond_history', 'reconcile', '92a8f7d4e1335704acc07e1cfdafb5f94b4f840eb1b5783380f95491f18be84d'),
}
SCOPE_ASSETS = {'stock_daily': 'a-share', 'fund_stock_history': 'fund-otc',
                'fund_bond_history': 'fund-lof'}
TOTAL, DRAIN, LAUNCH, BATCH, RECOVERY = 3600, 2400, 60, 840, 300
TERMINAL = ('restored', 'recovery_required')
APP_USERS = ('999', '999:999', 'app', 'app:app')
# This private one-off override never updates the existing runner service. Both
# Compose restart-policy forms are explicit so the bounded CLI cannot inherit
# the ordinary runner's unless-stopped policy and start another process attempt.
SOURCE_OVERRIDE = {'services': {'runner': {'restart': 'no', 'pull_policy': 'never',
    'deploy': {'restart_policy': {'condition': 'none'}}}}}


class Refused(RuntimeError):
    def __init__(self, code, *, uncertain=False):
        self.code, self.uncertain = code, uncertain
        super().__init__(code)


def require(value, code):
    if not value:
        raise Refused(code)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def utc():
    return datetime.now(timezone.utc).isoformat()


def private_directory(path):
    path = Path(path)
    require(path.is_absolute() and path.resolve() == path and '..' not in path.parts,
            'CANONICAL_PRIVATE_DIRECTORY_REQUIRED')
    info = path.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and not info.st_mode & 0o077, 'PRIVATE_DIRECTORY_REQUIRED')


def read_private(path):
    path = Path(path)
    private_directory(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and not info.st_mode & 0o077 and info.st_size <= 4 * 1024**2,
                'PRIVATE_FILE_REQUIRED')
        with os.fdopen(fd, 'rb', closefd=False) as handle:
            return json.load(handle)
    finally:
        os.close(fd)


def write_private(path, value, *, exclusive=False):
    """Durably publish bounded evidence; never follow a replacement symlink."""
    path = Path(path)
    private_directory(path.parent)
    raw = encoded(value) + b'\n'
    require(len(raw) <= 4 * 1024**2, 'EVIDENCE_BUDGET_EXCEEDED')
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        if exclusive:
            os.link(temporary, path, follow_symlinks=False)
        else:
            require(not path.is_symlink(), 'UNSAFE_JOURNAL_PATH')
            os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


class Journal:
    """Short atomic transactions, separate from the single-restorer lock.

    No network call or source process owns the journal lock. The independent
    watchdog must be able to read receipts even when the operator is blocked.
    """
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def lock(self, suffix='.lock', *, blocking=True):
        private_directory(self.path.parent)
        fd = os.open(str(self.path) + suffix, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                    and not info.st_mode & 0o077, 'UNSAFE_JOURNAL_LOCK')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                raise Refused('RESTORER_BUSY') from None
            yield
        finally:
            os.close(fd)

    def read(self):
        envelope = read_private(self.path)
        value = envelope['payload']
        require(envelope['sha256'] == digest(value)
                and value['plan_sha256'] == digest(value['plan']), 'JOURNAL_CHANGED')
        return value

    def create(self, value):
        with self.lock():
            write_private(self.path, {'sha256': digest(value), 'payload': value}, exclusive=True)

    def update(self, operation):
        with self.lock():
            value = self.read()
            original_plan = digest(value['plan'])
            operation(value)
            require(digest(value['plan']) == original_plan, 'PLAN_CHANGED')
            write_private(self.path, {'sha256': digest(value), 'payload': value})
            return value


def process_identity(pid=None):
    """Use Linux start ticks plus boot ID; a reused PID never owns this window."""
    pid = os.getpid() if pid is None else pid
    raw = Path(f'/proc/{pid}/stat').read_text()
    fields = raw[raw.rindex(')') + 2:].split()
    require(fields[0] != 'Z', 'PROCESS_EXITED')
    return {'pid': pid, 'start_ticks': fields[19],
            'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}


def alive(identity):
    try:
        return process_identity(identity['pid']) == identity
    except (OSError, Refused, KeyError, ValueError):
        return False


def definition(task):
    return {k: task[k] for k in DEFINITION}


def verify_application_user(expected, configured, identity):
    """Resolve the image's named app user without changing its numeric rights.

    The production image declares USER app, which Docker resolves to 999:999.
    Require the exact Compose/container user spelling and an actual process
    proof of real/effective UID/GID; a permitted name alone is insufficient.
    """
    require(expected in APP_USERS and configured == expected and isinstance(identity, dict)
            and all(type(identity.get(key)) is int and identity[key] == 999
                    for key in ('uid', 'euid', 'gid', 'egid')), 'APP_UID_CHANGED')


def matching(run):
    key = run['task_type'].removeprefix('data.ths.')
    if key not in SCOPE_ASSETS:
        return False
    p = run['parameters']
    require(isinstance(p, dict), 'ACTIVE_SCOPE_UNVERIFIABLE')
    if p.get('start_date') is not None or p.get('end_date') is not None:
        return False
    assets = p.get('asset_types')
    if assets is not None:
        require(isinstance(assets, list) and assets and all(type(a) is str for a in assets),
                'ACTIVE_SCOPE_UNVERIFIABLE')
        if SCOPE_ASSETS[key] not in assets:
            return False
    subjects = p.get('subjects')
    target = {'stock_daily': '000933.SZ', 'fund_stock_history': '001141.OF',
              'fund_bond_history': '160225.SZ'}[key]
    return (subjects is None or not isinstance(subjects, list) or not subjects
            or any(type(s) is not str for s in subjects) or target in subjects)


def frozen(run):
    return {k: v for k, v in run.items() if k != 'status'}


def verify_source_receipt(original, result, terminal):
    """Require actual matching worker evidence, not only a batch summary."""
    batch = result['batch']
    workers, results = result['workers'], batch['results']
    require(terminal['exit_code'] in (0, 2) and not result['tagged_activity'],
            'SOURCE_EXIT_OR_RESOURCES_UNVERIFIED')
    require(batch['counts_complete'] is True and batch['process_wall_budget_met'] is True
            and batch['scopes'] == original['source_scopes']
            and batch['budget']['retries'] == 0 and batch['budget']['wall_seconds'] == 840
            and batch['budget']['scope_seconds'] == 180 and batch['budget']['http_attempts'] == 5
            and type(batch['counts']['http_attempts']) is int
            and 0 <= batch['counts']['http_attempts'] <= 5
            and len(workers) == len(results) == batch['attempts_started'] <= 3,
            'SOURCE_RECEIPT_UNVERIFIED')
    total = 0
    for index, (worker, summary) in enumerate(zip(workers, results)):
        count = worker['counts']['http_attempts']
        require(worker['operation_id'] == summary['operation_id']
                and worker['outcome'] == summary['outcome']
                and worker['counts_complete'] is True and summary['process_exited'] is True
                and worker['unknown_publication'] is False
                and type(count) is int and 0 <= count <= (1, 2, 2)[index],
                'SOURCE_WORKER_RECEIPT_UNVERIFIED')
        if worker['outcome'] == 'succeeded':
            require(worker['publication']['status'] == 'verified', 'SOURCE_PUBLICATION_UNVERIFIED')
        total += count
    require(total == batch['counts']['http_attempts'], 'SOURCE_COUNTS_UNVERIFIED')
    if batch['outcome'] == 'completed':
        require(len(workers) == 3 and terminal['exit_code'] == 0
                and all(x['outcome'] == 'succeeded' for x in workers),
                'SOURCE_COMPLETION_UNVERIFIED')
    return batch['outcome']


def validate_snapshot(snapshot, original=None, confirmed=None):
    """Other task definitions are protected, while their run lifecycles continue."""
    tasks = {t['id']: t for t in snapshot['tasks']}
    require(len(tasks) == 178 and len(snapshot['tasks']) == 178, 'TASK_SET_CHANGED')
    require(snapshot['source_configs_sha256'] == CONFIG_SHA, 'SOURCE_CONFIG_CHANGED')
    require(snapshot['attempt_exists'] is False, 'SOURCE_ATTEMPT_ALREADY_EXISTS')
    for task_id, (dataset, mode, parameters_sha) in TARGETS.items():
        t = tasks.get(task_id)
        require(t is not None and t['task_type'] == 'data.ths.' + dataset
                and digest(t['parameters']) == parameters_sha
                and t['parameters']['mode'] == mode and t['parameter_version'] == 1,
                'NINE_TASK_PARAMETERS_CHANGED')
        receipt = (confirmed or {}).get(task_id)
        require(t['state'] == ('paused' if receipt else 'active')
                and type(t['version']) is int and t['version'] == (receipt['version'] if receipt else 4),
                'NINE_TASK_STATE_OR_VERSION_CHANGED')
        require(t['schedule'] == {'type': 'cron', 'timezone': 'Asia/Shanghai',
                                 'expression': '*/10 * * * *'}
                and t['concurrency_limit'] == 1 and t['queue_limit'] == 1
                and t['overlap_policy'] == 'skip', 'NINE_TASK_SCHEDULE_CHANGED')
    require(tasks[UPDATER]['state'] == 'active' and tasks[UPDATER]['version'] == 24,
            'UPDATER_CHANGED')
    if original is None:
        originals = [t for t in snapshot['tasks'] if t['id'] != UPDATER]
        require(digest(originals) == ORIGINAL_SHA, 'ORIGINAL_TASKS_CHANGED')
    else:
        before = {t['id']: t for t in original['tasks']}
        for task_id, t in tasks.items():
            require(definition(t) == definition(before[task_id]), 'TASK_DEFINITION_CHANGED')
            if task_id not in TARGETS:
                require(t == before[task_id], 'PROTECTED_TASK_CHANGED')
    source_types = {'data.ths.' + x for x in SCOPE_ASSETS}
    require({t['id'] for t in tasks.values() if t['task_type'] in source_types} == set(TARGETS),
            'ADDITIONAL_COLLECTOR_TASK')
    for run in snapshot['runs']:
        if matching(run):
            require(run['task_id'] in TARGETS, 'UNREVIEWED_MATCHING_RUN')
    return tasks


# SQL is read-only. API authentication and provider secrets never leave the
# existing backend; only explicit metadata, hashes and bounded receipts do.
CONTAINER_CODE = r'''
import hashlib,json,sys
from pathlib import Path
from urllib.request import Request,build_opener,HTTPRedirectHandler
from urllib.error import HTTPError
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.db.session import get_engine
from app.core.config import get_settings
from app.data_store.adapters.contracts import digest
request=json.load(sys.stdin)
action=request['action']
root=Path('/app/data/logs/lf-r01-c205736-source-three-five-http-review-20261005')
fields=request['fields']
class NoRedirect(HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs):return None
try:
 if action=='change':
  from uuid import UUID
  task_id=str(UUID(request['id']));version=request['version'];target=request['target']
  assert type(version) is int and version>0 and target in ('paused','active')
  settings=get_settings();endpoint='pause' if target=='paused' else 'resume'
  url=f'http://127.0.0.1:{settings.server_port}/api/admin/tasks/{task_id}/{endpoint}?version={version}'
  with build_opener(NoRedirect).open(Request(url,method='POST',headers={'Authorization':'Bearer '+settings.api_token.get_secret_value()}),timeout=5) as response:
   assert response.status==200
   t=json.load(response)
   assert t['id']==task_id and t['state']==target and type(t['version']) is int and t['version']==version+1
   value={'id':task_id,'state':target,'version':t['version'],'definition_sha256':digest({k:t[k] for k in fields if k not in ('state','version')})}
 else:
  with Session(get_engine().execution_options(isolation_level='REPEATABLE READ')) as s:
   s.execute(text('SET TRANSACTION READ ONLY'));s.execute(text("SET statement_timeout='5s'"));s.execute(text("SET lock_timeout='2s'"))
   def rows(q,p=None):return [dict(x) for x in s.execute(text(q),p or {}).mappings()]
   if action=='snapshot':
    tasks=rows('SELECT id::text,name,description,task_type,parameters,parameter_version,schedule,state,concurrency_limit,overlap_policy,queue_limit,priority,version FROM scheduled_tasks ORDER BY id LIMIT 513')
    assert len(tasks)<=512
    run_sql='SELECT id::text,task_id::text,task_version,task_type,status,trigger_type,parameters,parameter_version,priority,created_at::text,available_at::text FROM task_runs'
    active=[]
    for key in ('stock_daily','fund_stock_history','fund_bond_history'):
     a=rows(run_sql+" WHERE task_type=:key AND status IN ('queued','running') ORDER BY id LIMIT 201",{'key':'data.ths.'+key})
     assert len(a)<=200;active.extend(a)
    ids=request.get('accepted',[]);assert len(ids)<=64
    saved=rows(run_sql+' WHERE id=ANY(CAST(:ids AS uuid[])) ORDER BY id',{'ids':ids}) if ids else []
    configs=s.execute(text('SELECT row_to_json(t) FROM (SELECT * FROM data_source_configs ORDER BY key LIMIT 65) t')).scalars().all()
    assert len(configs)<=64
    # The preserved configuration fingerprint comes from r01_service_switch's
    # ASCII-escaped JSON contract. Native/task definitions use the adapter's
    # UTF-8 contract instead; mixing these rejects unchanged Chinese messages.
    config_sha=hashlib.sha256(json.dumps(configs,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()).hexdigest()
    value={'tasks':tasks,'runs':active,'saved':saved,'source_configs_sha256':config_sha,'attempt_exists':(root/'attempt1').exists(),'utc':s.scalar(text('SELECT now()'))}
    if request.get('pins'):
     from app.data_ingestion.tonghuashun.bounded import RepairScope
     from app.data_sources.models import DataSourceConfig
     from app.data_sources.service import configured
     from app.data_ingestion.models.tonghuashun import TonghuashunTicker
     raw=(root/'plan.json').read_bytes()
     assert hashlib.sha256(raw).hexdigest()=='d630c0f94d15a16fa2fdfe006a9970bb6b2da1539faf703da3295a1f223d7549'
     scopes=[RepairScope.from_dict(p) for p in json.loads(raw)]
     assert len(scopes)==3 and [x.max_http_attempts for x in scopes]==[1,2,2]
     source=s.get(DataSourceConfig,'tonghuashun');assert source.enabled and source.version==2 and configured(source)
     for scope in scopes:
      scope.selection.baseline(s)
      ticker=s.get(TonghuashunTicker,scope.subject);identity=json.loads(ticker.raw_json)
      assert ticker.asset_type==scope.asset_type and identity['asset_type']==scope.asset_type and identity['thscode']==scope.subject
     value['pins_valid']=True
     value['source_scopes']=[scope.as_dict() for scope in scopes]
     if request.get('ready'):
      from app.data_ingestion.tonghuashun.bounded import admission
      for scope in scopes:
       scope.parameters()
       with admission(get_engine(),scope):pass
      value['all_three_admitted']=True
   elif action=='receipt':
    path=root/'attempt1'/'batch.json';assert not path.is_symlink() and path.stat().st_size<=131072
    batch=json.loads(path.read_text());workers=[];names=[]
    for index in range(1,4):
     p=root/'attempt1'/f'scope-{index:02d}'/'worker.json'
     if p.exists():
      assert not p.is_symlink() and p.stat().st_size<=131072
      w=json.loads(p.read_text());workers.append({k:w.get(k) for k in ('operation_id','outcome','publication','counts','counts_complete','unknown_publication','provenance','problem')})
      name=w.get('provenance',{}).get('db_application_name')
      if name is not None:names.append(name)
    assert len(names)<=3 and all(isinstance(n,str) and n.startswith('ths-bounded:') and len(n)<=64 for n in names)
    activity=rows('SELECT application_name,count(*) AS n FROM pg_stat_activity WHERE datname=current_database() AND application_name=ANY(CAST(:names AS text[])) GROUP BY application_name',{'names':names})
    value={'batch':batch,'workers':workers,'tagged_activity':activity}
   else:raise ValueError()
 print(json.dumps({'ok':True,'value':value},default=str))
except HTTPError as error:
 code=error.code;error.close()
 print(json.dumps({'ok':False,'reason':'API_HTTP_'+str(code),'uncertain':code not in (400,401,403,404,409,422)}))
except Exception:
 print(json.dumps({'ok':False,'reason':'CONTAINER_ACTION_UNVERIFIED','uncertain':action=='change'}))
'''


class DockerClient:
    """Keep credentials in the existing backend and bound every host call."""
    def __init__(self, manifest):
        self.manifest = manifest

    def command(self, argv, *, seconds=10, input=None):
        require(seconds > 0, 'CALL_DEADLINE_EXPIRED')
        try:
            result = subprocess.run(argv, input=input, text=True, capture_output=True,
                                    timeout=min(10, seconds), cwd=PROJECT_DIRECTORY)
        except (OSError, subprocess.TimeoutExpired):
            raise Refused('HOST_CALL_UNVERIFIED', uncertain=True) from None
        require(result.returncode == 0, 'HOST_CALL_FAILED')
        require(len(result.stdout) <= 16 * 1024**2, 'HOST_OUTPUT_BUDGET_EXCEEDED')
        return result.stdout

    def inspect(self, name, *, seconds=10):
        return json.loads(self.command(['docker', 'inspect', name], seconds=seconds))[0]

    def backend(self, *, recovery=False, seconds=10):
        # A same-image backend recreation can still serve guarded recovery.
        # Forward execution requires the exact reviewed original container IDs.
        item = self.inspect(PROJECT + '-backend-1', seconds=seconds)
        labels = item['Config']['Labels']
        require(item['State']['Running'] and item['Image'] == APP_IMAGE
                and labels.get('com.docker.compose.project') == PROJECT
                and labels.get('com.docker.compose.service') == 'backend',
                'BACKEND_IDENTITY_UNVERIFIED')
        if not recovery:
            require(item['Id'] == self.manifest['backend_id'], 'BACKEND_CHANGED')
        return item['Id']

    def call(self, action, *, seconds=10, recovery=False, **kwargs):
        require(action in ('snapshot', 'change', 'receipt'), 'UNSUPPORTED_ACTION')
        until = time.monotonic() + min(10, seconds)
        backend = self.backend(recovery=recovery, seconds=until - time.monotonic())
        if not recovery:
            self.verify_runner(seconds=until - time.monotonic())
        request = dict(action=action, fields=list(FIELDS), **kwargs)
        try:
            raw = self.command(['docker', 'exec', '-i', backend, 'python', '-B', '-c',
                                CONTAINER_CODE], seconds=until - time.monotonic(), input=json.dumps(request))
            response = json.loads(raw)
            require(type(response.get('ok')) is bool, 'CONTAINER_PROTOCOL_INVALID')
            if response['ok']:
                return response['value']
            raise Refused(response['reason'], uncertain=bool(response.get('uncertain')))
        except (KeyError, TypeError, ValueError):
            raise Refused('CONTAINER_RESPONSE_UNVERIFIED', uncertain=action == 'change') from None
        except Refused as error:
            # Docker/transport failure while an API was in flight is never a
            # definite rejection, even if the outer command returned nonzero.
            if action == 'change' and error.code.startswith(('HOST_', 'CONTAINER_')):
                error.uncertain = True
            raise

    def verify_runner(self, *, seconds=10):
        runner = self.inspect(PROJECT + '-runner-1', seconds=seconds)
        require(runner['Id'] == self.manifest['runner_id'] and runner['State']['Running']
                and runner['Image'] == APP_IMAGE, 'RUNNER_SCHEDULE_UNVERIFIED')
        return True

    def snapshot(self, *, pins=False, ready=False, accepted=(), seconds=10):
        return self.call('snapshot', pins=pins, ready=ready,
                         accepted=list(accepted), seconds=seconds)

    def task(self, task_id, *, seconds=10):
        value = self.call('snapshot', recovery=True, seconds=seconds)
        return next((t for t in value['tasks'] if t['id'] == task_id), None)

    def change(self, task_id, version, target, *, seconds=10):
        require(task_id in TARGETS and target in ('paused', 'active')
                and type(version) is int, 'UNREVIEWED_STATE_CHANGE')
        return self.call('change', id=task_id, version=version, target=target,
                         seconds=seconds, recovery=target == 'active')

    def compose_prefix(self):
        result = ['docker', 'compose', '-p', PROJECT]
        for path in self.manifest['compose_files']:
            result += ['-f', path]
        return result + ['--profile', 'p01']

    def preflight(self, *, seconds=60):
        """Prove the existing Compose runner's image, env and mount bindings.

        Full Docker/environment objects stay local to this function. Receipts
        contain only hashes and exact immutable image/container identities.
        """
        until = time.monotonic() + min(60, seconds)
        def remaining():
            return until - time.monotonic()
        backend = self.backend(seconds=remaining())
        require(not self.command(['docker', 'ps', '-aq', '--filter',
                    'name=^/' + SOURCE_CONTAINER + '$'], seconds=remaining()).strip(),
                'SOURCE_CONTAINER_ALREADY_EXISTS')
        runner = self.inspect(PROJECT + '-runner-1', seconds=remaining())
        labels = runner['Config']['Labels']
        require(runner['Id'] == self.manifest['runner_id'] and runner['State']['Running']
                and runner['Image'] == APP_IMAGE
                and labels.get('com.docker.compose.project') == PROJECT
                and labels.get('com.docker.compose.service') == 'runner', 'RUNNER_CHANGED')
        image = json.loads(self.command(['docker', 'image', 'inspect', APP_IMAGE],
                                        seconds=remaining()))[0]
        require(image['Config']['Labels'].get('org.opencontainers.image.revision') == APP_HEAD,
                'APP_REVISION_CHANGED')
        config = json.loads(self.command(self.compose_prefix() + ['config', '--format', 'json'],
                                          seconds=remaining()))
        service = config['services']['runner']
        resolved = json.loads(self.command(['docker', 'image', 'inspect', service['image']],
                                           seconds=remaining()))[0]
        require(resolved['Id'] == APP_IMAGE, 'COMPOSE_IMAGE_CHANGED')
        def environment(values):
            return dict(x.split('=', 1) for x in values)
        expected = environment(image['Config'].get('Env') or [])
        expected.update({k: str(v) for k, v in service.get('environment', {}).items()})
        require(environment(runner['Config'].get('Env') or []) == expected,
                'COMPOSE_ENVIRONMENT_CHANGED')
        expected_user = service.get('user') or image['Config'].get('User')
        identity = json.loads(self.command(['docker', 'exec', runner['Id'],
            'python', '-B', '-c', 'import json,os;print(json.dumps(dict(uid=os.getuid(),'
            'euid=os.geteuid(),gid=os.getgid(),egid=os.getegid())))'], seconds=remaining()))
        verify_application_user(expected_user, runner['Config']['User'], identity)
        declared = service.get('volumes', [])
        require(len(declared) == len(runner['Mounts']), 'COMPOSE_MOUNTS_CHANGED')
        for volume in declared:
            matches = [m for m in runner['Mounts'] if m['Destination'] == volume['target']]
            require(len(matches) == 1, 'COMPOSE_MOUNTS_CHANGED')
            actual = matches[0]
            require(actual['Type'] == volume['type']
                    and actual['RW'] == (not bool(volume.get('read_only', False))),
                    'COMPOSE_MOUNTS_CHANGED')
            if volume['type'] == 'bind':
                require(actual['Source'] == volume['source'], 'COMPOSE_MOUNTS_CHANGED')
            else:
                require(actual['Name'] == config['volumes'][volume['source']]['name'],
                        'COMPOSE_MOUNTS_CHANGED')
        return {'backend_id': backend, 'runner_id': runner['Id'], 'image': APP_IMAGE,
                'app_head': APP_HEAD, 'env_sha256': digest(expected),
                'configured_user': runner['Config']['User'], 'application_identity': identity,
                'network_mode': runner['HostConfig']['NetworkMode'],
                'mounts_sha256': digest(sorted(runner['Mounts'], key=lambda m: m['Destination']))}

    def source(self, episode, plan_sha, *, control_directory, seconds=10):
        # Detached source ownership is explicit in random, private episode
        # labels. The watchdog can fence only this one-off after lost stdout;
        # it never stops an original scheduler worker or retries creation.
        until = time.monotonic() + min(10, seconds)
        require(isinstance(episode, str) and len(episode) == 32
                and all(c in '0123456789abcdef' for c in episode), 'SOURCE_EPISODE_INVALID')
        override = Path(control_directory) / ('source-' + episode + '.compose.json')
        write_private(override, SOURCE_OVERRIDE, exclusive=True)
        argv = self.compose_prefix() + ['-f', str(override),
            'run', '-d', '--no-deps', '-T',
            '--name', SOURCE_CONTAINER, '--label', 'qf.r01.window=' + episode,
            '--label', 'qf.r01.plan=' + plan_sha, 'runner', 'python', '-m',
            'app.data_ingestion.tonghuashun.bounded_batch', '--repair-plan',
            SOURCE_ROOT + '/plan.json', '--output', SOURCE_ROOT + '/attempt1',
            '--gap-seconds', '0', '--wall-seconds', '840']
        output = self.command(argv, seconds=until - time.monotonic()).strip()
        require(len(output) == 64 and all(c in '0123456789abcdef' for c in output),
                'SOURCE_CREATION_UNVERIFIED')
        return output

    def source_state(self, value, *, seconds=10, fencing=False):
        if not value.get('source_launch_intent'):
            return None
        try:
            item = self.inspect(SOURCE_CONTAINER, seconds=seconds)
        except Refused as error:
            # Missing after a creation intent is uncertainty, not permission
            # to recreate it or to invent a zero-request source result.
            raise Refused('SOURCE_IDENTITY_UNVERIFIED', uncertain=True) from error
        labels = item['Config']['Labels']
        require(item['Image'] == APP_IMAGE
                and labels.get('com.docker.compose.project') == PROJECT
                and labels.get('qf.r01.window') == value['episode']
                and labels.get('qf.r01.plan') == value['plan_sha256']
                and (value.get('source_id') is None or item['Id'] == value['source_id']),
                'SOURCE_OWNERSHIP_UNVERIFIED')
        runtime = value['plan']['runtime']
        require(digest(dict(x.split('=', 1) for x in item['Config'].get('Env', [])))
                == runtime['env_sha256'] and runtime.get('configured_user') in APP_USERS
                and item['Config']['User'] == runtime['configured_user']
                and item['HostConfig']['NetworkMode'] == runtime['network_mode']
                and digest(sorted(item['Mounts'], key=lambda m: m['Destination']))
                == runtime['mounts_sha256'], 'SOURCE_BINDINGS_UNVERIFIED')
        if not fencing:
            require(item['HostConfig']['RestartPolicy']['Name'] in ('no', '')
                    and item['RestartCount'] == 0, 'SOURCE_RESTART_POLICY_UNVERIFIED')
        return {'id': item['Id'], 'running': item['State']['Running'],
                'exit_code': item['State']['ExitCode'], 'status': item['State']['Status']}

    def stop_source(self, value, *, seconds=10):
        # Inspection, stop and exit verification share one allowance. Each
        # Docker step must consume the remainder, never renew the fence budget.
        until = time.monotonic() + min(10, seconds)
        def remaining():
            return until - time.monotonic()
        item = self.source_state(value, seconds=remaining(), fencing=True)
        if item and item['status'] == 'created':
            # An unstarted, positively episode-labelled one-off must not remain
            # available for a delayed Docker start after recovery. This removes
            # only that temporary container, without force or volume deletion.
            removed = self.command(['docker', 'rm', item['id']], seconds=remaining()).strip()
            require(removed == item['id'], 'UNSTARTED_SOURCE_REMOVAL_UNVERIFIED')
            return {**item, 'status': 'removed_unstarted'}
        if item and (item['running'] or item['status'] == 'restarting'):
            self.command(['docker', 'stop', '--time', '5', item['id']], seconds=remaining())
            item = self.source_state(value, seconds=remaining(), fencing=True)
            require(not item['running'] and item['status'] != 'restarting', 'SOURCE_EXIT_UNVERIFIED')
        return item

    def receipt(self, *, seconds=10):
        return self.call('receipt', recovery=True, seconds=seconds)


def validate_manifest(manifest):
    require(set(manifest) == {'tool_head', 'tool_sha256', 'project_directory',
                              'compose_files', 'backend_id', 'runner_id'},
            'MANIFEST_FIELDS_INVALID')
    for name, length in (('tool_head', 40), ('tool_sha256', 64),
                         ('backend_id', 64), ('runner_id', 64)):
        token = manifest[name]
        require(isinstance(token, str) and len(token) == length
                and all(c in '0123456789abcdef' for c in token), 'MANIFEST_IDENTITY_INVALID')
    require(manifest['project_directory'] == PROJECT_DIRECTORY, 'P01_DIRECTORY_REQUIRED')
    files = manifest['compose_files']
    require(isinstance(files, list) and 1 <= len(files) <= 16
            and len(set(files)) == len(files)
            and files[0] == PROJECT_DIRECTORY + '/compose.p01.yaml', 'P01_COMPOSE_REQUIRED')
    require(all(isinstance(p, str) and Path(p).is_absolute()
                and '..' not in Path(p).parts for p in files), 'COMPOSE_PATH_INVALID')
    require(manifest['tool_sha256'] == hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'REVIEWED_TOOL_CHANGED')


def make_plan(manifest, client):
    validate_manifest(manifest)
    runtime = client.preflight()
    snapshot = client.snapshot(pins=True)
    validate_snapshot(snapshot)
    require(snapshot.get('pins_valid') is True, 'NATIVE_PINS_UNVERIFIED')
    return {'protocol': 'r01-nine-source-window@1', 'manifest': manifest,
            'created_at': utc(), 'runtime': runtime, 'snapshot': snapshot,
            'source_plan_sha256': SOURCE_SHA, 'limits': [TOTAL, DRAIN, LAUNCH, BATCH, RECOVERY],
            'target_ids': sorted(TARGETS)}


def validate_plan(plan):
    require(plan['protocol'] == 'r01-nine-source-window@1'
            and plan['target_ids'] == sorted(TARGETS)
            and plan['source_plan_sha256'] == SOURCE_SHA
            and plan['limits'] == [TOTAL, DRAIN, LAUNCH, BATCH, RECOVERY], 'PLAN_SCOPE_CHANGED')
    validate_manifest(plan['manifest'])
    validate_snapshot(plan['snapshot'])


def new_state(plan, worker):
    started = time.monotonic()
    return {'plan': plan, 'plan_sha256': digest(plan), 'episode': uuid4().hex,
            'worker': worker, 'watchdog': None, 'watchdog_ready': False,
            'started_at': utc(), 'started_monotonic': started,
            'drain_deadline': started + DRAIN, 'launch_deadline': started + DRAIN + LAUNCH,
            'restore_deadline': started + TOTAL - RECOVERY, 'deadline': started + TOTAL,
            'phase': 'arming', 'confirmed': {}, 'accepted': {}, 'intent': None,
            'unresolved': [], 'restore': {}, 'errors': [], 'observations': [],
            'source_launch_intent': False, 'source_id': None, 'source_receipt': None,
            'source_terminal': None, 'source_verification_complete': False,
            'stop_requested': None}


def validate_state(value):
    validate_plan(value['plan'])
    start = value['started_monotonic']
    require(type(start) in (float, int) and math.isfinite(start)
            and value['drain_deadline'] == start + DRAIN
            and value['launch_deadline'] == start + DRAIN + LAUNCH
            and value['restore_deadline'] == start + TOTAL - RECOVERY
            and value['deadline'] == start + TOTAL, 'WINDOW_DEADLINE_CHANGED')
    require(value['worker']['boot_id'] == process_identity()['boot_id'],
            'HOST_BOOT_CHANGED')
    if 'source_start_deadline' in value or 'launch_started_monotonic' in value:
        launch_start = value.get('launch_started_monotonic')
        require(type(launch_start) in (float, int) and math.isfinite(launch_start)
                and start <= launch_start < value['drain_deadline']
                and value.get('source_start_deadline')
                == min(value['launch_deadline'], launch_start + LAUNCH),
                'SOURCE_START_DEADLINE_CHANGED')
    if 'recovery_deadline' in value or 'recovery_started_monotonic' in value:
        recovery_start = value.get('recovery_started_monotonic')
        require(type(recovery_start) in (float, int) and math.isfinite(recovery_start)
                and recovery_start >= start and value.get('recovery_deadline')
                == min(value['deadline'], recovery_start + RECOVERY),
                'RECOVERY_DEADLINE_CHANGED')
    require(set(value['confirmed']).issubset(TARGETS), 'RESTORE_SCOPE_CHANGED')
    originals = {t['id']: t for t in value['plan']['snapshot']['tasks']}
    for task_id, receipt in value['confirmed'].items():
        require(receipt['version'] == 5 and receipt['definition_sha256']
                == digest(definition(originals[task_id])), 'PAUSE_RECEIPT_CHANGED')


def record_error(journal, error, *, area='window'):
    code = error.code if isinstance(error, Refused) else type(error).__name__
    journal.update(lambda v: v['errors'].append({'at': utc(), 'reason': code,
                                               'uncertain': bool(getattr(error, 'uncertain', False)),
                                               'area': area}))


def emit(message, **fields):
    print(json.dumps({'message': message, 'at': utc(), **fields}, ensure_ascii=False), flush=True)


def restoring(journal, client, *, clock=time.monotonic):
    """Resume definite owned pauses only; unknown API writes are never replayed.

    The restore lock excludes a foreground/watchdog race. Journal locks remain
    short, so the independent deadline owner can still inspect the evidence.
    Every API operation fits the original remaining recovery budget.
    """
    with journal.lock('.restore.lock', blocking=False):
        value = journal.read()
        validate_state(value)
        if value['phase'] in TERMINAL:
            return value
        def begin(v):
            v['phase'] = 'restoring'
            if 'recovery_deadline' not in v:
                v['recovery_started_monotonic'] = clock()
                v['recovery_deadline'] = min(v['deadline'],
                    v['recovery_started_monotonic'] + RECOVERY)
        journal.update(begin)
        value = journal.read()
        # First fence only our tagged one-off. Failure must not prevent attempts
        # to restore the original collectors, and never permits a wider kill.
        try:
            terminal = client.stop_source(value, seconds=min(10, value['recovery_deadline'] - clock()))
            journal.update(lambda v: v.update(source_terminal=terminal))
        except Refused as error:
            record_error(journal, error, area='recovery')
        value = journal.read()
        if value['intent']:
            intent = value['intent']
            journal.update(lambda v: (v['unresolved'].append(intent), v.update(intent=None)))
        originals = {t['id']: t for t in value['plan']['snapshot']['tasks']}
        for task_id, receipt in reversed(list(value['confirmed'].items())):
            current = journal.read()
            if task_id in current['restore']:
                continue
            if any(i['id'] == task_id and i['action'] == 'resume' for i in current['unresolved']):
                continue
            if current['recovery_deadline'] - clock() < 20:
                journal.update(lambda v, i=task_id: v['restore'].update({i: {'outcome': 'DEADLINE_EXPIRED'}}))
                continue
            try:
                task = client.task(task_id, seconds=min(10, current['recovery_deadline'] - clock()))
                if (task is None or task['state'] != 'paused' or task['version'] != receipt['version']
                        or definition(task) != definition(originals[task_id])):
                    journal.update(lambda v, i=task_id: v['restore'].update({i: {'outcome': 'CONCURRENT_CHANGE_PRESERVED'}}))
                    continue
                intent = {'action': 'resume', 'id': task_id, 'version': receipt['version'],
                          'token': uuid4().hex, 'at': utc()}
                journal.update(lambda v: v.update(intent=intent))
                acknowledged = client.change(task_id, receipt['version'], 'active',
                                             seconds=min(10, current['recovery_deadline'] - clock()))
                require(acknowledged['definition_sha256'] == receipt['definition_sha256'],
                        'RESUME_DEFINITION_CHANGED')
                journal.update(lambda v, i=task_id, a=acknowledged:
                               (v['restore'].update({i: {'outcome': 'restored', 'receipt': a}}),
                                v.update(intent=None)))
            except Refused as error:
                def failed(v, i=task_id, error=error):
                    if v['intent'] and error.uncertain:
                        v['unresolved'].append(v['intent'])
                    v['intent'] = None
                    v['restore'][i] = {'outcome': error.code, 'uncertain': error.uncertain}
                journal.update(failed)
        # A database observation does not replace an uncertain response receipt.
        # It is still useful to report the actual state and the protected set.
        try:
            remaining = journal.read()['recovery_deadline'] - clock()
            final = client.call('snapshot', recovery=True, seconds=min(10, remaining),
                                accepted=list(journal.read()['accepted']))
            journal.update(lambda v: v.update(recovery_snapshot=final))
            before = {t['id']: t for t in value['plan']['snapshot']['tasks']}
            after = {t['id']: t for t in final['tasks']}
            require(set(before) == set(after) and all(after[i] == t for i, t in before.items()
                    if i not in TARGETS), 'PROTECTED_TASK_CHANGED')
            for task_id, receipt in journal.read()['restore'].items():
                if receipt['outcome'] == 'restored':
                    require(after[task_id]['state'] == 'active'
                            and after[task_id]['version'] == 6
                            and definition(after[task_id]) == definition(before[task_id]),
                            'RESTORATION_OBSERVATION_CHANGED')
            # An unchanged live runner retains the original cron jobs. A runner
            # restarted while tasks were paused may not have registered them;
            # database active rows alone do not prove scheduling was restored.
            client.verify_runner(seconds=min(10, journal.read()['recovery_deadline'] - clock()))
        except Refused as error:
            record_error(journal, error, area='recovery')
        def finish(v):
            known = set(v['restore']) == set(v['confirmed']) and all(
                x['outcome'] == 'restored' for x in v['restore'].values())
            v['deadline_met'] = clock() <= min(v['deadline'], v['recovery_deadline'])
            v['phase'] = 'restored' if known and not v['unresolved'] and not any(
                e['area'] == 'recovery' for e in v['errors']) and v['deadline_met'] else 'recovery_required'
            v['completed_at'] = utc()
        result = journal.update(finish)
        emit('九项采集窗口已退出，确切恢复回执与未确认事项已保存。',
             phase=result['phase'], owned_pauses=len(result['confirmed']),
             restored=sum(r['outcome'] == 'restored' for r in result['restore'].values()),
             unresolved=len(result['unresolved']), deadline_met=result['deadline_met'])
        return result


def forward_guard(journal, *, deadline, clock=time.monotonic, liveness=alive):
    value = journal.read()
    require(not value['stop_requested'] and value['phase'] not in ('restoring', *TERMINAL),
            'RECOVERY_REQUESTED')
    require(clock() < deadline, 'WINDOW_PHASE_EXPIRED')
    require(value['watchdog_ready'] and liveness(value['watchdog']), 'WATCHDOG_UNAVAILABLE')
    return value


def run_window(journal, client, *, clock=time.monotonic, sleep=time.sleep, liveness=alive):
    """Execute the exact reviewed scope once; recovery is the final operation."""
    value = journal.read()
    validate_state(value)
    original = value['plan']['snapshot']
    try:
        require(client.preflight() == value['plan']['runtime'], 'RUNTIME_CHANGED')
        require(clock() < value['drain_deadline'], 'WINDOW_PHASE_EXPIRED')
        initial = client.snapshot(pins=True)
        validate_snapshot(initial)
        require(initial.get('pins_valid') is True, 'NATIVE_PINS_UNVERIFIED')
        journal.update(lambda v: v.update(phase='pausing', accepted={
            r['id']: frozen(r) for r in initial['runs'] if matching(r)}))
        originals = {t['id']: t for t in original['tasks']}
        for task_id in sorted(TARGETS):
            value = forward_guard(journal, deadline=value['drain_deadline'], clock=clock, liveness=liveness)
            fresh = client.snapshot(accepted=value['accepted'])
            validate_snapshot(fresh, original, value['confirmed'])
            journal.update(lambda v: v['accepted'].update({r['id']: frozen(r)
                                 for r in fresh['runs'] if matching(r)}))
            intent = {'action': 'pause', 'id': task_id, 'version': 4,
                      'token': uuid4().hex, 'at': utc()}
            journal.update(lambda v: v.update(intent=intent))
            try:
                receipt = client.change(task_id, 4, 'paused',
                                        seconds=min(10, value['drain_deadline'] - clock()))
            except Refused as error:
                if not error.uncertain:
                    journal.update(lambda v: v.update(intent=None))
                raise
            require(receipt['definition_sha256'] == digest(definition(originals[task_id])),
                    'PAUSE_DEFINITION_CHANGED')
            journal.update(lambda v, i=task_id, receipt=receipt:
                           (v['confirmed'].update({i: receipt}), v.update(intent=None)))
        value = journal.read()
        fresh = client.snapshot(accepted=value['accepted'])
        validate_snapshot(fresh, original, value['confirmed'])
        # Enqueues committed during the sequential pauses are accepted work.
        # Record their frozen identity before enforcing the no-new-work guard.
        journal.update(lambda v: (v['accepted'].update({r['id']: frozen(r)
                               for r in fresh['runs'] if matching(r)}), v.update(phase='draining')))
        while True:
            value = forward_guard(journal, deadline=value['drain_deadline'], clock=clock, liveness=liveness)
            fresh = client.snapshot(accepted=value['accepted'],
                                    seconds=min(10, value['drain_deadline'] - clock()))
            forward_guard(journal, deadline=value['drain_deadline'], clock=clock, liveness=liveness)
            validate_snapshot(fresh, original, value['confirmed'])
            saved = {r['id']: frozen(r) for r in fresh['saved']}
            require(saved == value['accepted'], 'ACCEPTED_RUN_IDENTITY_CHANGED')
            active = [r for r in fresh['runs'] if matching(r)]
            require(all(r['id'] in value['accepted'] for r in active), 'NEW_MATCHING_WORK')
            journal.update(lambda v: v['observations'].append({'at': utc(),
                'counts': {s: sum(r['status'] == s for r in active) for s in ('queued', 'running')}}))
            if not active:
                break
            emit('九项采集后续入队已暂停，正在等待已接受运行自然完成。',
                 remaining_runs=len(active), HTTP=0)
            end = min(value['drain_deadline'], clock() + 300)
            while clock() < end:
                forward_guard(journal, deadline=value['drain_deadline'], clock=clock, liveness=liveness)
                sleep(min(5, end - clock()))
        launch_start = clock()
        journal.update(lambda v: v.update(launch_started_monotonic=launch_start,
            source_start_deadline=min(v['launch_deadline'], launch_start + LAUNCH)))
        value = forward_guard(journal, deadline=journal.read()['source_start_deadline'],
                              clock=clock, liveness=liveness)
        require(client.preflight(seconds=value['source_start_deadline'] - clock())
                == value['plan']['runtime'], 'RUNTIME_CHANGED')
        require(clock() < value['source_start_deadline'], 'WINDOW_PHASE_EXPIRED')
        fresh = client.snapshot(pins=True, ready=True, accepted=value['accepted'],
                                seconds=min(10, value['source_start_deadline'] - clock()))
        validate_snapshot(fresh, original, value['confirmed'])
        require(fresh.get('pins_valid') is True and fresh.get('all_three_admitted') is True
                and not any(matching(r) for r in fresh['runs']),
                'FINAL_SOURCE_ADMISSION_UNVERIFIED')
        # All three preflight scopes must remain clear; the official workers
        # repeat their own admission and pinned publication CAS after here.
        value = forward_guard(journal, deadline=value['source_start_deadline'], clock=clock, liveness=liveness)
        journal.update(lambda v: v.update(phase='source', source_launch_intent=True))
        source_id = client.source(value['episode'], value['plan_sha256'],
                                  control_directory=journal.path.parent,
                                  seconds=min(10, value['source_start_deadline'] - clock()))
        journal.update(lambda v: v.update(source_id=source_id))
        while True:
            value = forward_guard(journal, deadline=value['restore_deadline'], clock=clock, liveness=liveness)
            item = client.source_state(value, seconds=min(10, value['restore_deadline'] - clock()))
            if not item['running']:
                require(item['status'] == 'exited', 'SOURCE_TERMINAL_UNVERIFIED')
                journal.update(lambda v: v.update(source_terminal=item))
                break
            sleep(min(5, value['restore_deadline'] - clock()))
        result = client.receipt(seconds=min(10, value['restore_deadline'] - clock()))
        journal.update(lambda v: v.update(source_receipt=result))
        outcome = verify_source_receipt(original, result, item)
        # A known incomplete batch is a real outcome, not a failed restoration.
        journal.update(lambda v: v.update(source_outcome=outcome, source_verification_complete=True))
    except BaseException as error:
        record_error(journal, error)
    finally:
        try:
            restoring(journal, client, clock=clock)
        except Refused as error:
            if error.code != 'RESTORER_BUSY':
                record_error(journal, error)
    return journal.read()


def watchdog(journal, client, *, clock=time.monotonic, sleep=time.sleep,
             liveness=alive, identity=process_identity, stop_worker=None):
    """Independent Linux-host expiry and operator-loss recovery, one episode.

    This process has its own session and no SSH stdin. Its readiness receipt
    precedes every pause. It never starts a supplier request or renews a window.
    A ten-second grace is only for the owned controller to release its current
    bounded call; it is taken from, not added to, the recovery reserve.
    """
    value = journal.read()
    validate_state(value)
    require(value['phase'] == 'arming' and value['watchdog'] is None,
            'WATCHDOG_ALREADY_ARMED')
    me = identity()
    journal.update(lambda v: v.update(watchdog=me, watchdog_ready=True,
                                     watchdog_armed_at=utc()))
    while True:
        value = journal.read()
        validate_state(value)
        if value['phase'] in TERMINAL:
            return value
        # Do not interrupt an already bounded normal restoration halfway through
        # an API acknowledgement. The last twenty seconds remain a takeover
        # boundary, rather than granting the foreground a renewed deadline.
        recovery_limit = min(value['deadline'], value.get('recovery_deadline', value['deadline']))
        if (value['phase'] == 'restoring' and liveness(value['worker'])
                and clock() < recovery_limit - 20):
            sleep(1)
            continue
        if (value['stop_requested'] or not liveness(value['worker'])
                or clock() >= value['restore_deadline']
                or value['phase'] == 'restoring' and clock() >= recovery_limit - 20):
            reason = value['stop_requested'] or ('OPERATOR_EXITED' if not liveness(value['worker'])
                else 'RECOVERY_CUTOFF' if value['phase'] == 'restoring' else 'RESTORE_CUTOFF')
            journal.update(lambda v: v.update(stop_requested=reason))
            if stop_worker is not None and liveness(value['worker']):
                stop_worker(value['worker'], signal.SIGTERM)
                end = min(recovery_limit, clock() + 10)
                while liveness(value['worker']) and clock() < end:
                    if journal.read()['phase'] in TERMINAL:
                        return journal.read()
                    sleep(min(.5, end - clock()))
                if liveness(value['worker']):
                    stop_worker(value['worker'], signal.SIGKILL)
            try:
                return restoring(journal, client, clock=clock)
            except Refused as error:
                if error.code == 'RESTORER_BUSY' and clock() < recovery_limit:
                    sleep(.5)
                    continue
                record_error(journal, error, area='recovery')
                return journal.read()
        sleep(min(1, max(0, value['restore_deadline'] - clock())))


def stop_owned_worker(expected, signum):
    # A pidfd binds the signal to one kernel process even if it exits between
    # identity verification and signalling. Never address the SSH process group
    # or fall back to a reusable numeric PID on an unsupported Linux kernel.
    try:
        fd = os.pidfd_open(expected['pid'])
    except ProcessLookupError:
        return
    try:
        if alive(expected):
            signal.pidfd_send_signal(fd, signum)
    except ProcessLookupError:
        pass
    finally:
        os.close(fd)


def arm_watchdog(journal):
    """Detach on the deployment host before any API mutation is attempted."""
    require(callable(getattr(os, 'pidfd_open', None))
            and callable(getattr(signal, 'pidfd_send_signal', None)),
            'PIDFD_REQUIRED_BEFORE_PAUSE')
    try:
        fd = os.pidfd_open(os.getpid())
        try:
            signal.pidfd_send_signal(fd, 0)
        finally:
            os.close(fd)
    except OSError:
        raise Refused('PIDFD_REQUIRED_BEFORE_PAUSE') from None
    log = journal.path.with_name(journal.path.name + '.watchdog.jsonl')
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        process = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()),
            'watch', '--state', str(journal.path)], stdin=subprocess.DEVNULL,
            stdout=fd, stderr=fd, close_fds=True, start_new_session=True)
    finally:
        os.close(fd)
    until = min(journal.read()['drain_deadline'], time.monotonic() + 10)
    while time.monotonic() < until:
        value = journal.read()
        if value['watchdog_ready']:
            require(value['watchdog']['pid'] == process.pid and alive(value['watchdog']),
                    'WATCHDOG_IDENTITY_UNVERIFIED')
            return process
        require(process.poll() is None, 'WATCHDOG_ARM_FAILED')
        time.sleep(.1)
    raise Refused('WATCHDOG_ARM_TIMEOUT')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan', 'run', 'watch', 'restore', 'status'))
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--state', type=Path)
    parser.add_argument('--expect-plan-sha256')
    args = parser.parse_args(argv)
    try:
        # This is a host operator. Running under Mac or an unknown boot cannot
        # silently create an SSH-dependent expiry mechanism.
        process_identity()
        if args.action == 'plan':
            require(args.manifest is not None and args.plan is not None,
                    'MANIFEST_AND_NEW_PLAN_REQUIRED')
            manifest = read_private(args.manifest)
            plan = make_plan(manifest, DockerClient(manifest))
            write_private(args.plan, plan, exclusive=True)
            emit('九项采集窗口只读计划已保存，尚未暂停调度或调用来源。',
                 plan_sha256=digest(plan), task_ids=sorted(TARGETS),
                 HTTP=0, tool_head=manifest['tool_head'])
            return 0
        require(args.state is not None, 'PRIVATE_STATE_REQUIRED')
        journal = Journal(args.state)
        if args.action == 'run':
            require(args.plan is not None and args.expect_plan_sha256 is not None,
                    'REVIEWED_PLAN_REQUIRED')
            plan = read_private(args.plan)
            validate_plan(plan)
            require(digest(plan) == args.expect_plan_sha256, 'REVIEWED_PLAN_CHANGED')
            journal.create(new_state(plan, process_identity()))
            client = DockerClient(plan['manifest'])
            def interrupted(*_):
                raise Refused('OPERATOR_INTERRUPTED')
            for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                signal.signal(signum, interrupted)
            try:
                arm_watchdog(journal)
                result = run_window(journal, client)
            except BaseException as error:
                record_error(journal, error)
                result = restoring(journal, client)
        else:
            result = journal.read()
            validate_state(result)
            client = DockerClient(result['plan']['manifest'])
            if args.action == 'watch':
                result = watchdog(journal, client, stop_worker=stop_owned_worker)
            elif args.action == 'restore':
                result = restoring(journal, client)
        emit('九项采集窗口状态已记录，来源结果与恢复结果分别保留。',
             phase=result['phase'], source_outcome=result.get('source_outcome'),
             source_started=result['source_launch_intent'],
             paused=len(result['confirmed']), restored=sum(
                 r['outcome'] == 'restored' for r in result['restore'].values()),
             unresolved=len(result['unresolved']))
        return 0 if result['phase'] == 'restored' and result.get('source_outcome') == 'completed' else 2
    except (Refused, OSError, KeyError, TypeError, ValueError) as error:
        emit('九项采集窗口未完成，请按私有回执核对退出与恢复状态。',
             reason=error.code if isinstance(error, Refused) else type(error).__name__,
             uncertain=bool(getattr(error, 'uncertain', False)))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
