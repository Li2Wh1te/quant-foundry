"""Bounded compressed rescue of legacy containers that may hold unique inputs.

The export is source shaped, never a release or contribution snapshot. Native
copies are compared by their exact primary key and complete original row. A
changed source view invalidates the export before reset can drop a container.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import gzip
import hashlib
import json
import os
from pathlib import Path
from uuid import UUID

from sqlalchemy import text

from app.data_store.adapters.contracts import digest as current_digest, loads, native_json
from app.data_store.adapters.registry import BY_NATIVE
from app.data_store.local_sources import TABLES

from .catalog import ResetRefused, canonical, ident, snapshot
from .operations import require_safe_output, transaction, write_new_json

MAX_RECORD_BYTES = 64 * 1024 * 1024
MAX_RESCUE_BYTES = 4 * 1024 * 1024 * 1024
LEGACY_PREFIX = b'foundation-canonical-v1\0baseline-rows\0'


def _legacy_normalized(value):
    """Freeze the old canonical checksum algorithm without importing its code."""
    if isinstance(value,Decimal):
        if not value.is_finite():
            raise ResetRefused('ORIGINAL_INVALID','原件含非有限精度数。')
        result = format(value,'f')
        return (result.rstrip('0').rstrip('.') if '.' in result else result) if value else '0'
    if isinstance(value,datetime):
        if value.tzinfo is None:
            raise ResetRefused('ORIGINAL_INVALID','原件时间缺少时区。')
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value,(date,UUID)):
        return str(value)
    if isinstance(value,float):
        raise ResetRefused('ORIGINAL_INVALID','原件含二进制浮点数。')
    if isinstance(value,dict):
        return {k:_legacy_normalized(v) for k,v in sorted(value.items())}
    if isinstance(value,(list,tuple)):
        return [_legacy_normalized(v) for v in value]
    if value is None or isinstance(value,(str,int,bool)):
        return value
    raise ResetRefused('ORIGINAL_INVALID','原件存在未登记的数据类型。')


def _legacy_item(value):
    return json.dumps(_legacy_normalized(value),sort_keys=True,ensure_ascii=False,
                      separators=(',',':'),allow_nan=False).encode()


def _hash_rows(rows):
    h = hashlib.sha256(LEGACY_PREFIX)
    h.update(b'[')
    for ordinal,row in enumerate(rows):
        if ordinal:
            h.update(b',')
        h.update(_legacy_item(row))
    h.update(b']')
    return h.hexdigest()


def _pk_columns(c, schema: str, table: str) -> list[tuple[str,str]]:
    rows = c.execute(text("""SELECT a.attname AS name,format_type(a.atttypid,a.atttypmod) AS type
        FROM pg_index i JOIN pg_class r ON r.oid=i.indrelid
        JOIN pg_namespace n ON n.oid=r.relnamespace
        JOIN pg_attribute a ON a.attrelid=r.oid AND a.attnum=ANY(i.indkey)
        WHERE n.nspname=:s AND r.relname=:t AND i.indisprimary
        ORDER BY a.attnum"""),{'s':schema,'t':table}).mappings().all()
    allowed = ('uuid','date','integer','bigint','smallint','text','character varying')
    result = [(r['name'],r['type']) for r in rows]
    if not result or any(not any(kind in dtype for kind in allowed) for _,dtype in result):
        raise ResetRefused('NATIVE_IDENTITY_UNPROVEN','原生表主键类型无法安全对照。')
    return result


def _native_matches(c, schema: str, table: str, row: dict, pk_cache: dict) -> bool:
    keys = pk_cache.setdefault(table,_pk_columns(c,schema,table))
    if any(name not in row or row[name] is None for name,_ in keys):
        raise ResetRefused('NATIVE_IDENTITY_UNPROVEN','旧原件行缺少原生主键。')
    clauses = []
    params = {}
    for ordinal,(name,dtype) in enumerate(keys):
        # Type text comes from pg_catalog, restricted above to simple built-ins.
        clauses.append(f't.{ident(name)}=CAST(:k{ordinal} AS {dtype})')
        params[f'k{ordinal}'] = row[name]
    encoded = c.execute(text(f'SELECT row_to_json(t)::text FROM {ident(schema)}.{ident(table)} t '
        f'WHERE {" AND ".join(clauses)}'),params).scalar_one_or_none()
    return encoded is not None and _legacy_normalized(loads(encoded))==_legacy_normalized(row)


def _baseline_records(c, schema: str, pk_cache: dict):
    sql = f"""SELECT b.id::text AS id,b.source,b.dataset,b.scope_json,b.observed_at,
        b.row_count AS baseline_rows,b.content_hash AS baseline_hash,
        block.ordinal,block.payload_json,block.content_hash AS block_hash,block.row_count AS block_rows
        FROM {ident(schema)}.foundation_baselines b
        LEFT JOIN {ident(schema)}.foundation_baseline_blocks block ON block.baseline_id=b.id
        ORDER BY b.id,block.ordinal"""
    result = c.execute(text(sql).execution_options(stream_results=True,yield_per=1,max_row_buffer=1))
    try:
        previous = None
        baseline_rows = []
        baseline = None
        expected_ordinal = 0
        for item in result.mappings():
            if item['id']!=previous:
                if baseline is not None:
                    yield from _finish_baseline(c,schema,baseline,baseline_rows,pk_cache)
                previous = item['id']; baseline = dict(item)
                baseline_rows = []; expected_ordinal = 0
            if item['ordinal'] is None or item['ordinal']!=expected_ordinal:
                raise ResetRefused('ORIGINAL_INVALID','基线块序号不连续。')
            expected_ordinal += 1
            if len(item['payload_json'].encode())>MAX_RECORD_BYTES:
                raise ResetRefused('ORIGINAL_BUDGET_EXCEEDED','单个原件块超过校验预算。')
            rows = loads(item['payload_json'])
            if not isinstance(rows,list) or len(rows)!=item['block_rows'] or _hash_rows(rows)!=item['block_hash']:
                raise ResetRefused('ORIGINAL_INVALID','基线块摘要或行数不一致。')
            baseline_rows.extend(rows)
            if len(native_json(baseline_rows).encode())>MAX_RECORD_BYTES:
                raise ResetRefused('ORIGINAL_BUDGET_EXCEEDED','单个基线超过校验预算。')
        if baseline is not None:
            yield from _finish_baseline(c,schema,baseline,baseline_rows,pk_cache)
    finally:
        result.close()


def _finish_baseline(c, schema, baseline, rows, pk_cache):
    if len(rows)!=baseline['baseline_rows'] or _hash_rows(rows)!=baseline['baseline_hash']:
        raise ResetRefused('ORIGINAL_INVALID','基线总摘要或行数不一致。')
    scope = loads(baseline['scope_json'])
    source,dataset = baseline['source'],baseline['dataset']
    if source=='tushare' and dataset in TABLES:
        table = TABLES[dataset][0]
        if isinstance(scope,dict) and scope.get('table') not in (None,table):
            raise ResetRefused('ORIGINAL_SCOPE_INVALID','基线声明的来源表与已登记原生表不一致。')
        for row in rows:
            if not isinstance(row,dict):
                raise ResetRefused('ORIGINAL_INVALID','原生表基线不是行对象。')
            if _native_matches(c,schema,table,row,pk_cache):
                continue
            entry = BY_NATIVE.get(('tushare',dataset))
            if entry is None:
                raise ResetRefused('ORIGINAL_SCOPE_INVALID','原生类型缺少新 reader。')
            yield {'format':'qf-local-rescue-v1','kind':'table_row','entry_id':entry.id,
                   'record':row,'snapshot_started_at':baseline['observed_at'].isoformat(),
                   'content_hash':current_digest(row),'origin_baseline':baseline['id']}
    elif source=='tonghuashun' and isinstance(scope,dict) and scope.get('kind')=='local-staged-dump-v1':
        if len(rows)!=1 or not isinstance(rows[0],dict) or rows[0].get('format')!='local-staged-dump-v1' \
           or rows[0].get('native_dataset')!=dataset:
            raise ResetRefused('ORIGINAL_INVALID','旧暂存原件封装无效。')
        entry = BY_NATIVE.get(('tonghuashun',dataset))
        if entry is None:
            raise ResetRefused('ORIGINAL_SCOPE_INVALID','暂存领域缺少新 reader。')
        yield {'format':'qf-local-rescue-v1','kind':'staged_dump','entry_id':entry.id,
               'record':rows[0],'observed_at':baseline['observed_at'].isoformat(),
               'origin_baseline':baseline['id']}
    elif source=='tonghuashun' and dataset=='unresolved_request_units':
        for row in rows:
            yield {'format':'qf-local-rescue-v1','kind':'unresolved_request',
                   'record':row,'observed_at':baseline['observed_at'].isoformat(),
                   'origin_baseline':baseline['id']}
    elif (source=='tushare' and dataset=='full_local_capture_manifest') or \
         (source=='tonghuashun' and dataset=='staged_import_manifest'):
        # These are manifests of already processed originals, not inputs.
        return
    else:
        raise ResetRefused('ORIGINAL_UNCLASSIFIED','旧基线包含未分类原件，拒绝清退。')


def _change_records(c, schema: str, pk_cache: dict):
    sql = f"""SELECT id,dataset,before_json,after_json,observed_at,key_json
        FROM {ident(schema)}.foundation_table_changes ORDER BY id"""
    result = c.execute(text(sql).execution_options(stream_results=True,yield_per=1,max_row_buffer=1))
    try:
        for item in result.mappings():
            dataset = item['dataset']
            if dataset not in TABLES:
                raise ResetRefused('ORIGINAL_UNCLASSIFIED','表变化含未登记来源类型。')
            before = loads(item['before_json']) if item['before_json'] else None
            after = loads(item['after_json']) if item['after_json'] else None
            if not isinstance(before or after,dict):
                raise ResetRefused('ORIGINAL_INVALID','表变化缺少完整前后原件。')
            if after is not None and _native_matches(c,schema,TABLES[dataset][0],after,pk_cache):
                continue
            yield {'format':'qf-local-rescue-v1','kind':'table_change',
                   'entry_id':BY_NATIVE[('tushare',dataset)].id,'before':before,'after':after,
                   'key':loads(item['key_json']),'observed_at':item['observed_at'].isoformat(),
                   'origin_change':item['id']}
    finally:
        result.close()


def _records(c, plan: dict):
    schema = plan['database']['schema']
    cache = {}
    names = {row['name'] for row in plan['tables']}
    if 'foundation_baselines' in names:
        yield from _baseline_records(c,schema,cache)
    if 'foundation_table_changes' in names:
        yield from _change_records(c,schema,cache)


def _record_summary(c, plan: dict, sink=None) -> dict:
    digest = hashlib.sha256()
    count = 0
    bytes_written = 0
    kinds = {}
    for record in _records(c,plan):
        line = native_json(record).encode()+b'\n'
        if len(line)>MAX_RECORD_BYTES or bytes_written+len(line)>MAX_RESCUE_BYTES:
            raise ResetRefused('ORIGINAL_BUDGET_EXCEEDED','救回原件达到有限容量；不能视为完成。')
        if sink:
            sink.write(line)
        digest.update(line)
        count += 1; bytes_written += len(line)
        kinds[record['kind']] = kinds.get(record['kind'],0)+1
    return {'record_count':count,'uncompressed_bytes':bytes_written,
            'records_sha256':digest.hexdigest(),'kinds':kinds}


def export(engine, *, plan: dict, output: Path, manifest_path: Path) -> dict:
    require_safe_output(output)
    require_safe_output(manifest_path)
    if output.suffix!='.gz' or output.exists() or manifest_path.exists():
        raise ResetRefused('OUTPUT_PATH_UNSAFE','救回输出需要新的 .gz 文件和新的清单文件。')
    fd = os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(fd,'wb') as raw:
            with gzip.GzipFile(filename='',mode='wb',fileobj=raw,mtime=0,compresslevel=6) as zipped:
                with transaction(engine,read_only=True) as c:
                    if snapshot(c,expect_database=plan['database']['database'])['database']!=plan['database']:
                        raise ResetRefused('WRONG_DATABASE','导出连接的数据库身份与计划不一致。')
                    summary = _record_summary(c,plan,zipped)
            raw.flush(); os.fsync(raw.fileno())
        if output.stat().st_size>MAX_RESCUE_BYTES:
            raise ResetRefused('ORIGINAL_BUDGET_EXCEEDED','压缩救回文件超过 reader 预算。')
        with output.open('rb') as stream:
            file_hash = hashlib.file_digest(stream,'sha256').hexdigest()
        body = {'format':'qf-local-rescue-manifest-v1','plan_hash':plan['digest'],
                'database':plan['database'],'file':output.name,'file_sha256':file_hash,
                **summary,'verified':False}
        write_new_json(manifest_path,body)
        return body
    except BaseException:
        output.unlink(missing_ok=True)
        raise


def verify(engine, *, plan: dict, rescue_path: Path, manifest_path: Path) -> dict:
    fd = os.open(manifest_path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'r',encoding='utf-8') as stream:
        body = json.load(stream)
    if body.get('format')!='qf-local-rescue-manifest-v1' or body.get('plan_hash')!=plan['digest'] \
       or body.get('database')!=plan['database'] or body.get('file')!=rescue_path.name:
        raise ResetRefused('RESCUE_MANIFEST_MISMATCH','救回清单与计划或文件不一致。')
    fd = os.open(rescue_path,os.O_RDONLY|os.O_NOFOLLOW)
    digest = hashlib.sha256(); size = 0; count = 0
    with os.fdopen(fd,'rb') as stream:
        info = os.fstat(stream.fileno())
        if not __import__('stat').S_ISREG(info.st_mode) or info.st_size>MAX_RESCUE_BYTES:
            raise ResetRefused('RESCUE_PATH_UNSAFE','救回文件不是有限普通文件。')
        actual_hash = hashlib.file_digest(stream,'sha256').hexdigest()
        if actual_hash!=body['file_sha256']:
            raise ResetRefused('RESCUE_FILE_CHANGED','救回文件摘要变化。')
        stream.seek(0)
        with gzip.GzipFile(fileobj=stream,mode='rb') as zipped:
            while True:
                line = zipped.readline(MAX_RECORD_BYTES+1)
                if not line:
                    break
                size += len(line); count += 1
                if len(line)>MAX_RECORD_BYTES or size>MAX_RESCUE_BYTES:
                    raise ResetRefused('ORIGINAL_BUDGET_EXCEEDED','救回读取达到预算。')
                record = loads(line.decode())
                if record.get('format')!='qf-local-rescue-v1':
                    raise ResetRefused('RESCUE_FORMAT_INVALID','救回文件包含未知格式。')
                digest.update(line)
    if (size,count,digest.hexdigest())!=(body['uncompressed_bytes'],body['record_count'],body['records_sha256']):
        raise ResetRefused('RESCUE_FILE_CHANGED','救回文件内容摘要不符。')
    with transaction(engine,read_only=True) as c:
        if snapshot(c,expect_database=plan['database']['database'])['database']!=plan['database']:
            raise ResetRefused('WRONG_DATABASE','校验连接的数据库身份与计划不一致。')
        current = _record_summary(c,plan)
    if any(current[k]!=body[k] for k in ('record_count','uncompressed_bytes','records_sha256','kinds')):
        raise ResetRefused('ORIGINALS_CHANGED','原件或原生副本在救回后变化。')
    body['verified'] = True
    return body
