"""Explicit, restartable LF-D03 maintenance operations.

No legacy runtime modules are imported. Every deletion is named from the
reviewed ownership map and rechecked against the live PostgreSQL catalog.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Iterator

from sqlalchemy import text

from .catalog import ResetRefused, canonical, ident, sha, snapshot
from .ownership import LEGACY_TASK_TYPES, ORIGINAL_CONTAINERS

GROUPS = ('hooks', 'derived', 'originals', 'functions', 'files')


@contextmanager
def transaction(engine, *, read_only: bool = False, isolation_level: str = 'REPEATABLE READ') -> Iterator:
    with engine.connect().execution_options(isolation_level=isolation_level) as c:
        with c.begin():
            c.execute(text("SET LOCAL lock_timeout = '5s'"))
            c.execute(text("SET LOCAL statement_timeout = '60s'"))
            if read_only:
                c.execute(text('SET TRANSACTION READ ONLY'))
            yield c


def _maintenance(c, schema: str, *, lock: bool = False) -> dict | None:
    query = f'SELECT phase,plan_hash,completed_json,files_started FROM {ident(schema)}.data_store_legacy_maintenance WHERE singleton=1'
    if lock:
        query += ' FOR UPDATE'
    row = c.execute(text(query)).mappings().first()
    return dict(row) if row else None


def enter(engine, *, expect_database: str, pause_task_ids: tuple[str, ...] = ()) -> dict:
    """Pause exact old tasks, and only explicitly named shared tasks.

    The task snapshot and every state change commit together. This command
    never alters a provider's enabled setting or deletes scheduler history.
    """
    with transaction(engine) as c:
        live = snapshot(c, expect_database=expect_database)
        schema = live['database']['schema']
        if any(b['code'].startswith('UNKNOWN_') for b in live['blockers']):
            raise ResetRefused('CATALOG_UNREVIEWED', '存在未审查旧对象，不能进入可清理状态。')
        if live['active_work'] or live['writer_sessions'] or any(r['status']=='running' for r in live['active_runs']):
            raise ResetRefused('LEGACY_WRITER_ACTIVE', '旧正式化写入仍在运行。')
        current = _maintenance(c,schema,lock=True)
        if current:
            if pause_task_ids:
                raise ResetRefused('MAINTENANCE_ALREADY_ENTERED','维护态已固定任务清单；不能静默加入新的暂停任务。')
            if current['phase'] not in ('rebuilding','resetting','reset_done'):
                raise ResetRefused('MAINTENANCE_STATE_INVALID','维护状态不允许重新暂停。')
            return {'phase':current['phase'],'paused':c.execute(text(
                f'SELECT count(*) FROM {ident(schema)}.data_store_legacy_task_state')).scalar_one()}
        c.execute(text(f"INSERT INTO {ident(schema)}.data_store_legacy_maintenance(singleton,phase) VALUES (1,'rebuilding')"))
        # A queued legacy run is recorded as skipped, rather than left runnable
        # after its task is paused. Historical runs stay in the shared table.
        c.execute(text("""UPDATE task_runs SET status='skipped',finished_at=clock_timestamp(),
            error_type='LegacyMaintenance',error_message='旧底座维护态已停止排队运行'
            WHERE task_type=ANY(:types) AND status='queued'"""), {'types':list(LEGACY_TASK_TYPES)})
        selected = c.execute(text("""SELECT id,task_type,state,version FROM scheduled_tasks
            WHERE task_type=ANY(:types) OR id::text=ANY(:ids) ORDER BY id FOR UPDATE"""),
            {'types':list(LEGACY_TASK_TYPES),'ids':list(pause_task_ids)}).mappings().all()
        found = {str(row['id']) for row in selected}
        if set(pause_task_ids)-found:
            raise ResetRefused('TASK_ID_UNKNOWN','显式共享任务 ID 不存在。')
        for row in selected:
            task_id = str(row['id'])
            new_version = row['version'] + (row['state']!='paused')
            c.execute(text(f"""INSERT INTO {ident(schema)}.data_store_legacy_task_state
                (task_id,task_type,previous_state,paused_version)
                VALUES (:id,:type,:state,:version)"""),
                {'id':task_id,'type':row['task_type'],'state':row['state'],'version':new_version})
            if row['state']!='paused':
                c.execute(text("""UPDATE scheduled_tasks SET state='paused',version=version+1,
                    updated_at=clock_timestamp() WHERE id=:id AND version=:version"""),
                    {'id':task_id,'version':row['version']})
        return {'phase':'rebuilding','paused':len(selected)}


def restore_tasks(engine, *, expect_database: str, include_legacy: bool = False) -> dict:
    """Restore only the captured state, rejecting concurrent task edits.

    Retired foundation types can be restored only before any reset group starts.
    Shared collector tasks may be restored after reset while the store rebuilds.
    """
    with transaction(engine) as c:
        live = snapshot(c,expect_database=expect_database)
        schema = live['database']['schema']
        state = _maintenance(c,schema,lock=True)
        if not state:
            raise ResetRefused('MAINTENANCE_NOT_ENTERED','没有已记录的暂停状态。')
        if include_legacy and (state['phase']!='rebuilding' or state['completed_json']):
            raise ResetRefused('LEGACY_RESTORE_UNSAFE','旧对象已清退，不能恢复旧任务。')
        rows = c.execute(text(f'SELECT task_id::text AS id,task_type,previous_state,paused_version '
            f'FROM {ident(schema)}.data_store_legacy_task_state ORDER BY task_id')).mappings().all()
        restored = 0
        for row in rows:
            if row['task_type'] in LEGACY_TASK_TYPES and not include_legacy:
                continue
            changed = c.execute(text("""UPDATE scheduled_tasks SET state=:state,version=version+1,
                updated_at=clock_timestamp() WHERE id=:id AND state='paused' AND version=:version"""),
                {'state':row['previous_state'],'id':row['id'],'version':row['paused_version']}).rowcount
            if changed != 1:
                raise ResetRefused('TASK_STATE_CHANGED','任务暂停后被其他操作修改，未执行自动恢复。')
            c.execute(text(f'DELETE FROM {ident(schema)}.data_store_legacy_task_state WHERE task_id=:id'),
                      {'id':row['id']})
            restored += 1
        if include_legacy:
            c.execute(text(f'DELETE FROM {ident(schema)}.data_store_legacy_maintenance WHERE singleton=1'))
        return {'restored':restored,'phase':None if include_legacy else state['phase']}


def create_plan(engine, *, expect_database: str, archive_root: Path | None = None) -> dict:
    with transaction(engine,read_only=True) as c:
        live = snapshot(c,expect_database=expect_database,archive_root=archive_root)
        schema = live['database']['schema']
        state = _maintenance(c,schema)
        if state is None or state['phase']!='rebuilding':
            live['blockers'].append({'code':'MAINTENANCE_NOT_ENTERED'})
        if live['active_runs'] or live['active_work'] or live['writer_sessions']:
            live['blockers'].append({'code':'LEGACY_WRITER_ACTIVE'})
        if any(t['state']!='paused' for t in live['tasks']):
            live['blockers'].append({'code':'LEGACY_TASK_NOT_PAUSED'})
        # A plan records names, OIDs, definitions and IDs. It never stores
        # source payloads, credentials, or complete connection strings.
        live['digest'] = sha(live)
        return live


def write_new_json(path: Path, payload: dict) -> None:
    require_safe_output(path)
    encoded = canonical(payload)+'\n'
    if len(encoded.encode())>4*1024*1024:
        raise ResetRefused('PLAN_TOO_LARGE','维护清单超过有界文件预算。')
    fd = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def require_safe_output(path: Path) -> None:
    """Reject relative or symlinked output parents before creating evidence."""
    try:
        if not path.is_absolute() or not path.parent.is_dir() or path.parent.resolve(strict=True)!=path.parent:
            raise ResetRefused('OUTPUT_PATH_UNSAFE','维护输出必须位于现有的无符号链接绝对目录。')
    except OSError as exc:
        raise ResetRefused('OUTPUT_PATH_UNSAFE','维护输出目录不可用。') from exc


def read_plan(path: Path) -> dict:
    fd = os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'r',encoding='utf-8') as stream:
        raw = stream.read(4*1024*1024+1)
    if len(raw.encode())>4*1024*1024:
        raise ResetRefused('PLAN_TOO_LARGE','维护清单超过有界文件预算。')
    data = json.loads(raw)
    digest = data.pop('digest',None)
    if digest != sha(data) or data.get('format')!='qf-legacy-reset-plan-v1':
        raise ResetRefused('PLAN_TAMPERED','计划文件摘要或格式无效。')
    data['digest'] = digest
    return data


def _same_objects(planned: dict, live: dict, completed: list[str]) -> None:
    if planned['database'] != live['database']:
        raise ResetRefused('WRONG_DATABASE','实际数据库身份与计划不一致。')
    if live['blockers']:
        raise ResetRefused('CATALOG_CHANGED','当前系统目录存在未审查对象或外部依赖。')
    if live['active_runs'] or live['active_work'] or live['writer_sessions'] or any(t['state']!='paused' for t in live['tasks']):
        raise ResetRefused('LEGACY_WRITER_ACTIVE','旧任务或写入仍在运行。')
    def expected(group: str, key):
        return {key(v):v for v in planned[group]}
    def observed(group: str, key):
        return {key(v):v for v in live[group]}
    groups = (
        ('tables',lambda v:v['name']),
        ('sequences',lambda v:v['name']),
        ('functions',lambda v:v['name']),
        ('triggers',lambda v:(v['table_name'],v['name'])),
    )
    for name,key in groups:
        old = expected(name,key)
        new = observed(name,key)
        if set(new)-set(old):
            raise ResetRefused('CATALOG_CHANGED','计划外旧对象出现。')
        for identity,value in old.items():
            if identity in new and new[identity]!=value:
                raise ResetRefused('CATALOG_CHANGED','旧对象定义或身份已变化。')
            if identity not in new:
                if name=='tables':
                    belongs = 'originals' if value['name'] in ORIGINAL_CONTAINERS else 'derived'
                elif name=='sequences':
                    owner = next((x['name'] for x in planned['tables'] if x['oid']==value['owner_oid']),None)
                    belongs = 'originals' if owner in ORIGINAL_CONTAINERS else 'derived'
                elif name=='functions':
                    belongs = 'functions'
                else:
                    belongs = ('originals' if value['table_name'] in ORIGINAL_CONTAINERS else 'derived') if value['table_name'] in {x['name'] for x in planned['tables']} else 'hooks'
                if belongs not in completed:
                    raise ResetRefused('CATALOG_CHANGED','计划对象在未完成组中消失。')
    if planned['tasks']!=live['tasks']:
        raise ResetRefused('TASK_SET_CHANGED','旧任务 ID 或类型变化。')
    if live['originals']!=planned['originals'] and 'originals' not in completed:
        raise ResetRefused('ORIGINALS_CHANGED','原件容器状态已变化。')
    if live['paths']!=planned['paths'] and 'derived' not in completed:
        raise ResetRefused('ARCHIVES_CHANGED','旧归档路径清单变化。')


def _mark(c, schema: str, completed: list[str], phase: str = 'resetting') -> None:
    c.execute(text(f"""UPDATE {ident(schema)}.data_store_legacy_maintenance
        SET phase=:phase,completed_json=CAST(:completed AS jsonb),updated_at=clock_timestamp()
        WHERE singleton=1"""), {'phase':phase,'completed':canonical(completed)})


def _require_rescue(plan: dict, rescue_manifest: dict | None) -> None:
    if not any(plan['originals'].values()):
        return
    if rescue_manifest is None or rescue_manifest.get('format')!='qf-local-rescue-manifest-v1' \
       or rescue_manifest.get('plan_hash')!=plan['digest'] or not rescue_manifest.get('verified'):
        raise ResetRefused('ORIGINALS_NOT_PRESERVED','原件容器非空，缺少与本计划绑定的已校验救回文件。')


def _recheck_originals(c, schema: str, plan: dict, rescue_manifest: dict | None) -> None:
    """Freeze old and native inputs, then recheck rescue against their latest rows.

    READ COMMITTED is required here: a transaction that waited for a table lock
    must see writes committed before the lock was acquired. The locks stay held
    until the corresponding DROP transaction commits.
    """
    from app.data_store.local_sources import TABLES
    from .rescue import _record_summary

    legacy = [name for name in ('foundation_baselines','foundation_baseline_blocks',
                                'foundation_table_changes') if name in {v['name'] for v in plan['tables']}]
    if any(plan['originals'].values()):
        candidate = sorted({spec[0] for spec in TABLES.values()})
        native = [row['relname'] for row in c.execute(text("""SELECT r.relname
            FROM pg_class r JOIN pg_namespace n ON n.oid=r.relnamespace
            WHERE n.nspname=:schema AND r.relname=ANY(:names) AND r.relkind IN ('r','p')
            ORDER BY r.relname"""), {'schema':schema,'names':candidate}).mappings()]
        if native:
            c.execute(text('LOCK TABLE '+','.join(f'{ident(schema)}.{ident(name)}' for name in native)+
                           ' IN SHARE MODE'))
    if legacy:
        c.execute(text('LOCK TABLE '+','.join(f'{ident(schema)}.{ident(name)}' for name in legacy)+
                       ' IN ACCESS EXCLUSIVE MODE'))
    actual = {name:bool(c.execute(text(f'SELECT EXISTS(SELECT 1 FROM {ident(schema)}.{ident(name)})')).scalar_one())
              for name in plan['originals'] if name in legacy}
    if actual != {name:value for name,value in plan['originals'].items() if name in legacy}:
        raise ResetRefused('ORIGINALS_CHANGED','锁定后发现计划外原件。')
    if not any(plan['originals'].values()):
        return
    current = _record_summary(c,plan)
    if any(current[key]!=rescue_manifest[key] for key in
           ('record_count','uncompressed_bytes','records_sha256','kinds')):
        raise ResetRefused('ORIGINALS_CHANGED','删除前原件或原生副本与救回校验结果不同。')


def _preserve_current_restrictions(c, schema: str, table_names: set[str]) -> int:
    """Keep only the latest unresolved restriction per old issue identity.

    An unlocated restriction is an explicit global read guard for D04. It may
    be narrowed to a native object only after its source identity is proven.
    """
    statements = []
    if 'foundation_issue_revisions' in table_names:
        statements.append(('bar', """SELECT DISTINCT ON (issue_id) issue_id::text AS issue_id,
            scope_key,instrument_id::text AS instrument_id,start,"end",state,
            fields_json,reason,revision FROM foundation_issue_revisions
            ORDER BY issue_id,revision DESC"""))
    if 'foundation_record_issues' in table_names:
        statements.append(('record', """SELECT DISTINCT ON (issue_id) issue_id::text AS issue_id,
            scope_key,target_key,state,fields_json,reason,revision
            FROM foundation_record_issues ORDER BY issue_id,revision DESC"""))
    work_context = 'foundation_work' in table_names and all(c.execute(text("""SELECT EXISTS(
        SELECT 1 FROM information_schema.columns WHERE table_schema=:schema
          AND table_name='foundation_work' AND column_name=:column)"""),
        {'schema':schema,'column':column}).scalar_one() for column in ('scope_key','parameters_json'))
    context_cache = {}
    total = 0
    for family,statement in statements:
        result = c.execute(text(statement).execution_options(stream_results=True,yield_per=100))
        for row in result.mappings():
            if row['state'] not in ('confirmed','suspected'):
                continue
            body = dict(row)
            body['family'] = family
            scope = row['scope_key']
            if work_context and scope not in context_cache:
                contexts = c.execute(text(f"""SELECT DISTINCT parameters_json::jsonb->>'dataset' AS dataset
                    FROM {ident(schema)}.foundation_work WHERE scope_key=:scope
                    AND parameters_json IS NOT NULL LIMIT 2"""), {'scope':scope}).scalars().all()
                context_cache[scope] = contexts[0] if len(contexts)==1 and contexts[0] else None
            dataset = context_cache.get(scope)
            body['scope_dataset'] = dataset
            c.execute(text(f"""INSERT INTO {ident(schema)}.data_store_legacy_restrictions
                (origin_key,dataset,scope_key,target_key,fields_json,reason,payload_json,located)
                VALUES (:origin,:dataset,:scope,:target,:fields,:reason,:payload,false)
                ON CONFLICT(origin_key) DO NOTHING"""),
                {'origin':family+':'+row['issue_id'],'scope':scope,'dataset':dataset,
                 'target':row.get('target_key') if family=='record' else row.get('instrument_id'),
                 'fields':row['fields_json'],'reason':row['reason'],'payload':canonical(body)})
            total += 1
        result.close()
    if 'foundation_baselines' in table_names:
        pending = c.execute(text(f"""SELECT EXISTS(SELECT 1 FROM {ident(schema)}.foundation_baselines
            WHERE source='tonghuashun' AND dataset='unresolved_request_units')""")).scalar_one()
        if pending:
            c.execute(text(f"""INSERT INTO {ident(schema)}.data_store_legacy_restrictions
                (origin_key,dataset,scope_key,target_key,fields_json,reason,payload_json,located)
                VALUES ('unresolved-request-units',NULL,NULL,NULL,'[]',
                        '旧请求原文缺少可证明的业务身份',:payload,false)
                ON CONFLICT(origin_key) DO NOTHING"""), {'payload':canonical({'family':'unresolved_request'})})
            total += 1
    return total


def apply_database_group(engine, *, plan: dict, group: str,
                         rescue_manifest: dict | None = None, archive_root: Path | None = None) -> dict:
    if group not in GROUPS[:-1]:
        raise ValueError('Only transactional database groups belong here')
    if plan['blockers']:
        raise ResetRefused('PLAN_BLOCKED','计划包含阻断项，不能执行删除。')
    with transaction(engine,isolation_level='READ COMMITTED') as c:
        live = snapshot(c,expect_database=plan['database']['database'],archive_root=archive_root)
        schema = live['database']['schema']
        state = _maintenance(c,schema,lock=True)
        if state is None or state['phase'] not in ('rebuilding','resetting','reset_done'):
            raise ResetRefused('MAINTENANCE_NOT_ENTERED','未处于明确维护态。')
        completed = list(state['completed_json'])
        if state['plan_hash'] not in (None,plan['digest']):
            raise ResetRefused('PLAN_CHANGED','维护态已绑定另一个计划。')
        _same_objects(plan,live,completed)
        if group in completed:
            return {'group':group,'status':'already_complete'}
        if group != GROUPS[len(completed)]:
            raise ResetRefused('GROUP_ORDER','重置步骤必须按已记录顺序执行。')
        if group in ('derived','originals'):
            _require_rescue(plan,rescue_manifest)
            _recheck_originals(c,schema,plan,rescue_manifest)
        if group=='derived' and 'foundation_runtime_archives' in {row['name'] for row in live['tables']}:
            c.execute(text(f'LOCK TABLE {ident(schema)}.foundation_runtime_archives IN ACCESS EXCLUSIVE MODE'))
            _same_objects(plan,snapshot(c,expect_database=plan['database']['database'],
                                        archive_root=archive_root),completed)
        c.execute(text(f"UPDATE {ident(schema)}.data_store_legacy_maintenance "
            "SET plan_hash=:hash,phase='resetting' WHERE singleton=1"), {'hash':plan['digest']})
        if group=='hooks':
            for trigger in plan['triggers']:
                if trigger['table_name'] not in {v['name'] for v in plan['tables']}:
                    c.execute(text(f'DROP TRIGGER {ident(trigger["name"])} ON '
                        f'{ident(schema)}.{ident(trigger["table_name"])} RESTRICT'))
        elif group in ('derived','originals'):
            if group=='derived':
                existing={row['name'] for row in live['tables']}
                restrictions=[name for name in ('foundation_issue_revisions','foundation_record_issues',
                                                 'foundation_work') if name in existing]
                if restrictions:
                    c.execute(text('LOCK TABLE '+','.join(f'{ident(schema)}.{ident(name)}'
                                  for name in restrictions)+' IN ACCESS EXCLUSIVE MODE'))
                _preserve_current_restrictions(c,schema,existing)
            else:
                # The baseline lock acquired by _recheck_originals also closes
                # the gap for unresolved request units created after derived.
                _preserve_current_restrictions(c,schema,{row['name'] for row in live['tables']})
            names = [row['name'] for row in plan['tables'] if
                     (row['name'] in ORIGINAL_CONTAINERS)==(group=='originals')]
            if names:
                sql = ','.join(f'{ident(schema)}.{ident(name)}' for name in names)
                c.execute(text(f'DROP TABLE {sql} RESTRICT'))
        elif group=='functions':
            for fn in plan['functions']:
                c.execute(text(f'DROP FUNCTION {ident(schema)}.{ident(fn["name"])}() RESTRICT'))
        completed.append(group)
        _mark(c,schema,completed)
        return {'group':group,'status':'complete','completed':completed}


def _verify_archive(root: Path, item: dict) -> None:
    _check_archive(root,item,missing_ok=False,delete=False)


def _unlink_archive(root: Path, item: dict, *, missing_ok: bool) -> None:
    _check_archive(root,item,missing_ok=missing_ok,delete=True)


def _check_archive(root: Path, item: dict, *, missing_ok: bool, delete: bool) -> None:
    """Use directory-relative no-follow descriptors for the final path step."""
    import re
    if (root.name!='foundation-runtime-archives' or not root.is_dir()
            or root.resolve(strict=True)!=root.absolute()
            or str(root.absolute())!=item['root']):
        raise ResetRefused('ARCHIVE_ROOT_UNSAFE','归档目录与计划不一致或包含符号链接。')
    name = item['key']
    if not re.fullmatch(r'[0-9a-f]{64}\.tar',name):
        raise ResetRefused('ARCHIVE_NAME_UNSAFE','归档文件名越界。')
    directory = os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        try:
            fd = os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=directory)
        except FileNotFoundError:
            if missing_ok:
                return
            raise ResetRefused('ARCHIVE_MISSING','归档文件在开始删除前缺失。') from None
        with os.fdopen(fd,'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size!=item['bytes']:
                raise ResetRefused('ARCHIVE_CHANGED','归档不是计划中的普通文件或大小变化。')
            found = hashlib.file_digest(stream,'sha256').hexdigest()
            after = os.stat(name,dir_fd=directory,follow_symlinks=False)
            if (found!=item['hash'] or after.st_ino!=before.st_ino or
                    after.st_dev!=before.st_dev or after.st_mtime_ns!=before.st_mtime_ns):
                raise ResetRefused('ARCHIVE_CHANGED','归档摘要或文件身份变化。')
        if delete:
            os.unlink(name,dir_fd=directory)
    finally:
        os.close(directory)


def apply_files(engine, *, plan: dict, archive_root: Path | None) -> dict:
    if archive_root is None and plan['paths']:
        raise ResetRefused('ARCHIVE_ROOT_REQUIRED','计划包含专属归档文件。')
    with transaction(engine) as c:
        live = snapshot(c,expect_database=plan['database']['database'],archive_root=archive_root)
        schema = live['database']['schema']
        state = _maintenance(c,schema,lock=True)
        if not state or state['plan_hash']!=plan['digest']:
            raise ResetRefused('PLAN_CHANGED','维护态未绑定该计划。')
        completed = list(state['completed_json'])
        _same_objects(plan,live,completed)
        if 'files' in completed:
            return {'group':'files','status':'already_complete'}
        if completed != list(GROUPS[:-1]):
            raise ResetRefused('GROUP_ORDER','数据库对象尚未清退完成。')
        if not state['files_started']:
            for item in plan['paths']:
                _verify_archive(archive_root,item)
        c.execute(text(f'UPDATE {ident(schema)}.data_store_legacy_maintenance '
                       'SET files_started=true WHERE singleton=1'))
    # Each unlink is idempotent after files_started is durable. A missing file
    # on retry is accepted only after this marker exists, never on first entry.
    for item in plan['paths']:
        _unlink_archive(archive_root,item,missing_ok=True)
    with transaction(engine) as c:
        live = snapshot(c,expect_database=plan['database']['database'],archive_root=archive_root)
        schema = live['database']['schema']
        state = _maintenance(c,schema,lock=True)
        if not state or not state['files_started'] or state['plan_hash']!=plan['digest']:
            raise ResetRefused('PLAN_CHANGED','文件清理进度与计划不一致。')
        completed = list(state['completed_json'])
        if 'files' not in completed:
            completed.append('files')
            _mark(c,schema,completed,phase='reset_done')
        return {'group':'files','status':'complete','phase':'reset_done'}


def status(engine, *, expect_database: str) -> dict:
    with transaction(engine,read_only=True) as c:
        live = snapshot(c,expect_database=expect_database)
        state = _maintenance(c,live['database']['schema'])
        # Absence of a receipt is never proof that D02's current store exists.
        phase = state['phase'] if state else 'rebuilding'
        return {'phase':phase,'code':'DATA_STORE_REBUILDING' if phase!='ready' else 'READY',
                'legacy_tables':len(live['tables']),
                'completed_groups':list(state['completed_json']) if state else []}


def finish_rebuild(engine, *, expect_database: str) -> dict:
    """Open the store only after all static business entries are qualified."""
    from app.data_store.adapters.registry import ENTRIES
    with transaction(engine) as c:
        live = snapshot(c,expect_database=expect_database)
        schema = live['database']['schema']
        state = _maintenance(c,schema,lock=True)
        if not state or state['phase']!='reset_done' or live['tables'] or live['functions']:
            raise ResetRefused('REBUILD_NOT_READY','精确清退尚未完成。')
        unresolved = c.execute(text(f'SELECT count(*) FROM {ident(schema)}.data_store_legacy_restrictions '
            'WHERE located=false')).scalar_one()
        if unresolved:
            raise ResetRefused('RESTRICTIONS_UNLOCATED','旧当前限制尚未定位到原生范围。')
        business = {entry.id for entry in ENTRIES if entry.business}
        results = c.execute(text('SELECT entry_id,summary_json FROM data_store_entry_status '
                                 'WHERE entry_id=ANY(:ids)'),{'ids':list(business)}).mappings().all()
        statuses = {row['entry_id']:json.loads(row['summary_json']) for row in results}
        if set(statuses)!=business or any(not item.get('complete') or not item.get('qualified')
                                           for item in statuses.values()):
            raise ResetRefused('REBUILD_NOT_READY','新 reader 尚未完成全部业务入口的合格重建。')
        c.execute(text(f"UPDATE {ident(schema)}.data_store_legacy_maintenance "
            "SET phase='ready',updated_at=clock_timestamp() WHERE singleton=1"))
        return {'phase':'ready','qualified_business_entries':len(business)}
