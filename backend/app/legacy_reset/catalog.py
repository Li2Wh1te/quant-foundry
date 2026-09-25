"""Read the live PostgreSQL catalog before any LF-D03 maintenance decision."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from sqlalchemy import text

from .ownership import LEGACY_FUNCTIONS, LEGACY_TABLES, LEGACY_TASK_TYPES, SHARED_TRIGGERS


class ResetRefused(RuntimeError):
    """A maintenance precondition failed; no destructive step may continue."""

    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f'{code}: {detail}')


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)


def sha(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def ident(name: str) -> str:
    """Use identifiers only after a strict catalog/manifest boundary check."""
    if not re.fullmatch(r'[a-z_][a-z0-9_]*', name):
        raise ResetRefused('IDENTIFIER_UNSAFE', '数据库对象标识符不在可复核范围内。')
    return '"' + name + '"'


def _exists(c, schema: str, table: str) -> bool:
    return bool(c.execute(text('SELECT EXISTS (SELECT 1 FROM pg_class r JOIN pg_namespace n ON n.oid=r.relnamespace '
        'WHERE n.nspname=:s AND r.relname=:t AND r.relkind IN (\'r\',\'p\'))'),
        {'s': schema, 't': table}).scalar_one())


def _rows(c, statement: str, **params) -> list[dict]:
    return [dict(row) for row in c.execute(text(statement), params).mappings()]


def snapshot(c, *, expect_database: str, archive_root: Path | None = None) -> dict:
    """Return exact live objects, identities and blockers without changing data.

    The allowlist names only objects reviewed in migrations. A new matching
    prefix object, an untracked dependency or an unreadable path blocks apply.
    """
    if c.dialect.name != 'postgresql':
        raise ResetRefused('DATABASE_UNSUPPORTED', '精确重置仅支持 PostgreSQL。')
    db = c.execute(text('SELECT current_database()')).scalar_one()
    if db != expect_database:
        raise ResetRefused('WRONG_DATABASE', '目标数据库名称与显式参数不一致。')
    try:
        cluster = str(c.execute(text('SELECT system_identifier FROM pg_control_system()')).scalar_one())
    except Exception as exc:
        raise ResetRefused('DATABASE_ID_UNAVAILABLE', '无法读取数据库集群身份；维护角色需要该权限。') from exc
    schema = c.execute(text('SELECT current_schema()')).scalar_one()
    ident(schema)
    database_oid = c.execute(text('SELECT oid FROM pg_database WHERE datname=current_database()')).scalar_one()
    schema_oid = c.execute(text('SELECT oid FROM pg_namespace WHERE nspname=current_schema()')).scalar_one()
    fingerprint = {'cluster': cluster, 'database': db, 'database_oid': database_oid,
                   'schema': schema, 'schema_oid': schema_oid}
    raw_tables = _rows(c, """SELECT r.oid::bigint AS oid, r.relname AS name, r.relkind AS kind,
           CASE WHEN r.relkind IN ('r','p') THEN pg_total_relation_size(r.oid) ELSE 0 END AS estimated_bytes
        FROM pg_class r JOIN pg_namespace n ON n.oid=r.relnamespace
        WHERE n.nspname=:s AND left(r.relname,11)='foundation_' AND r.relkind IN ('r','p','v','m','f')
        ORDER BY r.relname""", s=schema)
    blockers = []
    tables = []
    for row in raw_tables:
        if row['kind'] not in ('r','p') or row['name'] not in LEGACY_TABLES:
            blockers.append({'code': 'UNKNOWN_LEGACY_OBJECT', 'object': row['name']})
        else:
            tables.append(row)
    table_oids = [row['oid'] for row in tables]
    table_names = {row['name'] for row in tables}
    sequences = _rows(c, """SELECT r.oid::bigint AS oid,r.relname AS name,
        owner.refobjid::bigint AS owner_oid
        FROM pg_class r JOIN pg_namespace n ON n.oid=r.relnamespace
        LEFT JOIN pg_depend owner ON owner.objid=r.oid
             AND owner.classid='pg_class'::regclass
             AND owner.refclassid='pg_class'::regclass AND owner.deptype IN ('a','i')
        WHERE n.nspname=:s AND left(r.relname,11)='foundation_' AND r.relkind='S'
        ORDER BY r.relname""", s=schema)
    for sequence in sequences:
        if sequence['owner_oid'] not in table_oids:
            blockers.append({'code':'UNKNOWN_LEGACY_SEQUENCE','object':sequence['name']})
    functions = _rows(c, """SELECT p.oid::bigint AS oid, p.proname AS name,
           pg_get_function_identity_arguments(p.oid) AS arguments,
           pg_get_functiondef(p.oid) AS definition
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname=:s AND left(p.proname,11)='foundation_'
        ORDER BY p.proname, p.oid""", s=schema)
    for fn in functions:
        if fn['name'] not in LEGACY_FUNCTIONS or fn['arguments']:
            blockers.append({'code': 'UNKNOWN_LEGACY_FUNCTION', 'object': fn['name'],
                             'arguments': fn['arguments']})
        fn['definition_hash'] = hashlib.sha256(fn.pop('definition').encode()).hexdigest()
    # All user triggers are listed, including unknown triggers on a legacy table.
    triggers = _rows(c, """SELECT t.oid::bigint AS oid, r.relname AS table_name,
           t.tgname AS name, p.proname AS function_name,
           pg_get_triggerdef(t.oid, true) AS definition,p.prosrc AS function_body
        FROM pg_trigger t JOIN pg_class r ON r.oid=t.tgrelid
        JOIN pg_namespace n ON n.oid=r.relnamespace
        JOIN pg_proc p ON p.oid=t.tgfoid
        WHERE n.nspname=:s AND NOT t.tgisinternal ORDER BY r.relname,t.tgname""", s=schema)
    owned_triggers = []
    for trigger in triggers:
        table = trigger['table_name']
        name = trigger['name']
        fn = trigger['function_name']
        body = trigger.pop('function_body') or ''
        references_old = (table in table_names or fn in LEGACY_FUNCTIONS or name.startswith('foundation_')
                          or (table in SHARED_TRIGGERS and re.search(r'foundation_|\bexecute\b',body,re.I)))
        if not references_old:
            continue
        if table in table_names:
            if fn not in LEGACY_FUNCTIONS:
                blockers.append({'code': 'UNKNOWN_LEGACY_TRIGGER', 'object': f'{table}.{name}'})
        elif name not in SHARED_TRIGGERS.get(table, ()) or fn not in LEGACY_FUNCTIONS:
            blockers.append({'code': 'UNKNOWN_EXTERNAL_TRIGGER', 'object': f'{table}.{name}'})
        trigger['definition_hash'] = hashlib.sha256(trigger.pop('definition').encode()).hexdigest()
        owned_triggers.append(trigger)
    # Dependencies attached to an external relation must be explicit. RESTRICT
    # remains the final PostgreSQL guard if a dependency escapes this report.
    if table_oids:
        external_fks = _rows(c, """SELECT con.conname AS name, rel.relname AS table_name,
            target.relname AS target_name FROM pg_constraint con
            JOIN pg_class rel ON rel.oid=con.conrelid
            JOIN pg_class target ON target.oid=con.confrelid
            WHERE con.contype='f' AND con.confrelid=ANY(:oids)
              AND NOT con.conrelid=ANY(:oids) ORDER BY rel.relname,con.conname""", oids=table_oids)
        for row in external_fks:
            blockers.append({'code': 'EXTERNAL_FK', **row})
        external_views = _rows(c, """SELECT DISTINCT v.relname AS name, target.relname AS target_name
            FROM pg_depend d JOIN pg_rewrite rule ON rule.oid=d.objid
            JOIN pg_class v ON v.oid=rule.ev_class
            JOIN pg_class target ON target.oid=d.refobjid
            WHERE d.refobjid=ANY(:oids) AND NOT rule.ev_class=ANY(:oids)
              AND v.relkind IN ('v','m') ORDER BY v.relname,target.relname""", oids=table_oids)
        for row in external_views:
            blockers.append({'code': 'EXTERNAL_VIEW', **row})
        inherited = _rows(c, """SELECT child.relname AS child, parent.relname AS parent
            FROM pg_inherits i JOIN pg_class child ON child.oid=i.inhrelid
            JOIN pg_class parent ON parent.oid=i.inhparent
            WHERE (i.inhrelid=ANY(:oids)) <> (i.inhparent=ANY(:oids))""", oids=table_oids)
        for row in inherited:
            blockers.append({'code': 'EXTERNAL_INHERITANCE', **row})
    external_functions = _rows(c, """SELECT p.proname AS name, pg_get_function_identity_arguments(p.oid) AS arguments,
        position('foundation_' in lower(p.prosrc))>0 AS references_old,
        p.prosrc ~* '(^|[^a-z_])execute([^a-z_]|$)' AS dynamic_sql
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname=:s AND left(p.proname,11)<>'foundation_'
          AND (position('foundation_' in lower(p.prosrc))>0
               OR p.prosrc ~* '(^|[^a-z_])execute([^a-z_]|$)') ORDER BY p.proname""", s=schema)
    for row in external_functions:
        blockers.append({'code': 'EXTERNAL_FUNCTION_BODY' if row.pop('references_old') else
                         'DYNAMIC_FUNCTION_UNREVIEWED', **row})
    tasks = _rows(c, """SELECT id::text AS id,task_type,state,version FROM scheduled_tasks
        WHERE task_type=ANY(:types) ORDER BY id""", types=list(LEGACY_TASK_TYPES)) if _exists(c,schema,'scheduled_tasks') else []
    runs = _rows(c, """SELECT id::text AS id,task_id::text AS task_id,task_type,status
        FROM task_runs WHERE task_type=ANY(:types) AND status IN ('queued','running')
        ORDER BY id""", types=list(LEGACY_TASK_TYPES)) if _exists(c,schema,'task_runs') else []
    active_work = bool(c.execute(text('SELECT EXISTS (SELECT 1 FROM '
        f'{ident(schema)}.foundation_work WHERE status=\'running\' AND '
        '(lease_until IS NULL OR lease_until>clock_timestamp()))')).scalar_one()) if 'foundation_work' in table_names else False
    writer_sessions = _rows(c, """SELECT pid,state FROM pg_stat_activity
        WHERE datname=current_database() AND pid<>pg_backend_pid()
          AND state IN ('active','idle in transaction')
          AND position('foundation_' in lower(coalesce(query,'')))>0
        ORDER BY pid""")
    originals = {}
    for name in ('foundation_baselines','foundation_table_changes'):
        originals[name] = bool(c.execute(text(f'SELECT EXISTS(SELECT 1 FROM {ident(schema)}.{ident(name)})')).scalar_one()) if name in table_names else False
    archives = _rows(c, f'SELECT archive_key AS key,archive_hash AS hash,byte_count AS bytes '
        f'FROM {ident(schema)}.foundation_runtime_archives ORDER BY archive_key') if 'foundation_runtime_archives' in table_names else []
    path_records = []
    if archives:
        if archive_root is None:
            blockers.append({'code':'ARCHIVE_ROOT_REQUIRED'})
        else:
            root = archive_root.absolute()
            try:
                safe_root = (root.name=='foundation-runtime-archives' and root.is_dir()
                             and root.resolve(strict=True)==root)
            except OSError:
                safe_root = False
            if not safe_root:
                blockers.append({'code':'ARCHIVE_ROOT_UNSAFE'})
            for item in archives:
                if not re.fullmatch(r'[0-9a-f]{64}\.tar', item['key']):
                    blockers.append({'code':'ARCHIVE_NAME_UNSAFE', 'key':item['key']})
                    continue
                target = root / item['key']
                if not target.exists() and not target.is_symlink():
                    blockers.append({'code':'ARCHIVE_MISSING', 'key':item['key']})
                elif target.is_symlink() or not target.is_file():
                    blockers.append({'code':'ARCHIVE_PATH_UNSAFE', 'key':item['key']})
                path_records.append({**item,'root':str(root)})
    return {'format':'qf-legacy-reset-plan-v1','database':fingerprint,
            'tables':tables,'sequences':sequences,'functions':functions,'triggers':owned_triggers,
            'tasks':tasks,'active_runs':runs,'active_work':active_work,'writer_sessions':writer_sessions,
            'originals':originals,'paths':path_records,'blockers':blockers}
