#!/usr/bin/env python3
"""Plan exact retired temporary objects; default is read-only dry-run.

Run with backend's locked environment. No production execution is authorized by
this tool. Unknown objects, business data, references and activity fail closed.
The plan is an automatic identity inventory, never a deletion authorization.
No container/image/volume deletion, wildcard, prune, truncate or CASCADE exists.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

from sqlalchemy import create_engine, inspect, text

ROOT_MARKER = '.qf-retired-temp-root.json'
OBJECT_MARKER = '.qf-retired-temp.json'
ROOT_KIND = 'qf.retired-temp-root.v1'
PATH_KIND = 'qf.stopped-legacy-worker-temp.v1'
TABLE_KIND = 'qf.empty-legacy-temp.v1'
PLAN_KIND = 'qf.backtest-cleanup-plan.v1'
MAX_FILES = 4096
MAX_BYTES = 1024**3
MAX_ROWS = 10000
MAX_REFERENCE_BYTES = 32 * 1024**2
IDENTIFIER = re.compile(r'[a-z_][a-z0-9_]{0,62}\Z')


class Refused(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code):
    raise Refused(code)


def _info(value):
    return [value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_nlink,
            value.st_size, value.st_mtime_ns, value.st_ctime_ns]


def _parts(path):
    if not path.is_absolute() or '..' in path.parts or str(path) != os.path.normpath(str(path)):
        _fail('ROOT_NOT_EXACT_ABSOLUTE')
    return path.parts[1:]


@contextmanager
def _directory(path):
    """Open every component without following links, pinning the final inode."""
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in _parts(path):
            new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = new
        yield fd
    finally:
        os.close(fd)


def _json_at(fd, name):
    opened = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    try:
        info = os.fstat(opened)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 16384:
            _fail('UNKNOWN_MARKER')
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            _fail('UNCONTROLLED_MARKER')
        value = json.loads(os.read(opened, 16385))
        if not isinstance(value, dict):
            _fail('UNKNOWN_MARKER')
        return value
    finally:
        os.close(opened)


def _relative(value):
    p = Path(value)
    if p.is_absolute() or not p.parts or any(x in ('.', '..') for x in p.parts) or p.as_posix() != value:
        _fail('PATH_NOT_EXACT_RELATIVE')
    return p


def _overlap(a, b):
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def _protected(root):
    from app.core.config import get_settings
    settings = get_settings()
    repo = Path(__file__).resolve().parents[1]
    paths = [repo, settings.data_store_root, settings.s3_result_root, settings.log_dir]
    paths += [p for p in (settings.s3_work_root, settings.s3_daemon_work_root) if p is not None]
    if root == Path('/') or len(root.parts) < 3 or any(_overlap(root, p.resolve()) for p in paths):
        _fail('SHARED_OR_RUNTIME_ROOT')


def _database(c, expected):
    row = c.execute(text('SELECT oid, datname FROM pg_database WHERE datname=current_database()')).one()
    if row.datname != expected:
        _fail('WRONG_DATABASE')
    # A name/port alone is insufficient after a drop/recreate or cluster swap.
    system = c.execute(text('SELECT system_identifier::text FROM pg_control_system()')).scalar_one()
    return {'name': row.datname, 'oid': row.oid, 'system_identifier': system}


def _known_tables():
    import app.models  # noqa: F401
    from app.db.base import Base
    from app.data_store.tables import metadata
    from app.data_store.source_range_tables import metadata as ranges
    from app.legacy_reset.tables import metadata as maintenance
    from app.legacy_reset.ownership import LEGACY_TABLES
    # R01's historical foundation objects are protected here as well; D17
    # never invokes R01 reset/retirement operations or clears their data.
    return set(Base.metadata.tables) | set(metadata.tables) | set(ranges.tables) | set(maintenance.tables) | set(LEGACY_TABLES) | {'alembic_version'}


def _tables(c, candidates):
    known = _known_tables()
    namespaces = c.execute(text("SELECT nspname FROM pg_namespace WHERE nspname NOT IN "
                                "('public','pg_catalog','information_schema') AND left(nspname,3)<>'pg_'")).all()
    if namespaces:
        _fail('UNKNOWN_DATABASE_NAMESPACE')
    rows = c.execute(text("SELECT n.nspname, r.relname, r.relkind FROM pg_class r JOIN pg_namespace n ON n.oid=r.relnamespace "
                          "WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%' "
                          "AND n.nspname NOT LIKE 'pg_temp%' AND r.relkind IN ('r','p','v','m','f')")).all()
    for schema, name, kind in rows:
        if schema != 'public' or (name not in known and f'{schema}.{name}' not in candidates) or kind != 'r':
            _fail('UNKNOWN_DATABASE_OBJECT')
    return sorted(name for schema, name, kind in rows if name in known)


def _activity(c, root):
    """Require known terminal decisions; inspect real processes and daemon mounts."""
    for table in ('backtest_runs', 's3_runs'):
        if not inspect(c).has_table(table, schema='public'):
            _fail('RUN_STATE_UNAVAILABLE')
        rows = c.execute(text(f'SELECT status FROM public."{table}" WHERE status NOT IN '
                              "('succeeded','failed','cancelled','timed_out') LIMIT 1")).all()
        if rows:
            _fail('ACTIVE_OR_UNKNOWN_RUN')
    # A terminal record alone cannot prove a named native worker has stopped.
    for (pid,) in c.execute(text('SELECT child_pid FROM public.backtest_runs WHERE child_pid IS NOT NULL')):
        if (Path('/proc')/str(pid)).exists():
            _fail('WORKER_TERMINATION_UNKNOWN')
    # Replaced names do not conceal activity: fd/cwd/maps refer to actual paths.
    for proc in Path('/proc').iterdir():
        if not proc.name.isdecimal() or int(proc.name) == os.getpid():
            continue
        try:
            # Known legacy worker PIDs were checked regardless of UID above;
            # restricted workers of other UIDs are inspected via daemon mounts.
            if proc.stat().st_uid != os.getuid():
                continue
            status = (proc / 'stat').read_text().split(') ', 1)[1].split()[0]
            if status == 'Z':
                continue
            links = [proc / 'cwd', *list((proc / 'fd').iterdir())]
            for link in links:
                try:
                    target = os.readlink(link).removesuffix(' (deleted)')
                except FileNotFoundError:
                    continue
                if target.startswith('/') and Path(target).is_relative_to(root):
                    _fail('ACTIVE_PATH_USER')
            for line in (proc / 'maps').read_text().splitlines():
                fields = line.split(None, 5)
                if len(fields) == 6 and fields[5].startswith('/') and Path(fields[5].removesuffix(' (deleted)')).is_relative_to(root):
                    _fail('ACTIVE_PATH_USER')
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            _fail('PROCESS_ACTIVITY_UNKNOWN')
    from app.backtest_service.process import DockerDaemon
    daemon = DockerDaemon()
    for row in daemon.request('GET', '/containers/json?all=1'):
        item = daemon.inspect(row['Id'])
        if item is None or 'Mounts' not in item:
            _fail('CONTAINER_ACTIVITY_UNKNOWN')
        for mount in item['Mounts']:
            if mount.get('Type') == 'tmpfs':
                continue
            if mount.get('Type') not in ('bind', 'volume'):
                _fail('CONTAINER_ACTIVITY_UNKNOWN')
            source = mount.get('Source')
            if not source or not Path(source).is_absolute():
                _fail('CONTAINER_ACTIVITY_UNKNOWN')
            if _overlap(Path(source), root):
                _fail('CONTAINER_REFERENCE')


def _references(c, tables, root, candidates):
    """Bounded actual stored-reference inspection, including nested JSON text."""
    used_rows = used_bytes = 0
    inspector = inspect(c)
    needles = [str(root), *candidates]
    for table in tables:
        columns = [col['name'] for col in inspector.get_columns(table, schema='public')
                   if any(t in str(col['type']).upper() for t in ('CHAR','TEXT','JSON','ARRAY'))]
        if not columns:
            continue
        names = ','.join('CAST("'+n.replace('"','""')+'" AS text)' for n in columns)
        for row in c.execute(text(f'SELECT {names} FROM public."{table}" LIMIT {MAX_ROWS+1}')):
            used_rows += 1
            if used_rows > MAX_ROWS:
                _fail('REFERENCE_SCAN_BUDGET')
            for value in row:
                if value is None:
                    continue
                used_bytes += len(value.encode('utf8'))
                if used_bytes > MAX_REFERENCE_BYTES:
                    _fail('REFERENCE_SCAN_BUDGET')
                if any(needle in value for needle in needles):
                    _fail('BUSINESS_REFERENCE')
    # Stored functions can contain dynamic SQL without catalog dependencies.
    for (source,) in c.execute(text("SELECT p.prosrc FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public'")):
        used_bytes += len(source.encode('utf8'))
        if used_bytes > MAX_REFERENCE_BYTES:
            _fail('REFERENCE_SCAN_BUDGET')
        if str(root) in source or any(name.split('.')[-1] in source for name in candidates):
            _fail('FUNCTION_REFERENCE')
    # Active source references supplement DB/catalog checks; never scan .env.
    repo = Path(__file__).resolve().parents[1]
    source_roots = [repo/'backend/app', repo/'sdk/python/quantfoundry', repo/'frontend/src', repo/'scripts']
    table_words = [name.split('.')[-1] for name in candidates]
    for directory in source_roots:
        for p in directory.rglob('*'):
            if p.suffix not in {'.py','.pyi','.ts','.tsx','.sh','.sql','.json','.toml'} or '__pycache__' in p.parts:
                continue
            if p.is_symlink():
                _fail('SOURCE_REFERENCE_UNKNOWN')
            body = p.read_text()
            if str(root) in body or any(re.search(r'\b'+re.escape(n)+r'\b', body) for n in table_words):
                _fail('SOURCE_REFERENCE')
    return {'rows_checked': used_rows, 'bytes_checked': used_bytes}


def _tree(fd, device, prefix='', result=None):
    result = [] if result is None else result
    for name in sorted(os.listdir(fd)):
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if info.st_uid != os.getuid() or info.st_dev != device or info.st_mode & 0o022:
            _fail('SHARED_OR_UNCONTROLLED_PATH')
        if len(result)>=MAX_FILES or sum(x['identity'][5] for x in result)+info.st_size>MAX_BYTES:
            _fail('FILE_INVENTORY_BUDGET')
        item = {'path': prefix+name, 'identity': _info(info)}
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                _tree(child, device, prefix+name+'/', result)
            finally:
                os.close(child)
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            opened = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                if _info(os.fstat(opened)) != item['identity']:
                    _fail('PATH_CHANGED')
                digest = hashlib.sha256()
                with os.fdopen(opened, 'rb', closefd=False) as stream:
                    for block in iter(lambda: stream.read(1024**2), b''):
                        digest.update(block)
                item['sha256'] = digest.hexdigest()
            finally:
                os.close(opened)
        else:
            _fail('LINK_OR_UNKNOWN_FILE')
        result.append(item)
        if len(result) > MAX_FILES or sum(x['identity'][5] for x in result) > MAX_BYTES:
            _fail('FILE_INVENTORY_BUDGET')
    return sorted(result, key=lambda x:x['path'])


def _path(c, root, name, root_id):
    relative = _relative(name)
    with _directory(root / relative) as fd:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o022 or info.st_dev != root.stat().st_dev:
            _fail('SHARED_OR_UNCONTROLLED_PATH')
        marker = _json_at(fd, OBJECT_MARKER)
        if set(marker) != {'kind','root_id','run_id','files'} or marker['kind'] != PATH_KIND or marker['root_id'] != root_id:
            _fail('UNKNOWN_PATH_ROLE')
        from uuid import UUID
        run_id = str(UUID(marker['run_id']))
        run = c.execute(text('SELECT status,finished_at,runner_exit_code FROM public.backtest_runs WHERE id=:id'), {'id':run_id}).one_or_none()
        if run is None or run.status not in ('succeeded','failed','cancelled','timed_out') or run.finished_at is None or run.runner_exit_code is None:
            _fail('UNKNOWN_TERMINATION')
        files = marker['files']
        if not isinstance(files,list) or len(set(files)) != len(files) or any(not isinstance(x,str) for x in files):
            _fail('UNKNOWN_PATH_CONTENTS')
        for file in files:
            _relative(file)
        tree = _tree(fd, info.st_dev)
        actual = {x['path'] for x in tree if 'sha256' in x}
        if actual != {*files, OBJECT_MARKER}:
            _fail('UNKNOWN_PATH_CONTENTS')
        return {'path':name,'identity':_info(info),'marker':marker,'tree':tree}


def _table(c, name, root_id, apply=False):
    parts = name.split('.')
    if len(parts) != 2 or parts[0] != 'public' or not all(IDENTIFIER.fullmatch(x) for x in parts):
        _fail('TABLE_NOT_EXACT_QUALIFIED')
    if parts[1] in _known_tables():
        _fail('PROTECTED_DATABASE_OBJECT')
    if apply:
        c.execute(text(f'LOCK TABLE public."{parts[1]}" IN ACCESS EXCLUSIVE MODE NOWAIT'))
    row = c.execute(text("SELECT r.oid,n.oid AS namespace_oid,r.relkind,r.relpersistence,r.relrowsecurity,"
                          "pg_get_userbyid(r.relowner) AS owner,obj_description(r.oid,'pg_class') AS comment,current_user AS username "
                          "FROM pg_class r JOIN pg_namespace n ON n.oid=r.relnamespace WHERE n.nspname=:schema AND r.relname=:name"),
                    {'schema':parts[0], 'name':parts[1]}).mappings().one_or_none()
    if row is None or row['relkind'] != 'r' or row['relpersistence'] != 'p' or row['relrowsecurity'] or row['owner'] != row['username']:
        _fail('UNKNOWN_TABLE_IDENTITY')
    if json.loads(row['comment'] or '{}') != {'kind':TABLE_KIND, 'root_id':root_id}:
        _fail('UNKNOWN_TABLE_ROLE')
    if c.execute(text(f'SELECT EXISTS(SELECT 1 FROM public."{parts[1]}" LIMIT 1)')).scalar_one():
        _fail('NONEMPTY_TABLE')
    if c.execute(text('SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid=:id OR confrelid=:id)'), {'id':row['oid']}).scalar_one():
        _fail('TABLE_CONSTRAINT_OR_FK')
    if c.execute(text('SELECT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid=:id)'), {'id':row['oid']}).scalar_one():
        _fail('TABLE_TRIGGER')
    if c.execute(text("SELECT EXISTS(SELECT 1 FROM pg_depend WHERE refclassid='pg_class'::regclass AND refobjid=:id "
                       "AND NOT (classid='pg_type'::regclass AND deptype='i'))"), {'id':row['oid']}).scalar_one():
        _fail('TABLE_DEPENDENCY')
    columns = c.execute(text('SELECT attname,atttypid,atttypmod,attnotnull FROM pg_attribute WHERE attrelid=:id AND attnum>0 AND NOT attisdropped ORDER BY attnum'), {'id':row['oid']}).all()
    return {'name':name, 'identity':dict(row), 'columns':[list(x) for x in columns]}


def plan(c, root, expected_database, paths, tables, *, apply=False):
    _protected(root)
    if len(set(paths)) != len(paths) or len(set(tables)) != len(tables) or not (paths or tables) or len(paths)+len(tables)>64:
        _fail('INVALID_TARGET_SET')
    relatives = [_relative(p) for p in paths]
    if any(len(p.parts)!=1 for p in relatives):
        _fail('TARGET_MUST_BE_DIRECT_PRIVATE_CHILD')
    if any(_overlap(a,b) for i,a in enumerate(relatives) for b in relatives[i+1:]):
        _fail('OVERLAPPING_TARGETS')
    with _directory(root) as fd:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            _fail('UNCONTROLLED_ROOT')
        marker = _json_at(fd, ROOT_MARKER)
        database = _database(c, expected_database)
        if set(marker) != {'kind','root_id','database'} or marker['kind'] != ROOT_KIND or marker['database'] != database:
            _fail('ROOT_DATABASE_IDENTITY_MISMATCH')
        from uuid import UUID
        root_id = str(UUID(marker['root_id']))
        known = _tables(c,tables)
        if apply:
            # Hold references/run creation stable while removing exact objects.
            # Fail rather than wait for a concurrent worker or reference writer.
            for name in known:
                c.execute(text(f'LOCK TABLE public."{name}" IN SHARE MODE NOWAIT'))
        _activity(c,root)
        references = _references(c,known,root,tables)
        result = {'kind':PLAN_KIND, 'deletion_authorized':False, 'root':str(root),
                  'root_identity':_info(info), 'marker':marker, 'database':database,
                  'reference_inspection':references,
                  'paths':[_path(c,root,p,root_id) for p in sorted(paths)],
                  'tables':[_table(c,t,root_id,apply) for t in sorted(tables)]}
        return result


def _remove_tree(fd, tree):
    # Exact current tree only, bottom-up; never follow a link or recurse into an
    # unlisted entry. A changed node is retained, never treated as extra work.
    for item in sorted(tree, key=lambda x:(len(Path(x['path']).parts),x['path']), reverse=True):
        p = _relative(item['path'])
        current = os.dup(fd)
        try:
            for part in p.parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                os.close(current); current=child
            info = os.stat(p.name,dir_fd=current,follow_symlinks=False)
            # Directory mtime changes when its already-checked children leave.
            expected = item['identity']
            if stat.S_ISDIR(info.st_mode):
                if _info(info)[:4] != expected[:4]:
                    _fail('PATH_CHANGED')
                os.rmdir(p.name, dir_fd=current)
            else:
                if _info(info) != expected:
                    _fail('PATH_CHANGED')
                os.unlink(p.name,dir_fd=current)
        finally:
            os.close(current)


def apply_plan(c, wanted):
    if wanted.get('kind') != PLAN_KIND or wanted.get('deletion_authorized') is not False:
        _fail('UNKNOWN_PLAN')
    root = Path(wanted['root'])
    with _directory(root) as fd:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        current = plan(c,root,wanted['database']['name'],[p['path'] for p in wanted['paths']],
                       [t['name'] for t in wanted['tables']],apply=True)
        if current != wanted:
            _fail('PLAN_IDENTITY_OR_REFERENCES_CHANGED')
        # Recheck activity after all DB locks and inventory are established.
        _activity(c,root)
        for item in current['paths']:
            with _directory(root) as live_root:
                if _info(os.fstat(live_root))[:4] != current['root_identity'][:4]:
                    _fail('ROOT_CHANGED')
            relative = _relative(item['path'])
            with _directory(root/relative) as child:
                if _info(os.fstat(child)) != item['identity'] or _tree(child,os.fstat(child).st_dev) != item['tree']:
                    _fail('PATH_CHANGED')
                _remove_tree(child,item['tree'])
            with _directory((root/relative).parent) as parent:
                if _info(os.stat(relative.name,dir_fd=parent,follow_symlinks=False))[:4] != item['identity'][:4]:
                    _fail('PATH_CHANGED')
                os.rmdir(relative.name,dir_fd=parent)
        for item in current['tables']:
            c.execute(text('DROP TABLE public."'+item['name'].split('.')[1]+'" RESTRICT'))
        return {'mode':'apply','removed_paths':[p['path'] for p in current['paths']],
                'removed_tables':[t['name'] for t in current['tables']], 'business_data_deleted':False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database-url-env', default='QF_D17_CLEANUP_DATABASE_URL')
    parser.add_argument('--database-name')
    parser.add_argument('--root',type=Path)
    parser.add_argument('--path',action='append',default=[])
    parser.add_argument('--table',action='append',default=[])
    parser.add_argument('--output',type=Path)
    parser.add_argument('--apply-plan',type=Path)
    args = parser.parse_args(argv)
    engine = None
    try:
        url = os.environ.get(args.database_url_env)
        if not url:
            _fail('EXPLICIT_DATABASE_REQUIRED')
        engine = create_engine(url,connect_args={'connect_timeout':5})
        if engine.dialect.name != 'postgresql':
            _fail('DATABASE_DIALECT_UNSUPPORTED')
        if args.apply_plan:
            if args.root or args.database_name or args.path or args.table or args.output:
                _fail('APPLY_REQUIRES_SAME_PLAN_ONLY')
            plan_path = args.apply_plan.absolute()
            with _directory(plan_path.parent) as parent:
                opened = os.open(plan_path.name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=parent)
                with os.fdopen(opened,'rb') as source:
                    info = os.fstat(source.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o022 or info.st_size > 4*1024**2:
                        _fail('UNKNOWN_PLAN_FILE')
                    wanted = json.loads(source.read(4*1024**2+1))
            with engine.begin() as c:
                c.execute(text("SET LOCAL lock_timeout='1s'"))
                c.execute(text("SET LOCAL statement_timeout='10s'"))
                result = apply_plan(c,wanted)
        else:
            if not args.root or not args.database_name:
                _fail('EXACT_ROOT_AND_DATABASE_REQUIRED')
            with engine.begin() as c:
                c.execute(text('SET TRANSACTION READ ONLY'))
                c.execute(text("SET LOCAL statement_timeout='10s'"))
                result = {'mode':'dry-run','plan':plan(c,args.root,args.database_name,args.path,args.table)}
            if args.output:
                output_path = args.output.absolute()
                if _overlap(output_path,args.root):
                    _fail('OUTPUT_INSIDE_CLEANUP_ROOT')
                with _directory(output_path.parent) as parent:
                    opened = os.open(output_path.name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent)
                    with os.fdopen(opened,'w') as output:
                        json.dump(result['plan'],output,ensure_ascii=False,indent=2);output.write('\n')
        print(json.dumps(result,ensure_ascii=False))
        return 0
    except Exception as exc:
        # Never print connection strings, SQL, paths from driver errors or keys.
        print(json.dumps({'mode':'refused','code':exc.code if isinstance(exc,Refused) else 'INSPECTION_OR_EXECUTION_UNAVAILABLE',
                          'reason_type':type(exc).__name__}),file=sys.stderr)
        return 2
    finally:
        if engine is not None:
            engine.dispose()


if __name__=='__main__':
    raise SystemExit(main())
