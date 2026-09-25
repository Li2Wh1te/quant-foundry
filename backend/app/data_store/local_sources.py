"""Bounded, read-only native sources and the D03 self-contained rescue format.

No supplier client, source toggle, legacy execution/definition/source-ref or
publication API is imported. A pass is a fresh repeatable-read snapshot. Primary
keys order a cursor *within that snapshot*, never a permanent change watermark.
A later update starts from the beginning, including late commits and corrections.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
import gzip
from datetime import datetime, timezone
import os
from pathlib import Path
import stat
import time
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from .adapters.canonical import NativeInputError
from .adapters.contracts import LocalInput, digest, loads, native_json, instant_ns
from .adapters.normalize import POINT_KEYS, REPORT_KEYS, GROUP_KEYS
from .adapters.registry import BY_ID, Entry

# Reviewed physical native tables, NOT caller-controlled SQL identifiers.
TABLES = {
    'exchange_calendar': ('trading_calendar_days', None),
    'etf_directory': ('etf_codes', 'source'),
    'etf_code_mapping_audits': ('etf_code_mapping_audits', 'source'),
    'etf_daily': ('etf_daily_bars', 'source'),
    'etf_daily_revision_audits': ('etf_daily_bar_revision_audits', 'source'),
    'etf_adjustment_factors': ('etf_adjustment_factors', 'source'),
    'corporate_action_source_facts': ('corporate_action_source_facts', 'source'),
    'corporate_action_facts': ('corporate_action_facts', 'source'),
    'corporate_action_coverage_facts': ('corporate_action_coverage_facts', 'source'),
    'trading_status_source_facts': ('trading_status_source_facts', 'source'),
    'trading_status_facts': ('trading_status_facts', 'source'),
    'trading_status_coverage_facts': ('trading_status_coverage_facts', 'source'),
    'trading_status_revision_audits': ('trading_status_fact_revision_audits', 'previous_source'),
}


@dataclass(frozen=True)
class SourceLimits:
    page_rows: int = 100
    payload_bytes: int = 32 * 1024 * 1024
    chain_bytes: int = 64 * 1024 * 1024
    pass_seconds: int = 300
    statement_ms: int = 30000
    rescue_bytes: int = 4 * 1024 * 1024 * 1024

    def __post_init__(self):
        if (any(type(v) is not int or v <= 0 for v in vars(self).values()) or
                self.page_rows > 1000 or self.payload_bytes > 64*1024*1024 or
                self.chain_bytes > 256*1024*1024 or self.pass_seconds > 3600 or
                self.statement_ms > 60000 or self.rescue_bytes > 64*1024**3):
            raise ValueError('Invalid finite local-source limits')


def _json(value, limit):
    if not isinstance(value, str) or len(value.encode()) > limit:
        raise NativeInputError('SOURCE_BUDGET_EXCEEDED', '单个原生来源超过解析预算。')
    try:
        return loads(value)
    except (ValueError, TypeError, RecursionError):
        raise NativeInputError('SOURCE_SCHEMA_INVALID', '原生来源编码无效。') from None


def _scope(row):
    return tuple(row.get(k) for k in ('dataset', 'subject', 'variant'))


def _requests(row, limits):
    raw = _json(row.get('request_json', '[]'), limits.payload_bytes)
    if not isinstance(raw, list) or any(not isinstance(r, dict) for r in raw):
        raise NativeInputError('SOURCE_SCHEMA_INVALID', '来源请求依据格式无效。')
    return raw


def _row_keys(data, field):
    rows = data.get('item', [])
    if not isinstance(rows, list):
        raise NativeInputError('SOURCE_SCHEMA_INVALID', '原生窗口不是记录集合。')
    # Preserve duplicates for the domain validator. Do not silently deduplicate.
    return rows


def _confirmed(key, rawkey, requests):
    """Only a request with explicit scope can reconfirm inherited equal points.

    Date ranges prove that a stored key was covered, not that an absent key was
    deleted. NAV day/range metadata is not used as evidence of missing dates.
    """
    for request in requests:
        p = request.get('parameters')
        if not isinstance(p, dict):
            continue
        if key == 'report_key' and {'end_date', 'report_type'} <= p.keys():
            if str(rawkey) == str(p['end_date'])+':'+str(p['report_type']):
                return True
        if key == 'report' and str(p.get('report', '')) == str(rawkey):
            return True
        returned=request.get('returned_keys',{})
        if (request.get('key_receipt')=='actual_returned_keys_v1' and isinstance(returned,dict)
                and isinstance(returned.get(key),list) and rawkey in returned[key]):
            return True
        # Explicit confirmed IDs in a rescue record retain the original scope.
        keys = request.get('confirmed_keys')
        if isinstance(keys, list) and len(keys) <= 100000 and rawkey in keys:
            return True
    return False


def _receipt_excludes(field, key, requests):
    # Every relevant successfully returned raw page carries explicit keys. The
    # absence of a key preserves its previous confirmation; it never withdraws it.
    receipts=[r.get('returned_keys',{}).get(field) for r in requests
              if r.get('key_receipt')=='actual_returned_keys_v1']
    return bool(receipts) and len(receipts)==len(requests) and all(isinstance(v,list) for v in receipts) and all(key not in v for v in receipts)


def _proof_order(field, key, requests, observed):
    applicable=[]
    for request in requests:
        if _confirmed(field,key,[request]):
            when=request.get('source_observed_at')
            value=instant_ns(when) if when is not None else observed
            if value>observed:
                raise NativeInputError('SOURCE_ORDER_UNPROVEN','来源确认时间晚于保存观察。')
            applicable.append(value)
    return max(applicable) if applicable else observed


def materialize_observation(row, lookup, limits=SourceLimits()):
    """Reconstruct and validate every dependency; never consult a mutable head."""
    chain, seen, cursor, size = [], set(), row, 0
    while True:
        identity = str(cursor.get('id'))
        if identity in seen or len(chain) >= 31 or _scope(cursor) != _scope(row):
            raise NativeInputError('SOURCE_DEPENDENCY_INVALID', '原生依赖循环、跨范围或过深。')
        seen.add(identity)
        size += len(str(cursor.get('data_json', '')).encode())
        if size > limits.chain_bytes:
            raise NativeInputError('SOURCE_BUDGET_EXCEEDED', '原生依赖链超过解析预算。')
        chain.append(cursor)
        parent = cursor.get('base_observation_id')
        if parent is None:
            break
        cursor = lookup(str(parent))
        if cursor is None:
            raise NativeInputError('SOURCE_DEPENDENCY_MISSING', '缺少原生增量依赖。')
    data, basis, field = None, {}, POINT_KEYS.get(row['dataset']) or REPORT_KEYS.get(row['dataset']) or GROUP_KEYS.get(row['dataset'])
    for current in reversed(chain):
        body = _json(current.get('data_json'), limits.payload_bytes)
        if not isinstance(body, dict):
            raise NativeInputError('SOURCE_SCHEMA_INVALID', '原生观察不是对象。')
        order = instant_ns(current['observed_at'])
        token = digest([_scope(current), current['content_hash'], current['observed_at'],
                        _requests(current, limits)])
        if data is None:
            data = body
            if field:
                basis = {str(r.get(field)): (_proof_order(field,r.get(field),_requests(current,limits),order), token)
                         for r in _row_keys(data, field) if isinstance(r, dict)}
        else:
            if (not field or body.get('key_field') != field or not isinstance(body.get('metadata'), dict)
                    or not isinstance(body.get('removed'), list) or not isinstance(body.get('upserts'), list)):
                raise NativeInputError('SOURCE_DEPENDENCY_INVALID', '原生增量格式与领域不一致。')
            old = _row_keys(data, field)
            if any(not isinstance(r, dict) or field not in r for r in old):
                raise NativeInputError('SOURCE_DEPENDENCY_INVALID', '基础窗口缺少业务键。')
            keys = [native_json(r[field]) for r in old]
            if len(set(keys)) != len(keys):
                raise NativeInputError('SOURCE_DEPENDENCY_INVALID', '基础窗口包含重复键。')
            records = {r[field]: r for r in old}
            for key in body['removed']:
                records.pop(key, None); basis.pop(str(key), None)
            changes = body['upserts']
            if any(not isinstance(r, dict) or field not in r for r in changes):
                raise NativeInputError('SOURCE_DEPENDENCY_INVALID', '增量缺少业务键。')
            if len({native_json(r[field]) for r in changes}) != len(changes):
                raise NativeInputError('SOURCE_DEPENDENCY_INVALID', '增量包含重复键。')
            for r in changes:
                records[r[field]] = r
                basis[str(r[field])] = _proof_order(field,r[field],_requests(current,limits),order), token
            for key in records:
                if _confirmed(field, key, _requests(current, limits)):
                    basis[str(key)] = _proof_order(field,key,_requests(current,limits),order), token
            try:
                data = {**body['metadata'], 'item': [records[k] for k in sorted(records)]}
            except TypeError:
                raise NativeInputError('SOURCE_DEPENDENCY_INVALID', '增量业务键类型不一致。') from None
        if digest(data) != current['content_hash']:
            raise NativeInputError('SOURCE_HASH_MISMATCH', '原生观察内容校验失败。')
        if len(native_json(data).encode()) > limits.payload_bytes:
            raise NativeInputError('SOURCE_BUDGET_EXCEEDED', '重建窗口超过解析预算。')
    return data, basis, field


# These native collectors merge historical rows before writing an observation.
# Other endpoints return whole current objects, so an equal returned value is
# genuinely reconfirmed by the new observation, not by an inferred request bound.
_MERGED = {'etf_daily','stock_daily','index_daily','fund_nav','stock_income','stock_balance',
           'stock_cash_flow','stock_indicators','fund_performance_history','rank_trend','fund_news',
           'fund_stock_history','fund_bond_history','stock_actions'}


def _excluded(field, key, requests):
    relevant=[]
    for req in requests:
        p=req.get('parameters',{})
        if not isinstance(p,dict):continue
        if field in ('date_ms','period_end_ms','nav_date') and type(key) is int:
            if type(p.get('start')) is int and type(p.get('end')) is int:
                relevant.append(p['start']<=key<=p['end'])
        if field=='report' and 'report' in p:relevant.append(str(key)==str(p['report']))
        if field=='report_key' and {'end_date','report_type'}<=p.keys():
            relevant.append(str(key)==str(p['end_date'])+':'+str(p['report_type']))
    return bool(relevant) and not any(relevant)


class EffectiveBasis:
    """Keep sparse confirmation through native full re-anchors.

    Legacy receipts without returned-key proof cannot safely grant a new
    confirmation to an inherited equal value. Such possibly covered keys become
    explicitly restricted, rather than reviving the FF-01 last-writer bug.
    Out-of-order rescue observations must be sorted by native scope/time before
    reading; table snapshots and pipeline input arrival are independently ordered.
    """
    def __init__(self):
        self.scope=None;self.position=None;self.rows={};self.basis={}

    def apply(self,row,data,basis,field,requests):
        scope=_scope(row);ns=instant_ns(row['observed_at'])
        position=(*scope,ns,str(row['id']))
        if self.position is not None and position<self.position:
            raise NativeInputError('RESCUE_ORDER_INVALID','原生观察需按范围和观察时间排序；不能用文件顺序作为来源先后。')
        self.position=position
        token=digest([scope,row['content_hash'],row['observed_at'],requests])
        current={};uncertain=[]
        if field:
            grouped={}
            for item in _row_keys(data,field):
                if isinstance(item,dict):grouped.setdefault(str(item.get(field)),[]).append(item)
            for key,items in grouped.items():
                rawkey=items[0].get(field)
                # Multiple legitimate corporate actions share one ex-date. Hash
                # the whole group; never let the last event erase its siblings.
                hashed=digest(sorted(digest(v) for v in items));current[key]=hashed
                confirmed=_confirmed(field,rawkey,requests)
                merged=(row['dataset'] in _MERGED and (row['dataset']!='stock_actions' or
                        any(r.get('artifact_sha256') for r in requests)))
                proof_order=_proof_order(field,rawkey,requests,ns)
                if scope==self.scope and self.rows.get(key)==hashed and merged:
                    if confirmed:basis[key]=(proof_order,token)
                    else:
                        basis[key]=self.basis.get(key,basis.get(key,(ns,token)))
                        if not (_excluded(field,rawkey,requests) or _receipt_excludes(field,rawkey,requests)):
                            uncertain.append(key)
                elif not merged or confirmed:
                    basis[key]=(proof_order,token)
                elif any(r.get('artifact_sha256') for r in requests):
                    # A rescued already-merged import without its old originals
                    # cannot turn inherited rows into freshly acquired evidence.
                    uncertain.append(key)
        self.scope,self.rows,self.basis=scope,current,dict(basis)
        return basis,tuple(uncertain)


class NativeSources:
    """One iterator invocation is one bounded consistent read; no network fallback."""
    def __init__(self, engine, *, limits=SourceLimits(), cancelled=None):
        if engine.dialect.name != 'postgresql':
            raise ValueError('Native sources require PostgreSQL')
        self.engine, self.limits, self.cancelled = engine, limits, cancelled
        self.summary = {}

    def _check(self):
        if self.cancelled and self.cancelled():
            raise NativeInputError('OPERATION_CANCELLED', '本地读取已取消。')
        if time.monotonic() > self.deadline:
            raise NativeInputError('SOURCE_BUDGET_EXCEEDED', '本地读取达到有限时间预算，范围未完成。')

    @contextmanager
    def _snapshot(self):
        self.deadline = time.monotonic() + self.limits.pass_seconds
        with self.engine.connect().execution_options(isolation_level='REPEATABLE READ') as c:
            with c.begin():
                c.execute(text('SET TRANSACTION READ ONLY'))
                c.execute(text("SELECT set_config('statement_timeout',:n,true)"), {'n':str(self.limits.statement_ms)})
                c.execute(text("SELECT set_config('lock_timeout','5000',true)"))
                when = c.execute(text('SELECT transaction_timestamp()')).scalar_one()
                yield c, when

    def _rows(self, c, table, where='', params=None, order=''):
        # Caller table and clauses originate exclusively in this module.
        sql = (f'SELECT CASE WHEN octet_length(row_to_json(t)::text)<=:payload_budget '
               f'THEN row_to_json(t)::text ELSE NULL END FROM {table} t {where} {order}')
        result = c.execution_options(stream_results=True, yield_per=1, max_row_buffer=1).execute(
            text(sql),{**(params or {}),'payload_budget':self.limits.payload_bytes})
        try:
            for (encoded,) in result:
                self._check()
                yield _json(encoded, self.limits.payload_bytes)
        finally:
            result.close()

    def iter_entry(self, entry: Entry):
        self.summary = {'entry_id':entry.id, 'source_rows':0, 'state':'reading',
                        'complete':False, 'scan_policy':'fresh_repeatable_read_rescan'}
        try:
            with self._snapshot() as (c, when):
                self.summary['snapshot_started_at'] = when.isoformat()
                if entry.source == 'tushare':
                    table, column = TABLES[entry.native]
                    where = f'WHERE t.{column}=:source' if column else ''
                    # Server cursor, no ORM materialization of the full table.
                    for row in self._rows(c, table, where, {'source':'tushare'}):
                        self.summary['source_rows'] += 1
                        if not entry.business:
                            continue
                        subject = str(row.get('ts_code') or row.get('instrument_id') or row.get('exchange') or 'market')
                        token = digest(row)
                        basis = {}
                        kind = 'current_table_snapshot'
                        if entry.native == 'corporate_action_facts':
                            version = row.get('fact_version')
                            if type(version) is not int or not 0 < version < 2**63:
                                yield LocalInput('tushare', entry.native, subject, 'default', when, row, token,
                                                 failure='SOURCE_ORDER_UNPROVEN'); continue
                            kind = 'source_revision'
                            basis[str(row.get('logical_fact_key'))] = version, token
                        yield LocalInput('tushare', entry.native, subject, 'default', when, row, token,
                                         order_kind=kind, row_basis=basis, representation='native_table')
                elif entry.disposition == 'remove_operational_pseudodataset':
                    pass  # The old artificial empty scope is deliberately not a dataset.
                elif entry.disposition == 'ingestion_channel':
                    target = BY_ID[entry.target]
                    # Native importer already writes actual price/event observations.
                    # Process those observations through the same target adapter, not
                    # stage rows that have not completed validation/publication.
                    self.summary['target_entry'] = target.id
                    for _ in self._rows(c, 'tonghuashun_dump_imports', 'WHERE t.dataset=:d', {'d':entry.native}):
                        self.summary['source_rows'] += 1
                        self.summary['import_status'] = _.get('status')
                    self.summary['state'] = 'ingestion_channel'
                else:
                    yield from self._ths(c, entry, when)
            self.summary['complete'] = True
            if self.summary['state'] == 'reading':
                self.summary['state'] = ('support' if not entry.business else
                                         'present' if self.summary['source_rows'] else 'empty')
        except SQLAlchemyError:
            self.summary['state'] = 'unavailable'
            raise NativeInputError('LOCAL_SOURCE_UNAVAILABLE', '原生本地表不可读取；没有调用供应商。') from None

    def _ths(self, c, entry, when):
        def lookup(identity):
            value = c.execute(text('SELECT CASE WHEN octet_length(row_to_json(t)::text)<=:n '
                                   'THEN row_to_json(t)::text ELSE NULL END '
                                   'FROM tonghuashun_observations t WHERE id=:id'),
                              {'id':UUID(identity),'n':self.limits.payload_bytes}).scalar_one_or_none()
            return _json(value, self.limits.payload_bytes) if value is not None else None
        tracker = EffectiveBasis()
        order = 'ORDER BY t.subject, t.variant, t.observed_at, t.id'
        # Equal timestamp conflicts are retained for the merger. The UUID is only
        # a deterministic iteration tie-break, never authority over another value.
        for row in self._rows(c, 'tonghuashun_observations', 'WHERE t.dataset=:d', {'d':entry.native}, order):
            self.summary['source_rows'] += 1
            try:
                content, basis, field = materialize_observation(row, lookup, self.limits)
                scope = _scope(row)
                token = digest([scope,row['content_hash'],row['observed_at'],_requests(row,self.limits)])
                ns = instant_ns(row['observed_at'])
                basis, uncertain = tracker.apply(row,content,basis,field,_requests(row,self.limits))
                value = LocalInput('tonghuashun',entry.native,row['subject'],row['variant'],
                                   row['observed_at'], content, token, row_basis=basis, basis_field=field,
                                   unconfirmed_keys=uncertain)
            except (NativeInputError, ValueError, TypeError, KeyError, RecursionError) as exc:
                code = exc.code if isinstance(exc,NativeInputError) else 'SOURCE_SCHEMA_INVALID'
                # Scope/time come from typed native columns, not the broken payload.
                value = LocalInput('tonghuashun',entry.native,row['subject'],row['variant'],row['observed_at'],
                                   {},digest([row.get('id'),row.get('content_hash')]),failure=code)
            yield value
        # Last failed refresh is separate from last-good native observation. Never
        # transform a failed attempted refresh into successful empty data.
        for row in self._rows(c,'tonghuashun_collection_states',
                              "WHERE t.dataset=:d AND t.status IN ('failed','partial')", {'d':entry.native}):
            self.summary['source_failures'] = self.summary.get('source_failures',0)+1
            if row['status']=='partial':
                continue  # per-report failures live in the observation; don't block unrelated reports twice
            yield LocalInput('tonghuashun',entry.native,row['subject'],row['variant'],row['attempted_at'],
                             {},digest([_scope(row),row.get('attempted_at'),row.get('error_kind')]),
                             failure='SOURCE_REFRESH_FAILED')


class RescueSources:
    """D03 qf-local-rescue-v1 JSONL, exact original records + closed dependencies.

    Each line: {format, kind, entry_id, record, dependencies?}. `ths_observation`
    contains original observation columns (including content hash/request_json),
    with *all* delta dependencies. `table_row` contains the original typed table
    row and `snapshot_started_at`. No old official payload is accepted as raw.
    Files are read only, never installed as an alternative formal history store.
    """
    def __init__(self, paths, *, limits=SourceLimits(), cancelled=None):
        self.paths = tuple(Path(p) for p in paths)
        if not self.paths or len(self.paths)>128: raise ValueError('Invalid rescue file list')
        self.limits, self.cancelled, self.summary = limits, cancelled, {}

    def iter_entry(self, entry):
        self.summary={'entry_id':entry.id,'source_rows':0,'state':'reading','complete':False,
                      'scan_policy':'self_contained_rescue_rescan'}
        started=time.monotonic(); self._total=0; tracker=EffectiveBasis()
        for path in self.paths:
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
            try:
                info=os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size>self.limits.rescue_bytes:
                    raise NativeInputError('SOURCE_BUDGET_EXCEEDED','救回来源不是有界普通文件。')
                with os.fdopen(fd,'rb',closefd=False) as handle:
                    reader=gzip.GzipFile(fileobj=handle) if path.suffix=='.gz' else handle
                    try:
                        yield from self._file_records(reader,entry,tracker,started)
                    finally:
                        if reader is not handle: reader.close()
            finally:
                os.close(fd)
        self.summary.update(complete=True,state='present' if self.summary['source_rows'] else 'empty')

    def _file_records(self,reader,entry,tracker,started):
        while True:
            line=reader.readline(self.limits.chain_bytes+1)
            if not line: break
            self._total+=len(line)
            if (len(line)>self.limits.chain_bytes or self._total>self.limits.rescue_bytes or
                    time.monotonic()-started>self.limits.pass_seconds):
                raise NativeInputError('SOURCE_BUDGET_EXCEEDED','救回来源达到预算，未完成读取。')
            if self.cancelled and self.cancelled():
                raise NativeInputError('OPERATION_CANCELLED','救回来源读取已取消。')
            record=loads(line.decode('utf-8'))
            if not isinstance(record,dict) or record.get('format')!='qf-local-rescue-v1' or record.get('kind') not in ('ths_observation','table_row','staged_dump','table_change','unresolved_request'):
                raise NativeInputError('SOURCE_SCHEMA_INVALID','不支持该救回格式；旧正式数据不能冒充原件。')
            if record.get('kind')=='unresolved_request':
                self.summary['unresolved_requests']=self.summary.get('unresolved_requests',0)+1
                continue
            if record.get('entry_id')!=entry.id: continue
            if record['kind']=='table_change':
                self.summary['historical_changes']=self.summary.get('historical_changes',0)+1
                continue
            self.summary['source_rows']+=1
            raw=record['record']
            if not entry.business: continue
            if record['kind']=='staged_dump':
                body=raw.get('content') if isinstance(raw,dict) else None
                meta=body.get('bulk_source') if isinstance(body,dict) else None
                when=meta.get('collected_at') if isinstance(meta,dict) else None
                if (not isinstance(raw,dict) or not isinstance(body,dict) or not isinstance(meta,dict)
                        or entry.source!='tonghuashun' or raw.get('format')!='local-staged-dump-v1'
                        or raw.get('native_dataset')!=entry.native or not isinstance(body.get('item'),list)
                        or not isinstance(raw.get('subject'),str) or not meta.get('sha256')
                        or when is None or instant_ns(when)>instant_ns(record['observed_at'])):
                    raise NativeInputError('SOURCE_ORDER_UNPROVEN','救回暂存原件缺少可验证的采集时间或身份。')
                yield LocalInput('tonghuashun',entry.native,raw['subject'],'default',when,body,
                                 digest([raw['subject'],entry.native,body,when]),
                                 representation='rescued_staged_dump')
                continue
            if record['kind']=='ths_observation':
                if entry.source!='tonghuashun' or raw.get('dataset')!=entry.native:
                    raise NativeInputError('IDENTITY_CONFLICT','救回入口与原生范围不一致。')
                deps=record.get('dependencies',[])
                if len(deps)>30 or len({str(d['id']) for d in deps})!=len(deps):
                    raise NativeInputError('SOURCE_DEPENDENCY_INVALID','救回依赖不完整或重复。')
                lookup={str(r['id']):r for r in deps}
                body,basis,field=materialize_observation(raw,lookup.get,self.limits)
                basis,uncertain=tracker.apply(raw,body,basis,field,_requests(raw,self.limits))
                # Rescue records may provide original sparse confirmations,
                # but every supplied tuple must name a retained native input.
                tokens={digest([_scope(r),r['content_hash'],r['observed_at'],_requests(r,self.limits)]):instant_ns(r['observed_at'])
                        for r in [raw,*deps]}
                for k,v in record.get('row_basis',{}).items():
                    if not isinstance(v,list) or len(v)!=2 or tokens.get(v[1])!=v[0]:
                        raise NativeInputError('SOURCE_ORDER_UNPROVEN','救回确认依据没有对应原件。')
                    origin=next(r for r in [raw,*deps] if digest([_scope(r),r['content_hash'],r['observed_at'],_requests(r,self.limits)])==v[1])
                    original,_,_=materialize_observation(origin,lookup.get,self.limits)
                    selected=[r for r in original.get('item',[]) if isinstance(r,dict) and str(r.get(field))==k]
                    current=[r for r in body.get('item',[]) if isinstance(r,dict) and str(r.get(field))==k]
                    if (not selected or digest(selected)!=digest(current) or
                            not _confirmed(field,selected[0].get(field),_requests(origin,self.limits))):
                        raise NativeInputError('SOURCE_ORDER_UNPROVEN','救回依据没有证明目标键确实返回并确认。')
                    basis[k]=tuple(v)
                yield LocalInput(entry.source,entry.native,raw['subject'],raw['variant'],raw['observed_at'],
                                 body,digest([_scope(raw),raw['content_hash'],raw['observed_at'],_requests(raw,self.limits)]),
                                 row_basis=basis,basis_field=field,representation='rescue_observation',
                                 unconfirmed_keys=tuple(k for k in uncertain if k not in record.get('row_basis',{})))
            else:
                if entry.source!='tushare':
                    raise NativeInputError('IDENTITY_CONFLICT','救回本地表来源不一致。')
                when=record['snapshot_started_at']; instant_ns(when)
                if record.get('content_hash')!=digest(raw):
                    raise NativeInputError('SOURCE_HASH_MISMATCH','救回本地表内容摘要不符。')
                subject=str(raw.get('ts_code') or raw.get('instrument_id') or raw.get('exchange') or 'market')
                basis={}; kind='current_table_snapshot'
                if entry.native=='corporate_action_facts':
                    version=raw.get('fact_version')
                    if type(version) is not int or not 0<version<2**63:
                        raise NativeInputError('SOURCE_ORDER_UNPROVEN','公司行动缺少来源修订顺序。')
                    basis[str(raw.get('logical_fact_key'))]=(version,digest(raw)); kind='source_revision'
                yield LocalInput(entry.source,entry.native,subject,'default',when,raw,digest(raw),
                                 order_kind=kind,row_basis=basis,representation='rescue_table')


class CombinedSources:
    """Read retained native sources and proven rescued gaps in one D02 pass.

    A rescue file supplements native rows. It never replaces the current native
    snapshot or turns a historical table change into a present source row.
    """
    def __init__(self, native: NativeSources, rescue: RescueSources):
        self.native,self.rescue = native,rescue
        self.summary = {}

    def iter_entry(self, entry):
        for value in self.rescue.iter_entry(entry):
            yield value
        rescued = dict(self.rescue.summary)
        for value in self.native.iter_entry(entry):
            yield value
        native = dict(self.native.summary)
        self.summary = dict(native, source_rows=native.get('source_rows',0)+rescued.get('source_rows',0),
                            rescue_rows=rescued.get('source_rows',0),
                            rescue_historical_changes=rescued.get('historical_changes',0),
                            rescue_unresolved_requests=rescued.get('unresolved_requests',0),
                            complete=bool(native.get('complete') and rescued.get('complete')))
