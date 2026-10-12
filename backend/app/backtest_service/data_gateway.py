"""Trusted run-scoped CurrentStore consumer. Never imports or executes strategy.

Bindings are code-owned projections of existing contracts, not client SQL or
supplier adapters. No default assumption upgrades provider_reported/unknown
units to raw execution prices. D12 supplies ownership and private socket;
D05 applies simulation visibility again after this resource/permission gate.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass, field, replace
import hashlib
import json
import time
from types import MappingProxyType
from typing import Callable, Mapping
from uuid import uuid4

import pyarrow as pa
import pyarrow.compute as pc
import numpy as np
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.auth import AuthenticatedPrincipal
from app.data_store.availability import require_ready
from app.data_store.errors import DataStoreError
from app.data_store.readers import Query
from app.data_store.schema import DatasetSpec, fingerprint

SCHEMA_ID = 'qf.market.v1'
TEXT_FIELDS = ('kind', 'security', 'session', 'source_session', 'channel',
               'open', 'high', 'low', 'close', 'price', 'bid', 'ask')
INT_FIELDS = ('time_ns', 'interval_start_ns', 'interval_end_ns', 'quantity', 'bid_quantity', 'ask_quantity')
SEQ_FIELDS = ('sequence', 'stable_input_sequence')
MARKET_SCHEMA = pa.schema([pa.field(n, pa.string()) for n in TEXT_FIELDS]
    + [pa.field(n, pa.int64()) for n in INT_FIELDS]
    + [pa.field(n, pa.uint64()) for n in SEQ_FIELDS], metadata={
        b'schema_id': SCHEMA_ID.encode(), b'time_unit': b'utc_nanoseconds',
        b'exchange_timezone': b'Asia/Shanghai', b'price_currency': b'CNY',
        b'quantity_unit': b'shares', b'price_basis': b'raw'})
IDENTITY_FIELDS = frozenset({'kind', 'security', 'session', 'source_session', 'channel',
                           'sequence', 'stable_input_sequence', 'time_ns',
                           'interval_start_ns', 'interval_end_ns'})
PUBLIC_FIELDS = frozenset({'open', 'high', 'low', 'close', 'price', 'bid', 'ask',
                         'quantity', 'bid_quantity', 'ask_quantity'})
# One retained event source and one synchronous callback query. This is a
# channel limit, not a strategy-selected option or a pool of prefetched streams.
MAX_ACTIVE_STREAMS = 2


class GatewayError(ValueError):
    def __init__(self, code, message, *, store_code=None):
        self.code, self.operation, self.message = code, 'data_gateway', message
        self.scope = {} if store_code is None else {'store_code': store_code,
            'http_status': '503' if store_code in {'DATA_STORE_REBUILDING', 'CATALOG_UNAVAILABLE',
                'STORAGE_UNAVAILABLE', 'DATA_STORE_NOT_INITIALIZED', 'LOCK_TIMEOUT'} else
                '409' if store_code in {'DATA_RESTRICTED', 'DATA_CHANGED'} else '422'}
        super().__init__(message)

    def to_dict(self):
        return dict(code=self.code, operation=self.operation, message=self.message, scope=self.scope)


@contextmanager
def store_errors():
    try:
        yield
    except DataStoreError as error:
        code = ('DATA_CHANGED' if error.code == 'DATA_CHANGED' else
                'DATA_RESTRICTED' if error.code == 'DATA_RESTRICTED' else
                'CANCELLED' if error.code == 'OPERATION_CANCELLED' else
                'RESOURCE_LIMIT' if error.code.endswith('BUDGET_EXCEEDED') or error.code in
                    {'QUERY_TIMEOUT', 'MEMORY_PRESSURE'} else 'CAPABILITY_UNAVAILABLE')
        raise GatewayError(code, str(error), store_code=error.code) from None
    except SQLAlchemyError as error:
        state = getattr(getattr(error, 'orig', None), 'sqlstate', None)
        if state == '57014':
            raise GatewayError('RESOURCE_LIMIT', '依赖检查达到时间预算', store_code='QUERY_TIMEOUT') from None
        raise GatewayError('CAPABILITY_UNAVAILABLE', '当前目录暂不可访问',
                           store_code='LOCK_TIMEOUT' if state == '55P03' else 'CATALOG_UNAVAILABLE') from None
    except (pa.ArrowException, OverflowError):
        raise GatewayError('NUMERIC_RANGE_UNSUPPORTED', '输入数值不能无损转换为引擎类型') from None


def _ns(value):
    if type(value) is not str:
        raise GatewayError('INVALID_CONTRACT', '时间必须为规范纳秒字符串')
    try:
        number = int(value)
    except ValueError:
        raise GatewayError('INVALID_CONTRACT', '时间必须为规范纳秒字符串') from None
    if str(number) != value or not -(2**63) <= number < 2**63:
        raise GatewayError('NUMERIC_RANGE_UNSUPPORTED', '时间超出纳秒范围')
    return number


@dataclass(frozen=True)
class AuthorizedRun:
    """Constructed from authenticated, accepted run by trusted supervisor."""
    run_id: str
    owner_scope: str
    universe: tuple[str, ...]
    start_ns: int
    end_ns: int

    def __post_init__(self):
        if (not self.run_id or len(self.run_id.encode()) > 128 or not self.owner_scope
                or type(self.universe) is not tuple or not 1 <= len(self.universe) <= 10000
                or len(set(self.universe)) != len(self.universe)
                or any(type(s) is not str or not s or len(s.encode()) > 128 for s in self.universe)
                or type(self.start_ns) is not int or type(self.end_ns) is not int
                or not -(2**63) <= self.start_ns <= self.end_ns < 2**63):
            raise GatewayError('INVALID_CONTRACT', '授权运行范围无效')


@dataclass(frozen=True)
class MarketBinding:
    """One accepted existing layout. All callbacks/configuration are trusted.

    bounds selects physical business keys; predicates select real event time.
    transform is a bounded Arrow column projection, never a native normalizer.
    Unknown price/quantity basis must be denied with unavailable_reason.
    """
    name: str
    spec: DatasetSpec
    frequency: str
    fields: Mapping[str, str]
    constants: Mapping[str, str | int]
    bounds: Callable[[str, int, int], tuple[tuple, tuple]]
    time_column: str
    time_offset_ns: int = 0
    guards: tuple[tuple[str, str | int], ...] = ()
    limitations: tuple[str, ...] = ('current_revision_not_historical_snapshot', 'observed_rows_not_coverage')
    unavailable_reason: str | None = None
    transform: Callable[[pa.Table], pa.Table] | None = None
    extra_columns: tuple[str, ...] = ()
    derived_columns: tuple[str, ...] = ()
    selectors: tuple[tuple[str, str, object], ...] = ()
    # Code-owned conversions for date-keyed formal nodes. These do not alter
    # the CurrentStore quality gate or expose native keys to a strategy.
    query_time_bounds: Callable | None = None
    storage_securities: Callable | None = None
    count_order: tuple[str, ...] | None = None

    def __post_init__(self):
        if (self.frequency not in ('1d', '1m', '5m', '15m', '30m', '60m', 'tick')
                or not set(self.fields) <= set(MARKET_SCHEMA.names)
                or not set(self.constants) <= set(MARKET_SCHEMA.names)
                or any(c not in self.spec.schema.names and c not in self.derived_columns for c in self.fields.values())
                or any(c not in self.spec.schema.names for c in self.extra_columns)
                or len(self.selectors) > 14
                or any(c not in self.spec.schema.names or op not in ('=', '>=', '<=', '>', '<')
                       for c, op, _ in self.selectors)
                or self.time_column not in self.spec.schema.names):
            raise GatewayError('INVALID_CONTRACT', '数据投影契约无效')
        if self.count_order is not None and (not self.count_order or
                any(c not in self.spec.schema.names for c in self.count_order)):
            raise GatewayError('INVALID_CONTRACT', '数据计数排序投影无效')
        object.__setattr__(self, 'fields', MappingProxyType(dict(self.fields)))
        object.__setattr__(self, 'constants', MappingProxyType(dict(self.constants)))

    def project(self, table, wanted):
        for column, expected in self.guards:
            values = table[column]
            if table.num_rows and (values.null_count or not pc.all(pc.equal(values, expected)).as_py()):
                raise GatewayError('CAPABILITY_UNAVAILABLE', '存储字段单位或价格基础未满足运行能力')
        if self.transform is not None:
            table = self.transform(table)
        arrays = []
        for f in MARKET_SCHEMA:
            if f.name not in wanted:
                arrays.append(pa.nulls(table.num_rows, f.type))
            elif f.name in self.fields:
                arrays.append(pc.cast(table[self.fields[f.name]], f.type, safe=True))
            elif f.name in self.constants:
                arrays.append(pa.array([self.constants[f.name]]*table.num_rows, type=f.type))
            else:
                arrays.append(pa.nulls(table.num_rows, f.type))
        result = pa.Table.from_arrays(arrays, schema=MARKET_SCHEMA)
        if self.time_offset_ns:
            result = result.set_column(result.schema.get_field_index('time_ns'), MARKET_SCHEMA.field('time_ns'),
                                       pc.add_checked(result['time_ns'], self.time_offset_ns))
        return result


@dataclass
class DependencyContext:
    scope_id: str
    dependencies: dict[str, dict] = field(default_factory=dict)
    closed: bool = False

    def to_dict(self):
        return dict(scope_id=self.scope_id, dependencies=list(self.dependencies.values()))


@dataclass(frozen=True)
class DataBatch:
    table: pa.Table
    metadata: dict

    def ipc(self, budget):
        if self.table.nbytes + 16384 > budget:
            raise GatewayError('RESOURCE_LIMIT', 'Arrow 批次超过传输预算')
        sink = pa.BufferOutputStream()
        with pa.ipc.new_stream(sink, self.table.schema, options=pa.ipc.IpcWriteOptions(compression=None)) as writer:
            table = self.table.combine_chunks()
            batch = table.to_batches()[0] if table.num_rows else pa.RecordBatch.from_arrays(
                [pa.array([], type=f.type) for f in table.schema], schema=table.schema)
            writer.write_batch(batch)
        payload = sink.getvalue().to_pybytes()
        if len(payload) > budget:
            raise GatewayError('RESOURCE_LIMIT', 'Arrow 批次超过传输预算')
        return payload


class RunDataGateway:
    def __init__(self, store, principal: AuthenticatedPrincipal, grant: AuthorizedRun,
                 bindings: tuple[MarketBinding, ...], *, cancelled=None,
                 batch_rows=1024, batch_bytes=64*1024*1024, cache_rows=10000,
                 cache_bytes=16*1024*1024, cache_entries=1024, replay_window_ns=None,
                 replay_cache_rows=0):
        if principal.owner_scope != grant.owner_scope:
            raise GatewayError('DATA_RESTRICTED', '运行所有者与认证身份不一致')
        if (not 1 <= batch_rows <= min(10000, store.limits.query_rows)
                or not 16384 < batch_bytes <= 64*1024*1024
                or not 1 <= cache_rows <= 100000 or not 16384 < cache_bytes <= 64*1024*1024
                or type(cache_entries) is not int or not 1 <= cache_entries <= 10000
                or replay_window_ns is not None and (type(replay_window_ns) is not int or not 1 <= replay_window_ns <= 366*86400*10**9)
                or type(replay_cache_rows) is not int or not 0 <= replay_cache_rows <= 2*batch_rows
                or not 1 <= len(bindings) <= 64 or len({b.name for b in bindings}) != len(bindings)):
            raise GatewayError('RESOURCE_LIMIT', '数据网关预算无效')
        self.store, self.grant = store, grant
        self.bindings = {b.name: b for b in bindings}
        self.cancelled = cancelled or (lambda: False)
        self.batch_rows, self.batch_bytes = batch_rows, batch_bytes
        self.cache_rows, self.cache_bytes = cache_rows, cache_bytes
        self.cache_entries = cache_entries
        self.replay_window_ns = replay_window_ns
        self.replay_cache_rows = replay_cache_rows
        self.replay_cache = None
        self.context = None
        self.cache = {}
        self.stats = dict(storage_reads=0, storage_rows=0, cache_rows=0,
                          selected_file_bytes=0, files_read=0, storage_scratch_peak_bytes=0)

    def _active(self, context):
        if context is not self.context or context is None or context.closed:
            raise GatewayError('INVALID_CONTRACT', '运行读取上下文已关闭或不属于本运行')
        if self.cancelled():
            raise GatewayError('CANCELLED', '数据读取已取消')

    def _capture(self, names, connection, checkpoint):
        checkpoint(query=True)
        with Session(bind=connection) as session:
            require_ready(session)
        result = {}
        for name in sorted(names):
            checkpoint(query=True)
            binding = self.bindings[name]
            previous = self.context.dependencies.get(name) if self.context else None
            try:
                state = self.store.catalog.dataset(binding.spec.name, connection)
            except DataStoreError as error:
                if previous is not None and error.code == 'DATASET_MISSING':
                    raise DataStoreError('DATA_CHANGED') from None
                raise
            if previous is not None and (previous['generation'] != str(state['generation'])
                    or state['schema_id'] != binding.spec.schema_id or state['rule'] != binding.spec.rule):
                raise DataStoreError('DATA_CHANGED')
            self.store._spec_current(binding.spec, state)
            checkpoint(query=True)
            legacy = connection.execute(text('SELECT EXISTS(SELECT 1 FROM data_store_legacy_restrictions '
                    'WHERE dataset=:d OR dataset IS NULL)'), {'d': binding.spec.name}).scalar_one()
            if legacy:
                if self.context is not None and name in self.context.dependencies:
                    raise DataStoreError('DATA_CHANGED')
                raise DataStoreError('DATA_RESTRICTED')
            # Conservative dataset-wide readability token; no permanent row ledger.
            issue_digest = hashlib.sha256()
            issue_bytes = 0
            checkpoint(query=True)
            issues = connection.execution_options(stream_results=True, max_row_buffer=1).execute(
                    text('SELECT issue_key,reason,evidence_token,target_json,resolution_json '
                    'FROM data_store_issues WHERE dataset=:d ORDER BY issue_key LIMIT :n'),
                    {'d': binding.spec.name, 'n': self.store.limits.issue_count+1})
            try:
                for i, issue in enumerate(issues.mappings()):
                    checkpoint()
                    if i >= self.store.limits.issue_count:
                        raise DataStoreError('ISSUE_BUDGET_EXCEEDED')
                    encoded = json.dumps(dict(issue), ensure_ascii=False, sort_keys=True,
                                         separators=(',', ':')).encode()
                    issue_bytes += len(encoded)
                    if issue_bytes > min(self.batch_bytes, self.store.limits.query_bytes):
                        raise DataStoreError('CONTROL_BUDGET_EXCEEDED')
                    issue_digest.update(encoded)
                    issue_digest.update(b'\0')
            finally:
                issues.close()
                connection.execution_options(stream_results=False)
            entry_id = binding.spec.semantics.get('entry_id')
            checkpoint(query=True)
            summary = connection.execute(text('SELECT summary_json FROM data_store_entry_status WHERE entry_id=:e'),
                                         {'e': entry_id}).scalar_one_or_none()
            # Only this part of entry status participates in the formal reader
            # gate. Refresh progress/counters are operational, not quality.
            restriction = json.loads(summary).get('overflow_restriction', {}) if summary else {}
            result[name] = dict(dataset=binding.spec.name, generation=str(state['generation']),
                readability=fingerprint({'schema': state['schema_id'], 'rule': state['rule'],
                    'issues': issue_digest.hexdigest(),
                    'overflow_restriction': restriction}))
        checkpoint()
        return result

    @contextmanager
    def _state(self, names, *, final=False):
        with store_errors(), self.store.locks.read_many([self.bindings[n].spec.name for n in names],
                timeout_ms=self.store.limits.lock_timeout_ms, cancelled=self.cancelled):
            deadline = time.monotonic()+min(5000, self.store.limits.query_timeout_ms)/1000
            # Captures use one coherent snapshot. Finalization uses fresh
            # READ COMMITTED statements AFTER acquiring writer-excluding locks,
            # so waiting for a writer cannot leave a pre-lock stale snapshot.
            isolation = 'READ COMMITTED' if final else 'REPEATABLE READ'
            with self.store.catalog.engine.connect().execution_options(isolation_level=isolation) as connection, connection.begin():
                configured_timeout = None
                def checkpoint(*, query=False):
                    nonlocal configured_timeout
                    if self.cancelled():
                        raise DataStoreError('OPERATION_CANCELLED')
                    remaining = int((deadline-time.monotonic())*1000)
                    if remaining <= 0:
                        raise DataStoreError('QUERY_TIMEOUT')
                    if query:
                        # Round DOWN and reuse only an identical, still smaller
                        # timeout. The deadline/cancellation checks still run at
                        # every checkpoint; no query gains extra wall time.
                        bounded_timeout = max(1, remaining//10*10)
                        if bounded_timeout != configured_timeout:
                            connection.execute(text("SELECT set_config('statement_timeout',:v,true)"),
                                               {'v': str(bounded_timeout)})
                            configured_timeout = bounded_timeout
                checkpoint(query=True)
                if final:
                    # Also coordinates quality-only/catalog updates, even writers
                    # which do not acquire the filesystem commit lock. Short final
                    # transaction ONLY; never includes network/Arrow/strategy work.
                    connection.execute(text("SET LOCAL lock_timeout='500ms'"))
                    connection.execute(text('LOCK TABLE data_store_datasets,data_store_issues,'
                        'data_store_entry_status,data_store_legacy_restrictions,'
                        'data_store_legacy_maintenance IN SHARE MODE'))
                states = self._capture(names, connection, checkpoint)
                checkpoint(query=True)
                yield states, connection
                checkpoint()

    def open(self, scope, *, dependencies=()):
        if self.context is not None or scope != dict(run_id=self.grant.run_id, universe=list(self.grant.universe)):
            raise GatewayError('DATA_RESTRICTED', '运行请求超出预先授权范围')
        self.context = DependencyContext(uuid4().hex)
        try:
            with self._state(()) as _:
                pass
            for name in dependencies:
                self.declare(name, self.context)
            return self.context
        except BaseException:
            self.close(self.context)
            raise

    def declare(self, name, context):
        self._active(context)
        if type(name) is not str or not name or len(name.encode()) > 128:
            raise GatewayError('INVALID_CONTRACT', '数据绑定标识无效')
        binding = self.bindings.get(name)
        if binding is None:
            raise GatewayError('CAPABILITY_UNAVAILABLE', '所需频率、字段或口径尚未具备正式读取能力')
        if binding.unavailable_reason:
            error = GatewayError('CAPABILITY_UNAVAILABLE', '正式数据尚不满足所需读取能力，请核对入口口径缺口')
            error.scope.update(binding=name, capability_gap=binding.unavailable_reason)
            raise error
        names = set(context.dependencies) | {name}
        with self._state(names) as (states, _):
            if any(states[n] != old for n, old in context.dependencies.items()):
                raise GatewayError('DATA_CHANGED', '已使用数据或质量状态发生变化')
            context.dependencies[name] = states[name]

    def check(self, context):
        self._active(context)
        with self._state(context.dependencies) as (states, _):
            if states != context.dependencies:
                self.cache.clear()
                raise GatewayError('DATA_CHANGED', '数据或质量状态发生变化，请重新运行')
        return 'unchanged'

    def finalize(self, context, commit: Callable):
        """D13 submits the authoritative result using THIS connection, no I/O."""
        self._active(context)
        with self._state(context.dependencies, final=True) as (states, connection):
            if states != context.dependencies:
                raise GatewayError('DATA_CHANGED', '成功提交前数据或质量状态发生变化')
            return commit(connection)

    def _request(self, name, request, context):
        self._active(context)
        if type(name) is not str or not name or len(name.encode()) > 128:
            raise GatewayError('INVALID_CONTRACT', '数据绑定标识无效')
        if name not in context.dependencies:
            self.declare(name, context)  # recheck old dependencies before adding any
        # This method validates shape/permission. read()/lookback() check before
        # and after exposing buffers, and _pages() checks around EACH physical
        # read. Re-validating an existing binding at every nested call does not
        # require another identical SQL transaction before any work occurs.
        binding = self.bindings[name]
        if (type(request) is not dict or set(request) != {'securities','fields','frequency','start_ns',
                'end_ns','count_per_security','adjustment'} or type(request['securities']) is not list
                or type(request['fields']) is not list or len(request['securities']) > 10000
                or any(type(s) is not str for s in request['securities'])
                or any(type(f) is not str for f in request['fields'])
                or len(set(request['securities'])) != len(request['securities'])
                or not set(request['securities']) <= set(self.grant.universe)
                or not request['fields'] or len(request['fields']) > 128
                or len(set(request['fields'])) != len(request['fields'])):
            raise GatewayError('DATA_RESTRICTED', '数据请求字段或标的超出本运行范围')
        if (request['frequency'] != binding.frequency or request['adjustment'] != 'none'
                or not set(request['fields']) <= PUBLIC_FIELDS & set(binding.fields)):
            raise GatewayError('CAPABILITY_UNAVAILABLE', '请求频率、字段或复权方式不受支持')
        end = _ns(request['end_ns'])
        count = request['count_per_security']
        if (request['start_ns'] is None) == (count is None):
            raise GatewayError('INVALID_CONTRACT', 'start 与 count 必须二选一')
        if count is not None and (type(count) is not int or not 1 <= count <= self.cache_rows):
            raise GatewayError('RESOURCE_LIMIT', '历史窗口超过行数预算')
        start = self.grant.start_ns if count is not None else _ns(request['start_ns'])
        if not self.grant.start_ns <= start <= end <= self.grant.end_ns:
            raise GatewayError('DATA_RESTRICTED', '时间范围超出运行授权窗口')
        return binding, start, end, count

    def _pages(self, binding, security, start, end, context, fields, *, count=None, page_rows=None):
        securities = security if type(security) is tuple else (security,)
        self.check(context)
        cached=self.replay_cache
        if cached is not None and cached[0]==binding.name and set(fields)<=cached[1]:
            table=cached[2]
            # Strict interior bounds exclude partially buffered same-ns groups.
            # The buffer always originated from an admitted full-universe read.
            if table.num_rows and int(table['time_ns'][0].as_py())<start<=end<int(table['time_ns'][-1].as_py()):
                mask=pc.and_(pc.and_(pc.greater_equal(table['time_ns'],start),pc.less_equal(table['time_ns'],end)),
                    pc.is_in(table['security'],value_set=pa.array(securities,type=pa.string())))
                table=table.filter(mask)
                wanted=set(fields)|IDENTITY_FIELDS
                table=pa.Table.from_arrays([table[f.name] if f.name in wanted else pa.nulls(table.num_rows,f.type)
                    for f in MARKET_SCHEMA],schema=MARKET_SCHEMA)
                rows=min(page_rows or self.batch_rows,self.batch_rows)
                for offset in range(0,max(1,table.num_rows),rows):
                    self.check(context)
                    yield self._batch(table.slice(offset,rows),binding)
                self.check(context)
                return
        bounds = [binding.bounds(s, start, end) for s in securities]
        lower = min((b[0] for b in bounds), key=binding.spec.key_bytes)
        upper = max((b[1] for b in bounds), key=binding.spec.key_bytes)
        lo, hi = binding.spec.key_bytes(lower), binding.spec.key_bytes(upper)
        with store_errors(), self.store.catalog.transaction() as c:
            partitions = c.execute(text('SELECT DISTINCT partition_key FROM data_store_files WHERE dataset=:d '
                'AND key_min<:hi AND key_max>=:lo ORDER BY partition_key LIMIT :n'),
                {'d': binding.spec.name, 'lo': lo, 'hi': hi, 'n': self.store.limits.query_partitions+1}).scalars().all()
        if len(partitions) > self.store.limits.query_partitions:
            raise GatewayError('RESOURCE_LIMIT', '所请求分区超过读取预算')
        if not partitions:
            # Match the existing formal reader's empty-range policy: absence
            # of files cannot hide unresolved quality/overflow restrictions.
            with store_errors():
                descriptor = self.store.describe_capability(binding.spec)
                if descriptor['issues']:
                    raise DataStoreError('DATA_RESTRICTED')
            partitions = ['default']
            with store_errors(), self.store.catalog.transaction() as c:
                summary = c.execute(text('SELECT summary_json FROM data_store_entry_status WHERE entry_id=:e'),
                    {'e': binding.spec.semantics.get('entry_id')}).scalar_one_or_none()
            if summary:
                overflow = json.loads(summary).get('overflow_restriction', {})
                if overflow.get('blocking_objects') and overflow.get('partition'):
                    # Feed the declared restriction scope into the ORIGINAL
                    # relevant_issue_count gate, including when no files exist.
                    partitions = list(dict.fromkeys([*partitions, overflow['partition']]))
        identity = getattr(binding, 'identity_fields', IDENTITY_FIELDS)
        columns = tuple(dict.fromkeys([binding.fields[f] for f in (*sorted(identity), *fields)
                    if f in binding.fields and binding.fields[f] in binding.spec.schema.names]
                    +[binding.time_column]+[c for c, _ in binding.guards]+list(binding.extra_columns)))
        rows = min(page_rows or self.batch_rows, self.batch_rows,
                   count*len(securities) if count is not None else self.batch_rows)
        if count is not None and len(securities)>1 and page_rows is None:
            # Materialize one admitted bounded cross-section in the parent's
            # window cache, then read() slices it to the worker's row credit.
            # The grouped ranking is not repeated in N single-symbol pages.
            rows = min(count*len(securities), self.store.limits.query_rows)
        # Contract worst-case materialization, before any strings/Arrow allocation.
        rows = binding.spec.bounded_rows(rows, min(self.batch_bytes//4, self.store.limits.query_bytes))
        security_column = binding.fields.get('security')
        if len(securities) > 1 and security_column not in binding.spec.schema.names:
            raise GatewayError('CAPABILITY_UNAVAILABLE', '此身份投影尚不支持集合批量读取')
        # Time-first keys cannot delimit one security by physical bounds alone.
        storage_securities = getattr(binding, 'storage_securities', None)
        stored_securities = storage_securities(securities) if storage_securities else securities
        if (type(stored_securities) is not tuple or len(stored_securities) != len(securities)
                or len(set(stored_securities)) != len(stored_securities)
                or any(type(s) is not str or not s for s in stored_securities)):
            raise GatewayError('INVALID_CONTRACT', '存储身份映射缺失、重复或无效')
        membership = ((security_column, 'in', stored_securities),) if security_column in binding.spec.schema.names else ()
        group_order = []
        if count is not None:
            for field in (() if binding.count_order is not None else
                          ('time_ns', 'source_session', 'channel', 'sequence', 'stable_input_sequence')):
                if field in binding.constants:
                    continue
                column = binding.fields.get(field)
                if column not in binding.spec.schema.names:
                    raise GatewayError('CAPABILITY_UNAVAILABLE', '计数窗口缺少可验证的完整事件排序投影')
                group_order.append(column)
            if binding.count_order is not None:
                group_order.extend(binding.count_order)
            if security_column not in binding.spec.schema.names:
                raise GatewayError('CAPABILITY_UNAVAILABLE', '计数窗口缺少可验证的标的集合投影')
        query_time_bounds = getattr(binding, 'query_time_bounds', None)
        time_lower, time_upper = (query_time_bounds(start, end)
            if query_time_bounds else
            (start-binding.time_offset_ns, end-binding.time_offset_ns))
        query = Query(partitions=tuple(partitions), lower=lower, upper=upper, columns=columns,
            page_size=rows, descending=count is not None,
            filters=((binding.time_column, '>=', time_lower),
                     (binding.time_column, '<=', time_upper), *binding.selectors, *membership),
            per_group=(security_column, count) if count is not None else None,
            group_order=tuple(dict.fromkeys(group_order)))
        remaining = count if len(securities) == 1 else None
        while True:
            self.check(context)
            with store_errors():
                page = self.store.read_arrow(binding.spec, query,
                    expected_generation=int(context.dependencies[binding.name]['generation']), cancelled=self.cancelled)
            self.stats['storage_reads'] += 1
            self.stats['storage_rows'] += page.table.num_rows
            self.stats['selected_file_bytes'] += page.metrics.current_read_bytes
            self.stats['files_read'] += page.metrics.files_read
            self.stats['storage_scratch_peak_bytes'] = max(
                self.stats['storage_scratch_peak_bytes'], page.metrics.scratch_peak_bytes)
            self.check(context)  # quality may change without generation during read
            with store_errors():
                table = binding.project(page.table, set(fields) | identity)
            if table.num_rows and (table['security'].null_count or not pc.all(pc.is_in(table['security'],
                    value_set=pa.array(securities, type=pa.string()))).as_py()):
                raise GatewayError('INVALID_CONTRACT', '存储身份与请求标的不一致')
            if remaining is not None:
                table = table.slice(0, remaining)
                remaining -= table.num_rows
            if table.num_rows or not page.next_cursor:
                result=self._batch(table, binding)
                if (self.replay_cache_rows and count is None and start<end
                        and set(securities)==set(self.grant.universe) and not hasattr(binding,'batch_metadata')):
                    self._remember_replay(binding.name,set(fields),result.table)
                yield result
            if not page.next_cursor or remaining == 0:
                break
            query = replace(query, cursor=page.next_cursor)
        self.check(context)

    def _remember_replay(self, name, fields, table):
        if not table.num_rows:
            return
        cached=self.replay_cache
        identity=('time_ns','security','source_session','channel','sequence','stable_input_sequence')
        if (cached is not None and cached[0]==name and cached[1]==fields
                and tuple(cached[2][k][-1].as_py() for k in identity)<tuple(table[k][0].as_py() for k in identity)):
            table=pa.concat_tables([cached[2],table])
        if table.num_rows>self.replay_cache_rows:
            table=table.take(pa.array(range(table.num_rows-self.replay_cache_rows,table.num_rows),type=pa.uint64()))
        table=table.combine_chunks()
        if table.nbytes>2*self.batch_bytes:
            raise GatewayError('RESOURCE_LIMIT','回放缓冲超过两批次预算')
        self.replay_cache=(name,fields,table)

    def _batch(self, table, binding):
        if table.nbytes+16384 > self.batch_bytes:
            raise GatewayError('RESOURCE_LIMIT', 'Arrow 批次超过内存预算')
        if hasattr(binding, 'batch_metadata'):
            return DataBatch(table, binding.batch_metadata(table))
        if table.num_rows:
            table = table.take(pc.sort_indices(table, sort_keys=[('time_ns', 'ascending'),
                ('security', 'ascending'), ('source_session', 'ascending'), ('channel', 'ascending'),
                ('sequence', 'ascending'), ('stable_input_sequence', 'ascending')]))
        times = table['time_ns']
        first = pc.min(times).as_py() if table.num_rows else None
        last = pc.max(times).as_py() if table.num_rows else None
        securities = pc.unique(table['security']).to_pylist()
        return DataBatch(table, dict(schema_id=SCHEMA_ID, time_unit='utc_nanoseconds',
            quantity_unit='shares', price_currency='CNY', rows=table.num_rows,
            actual_scope=dict(start_ns=None if first is None else str(first),
                end_ns=None if last is None else str(last), securities=securities),
            limitations=list(binding.limitations)))

    def read(self, name, request, context, *, page_rows=None):
        binding, start, end, count = self._request(name, request, context)
        if count is None:
            self.check(context)
        if count is None and start < end and len(request['securities']) > 1:
            # Single-page sorting cannot repair cross-page replay order. Bounded
            # count windows and a research query at one exact ns still support
            # existing security-first layouts. The latter is collected/clipped
            # by ReadView and does not advance a multi-interval replay clock.
            ordered = tuple(dict.fromkeys(binding.fields[field] for field in
                ('time_ns','security','source_session','channel','sequence','stable_input_sequence')
                if field not in binding.constants and field in binding.fields))
            if tuple(binding.spec.key[:len(ordered)]) != ordered:
                raise GatewayError('CAPABILITY_UNAVAILABLE', '多标的事件源缺少可验证的跨页时间顺序；不能用单页排序冒充有序回放')
        if count is not None:
            window = self.lookback(name, request, context)
            rows = min(page_rows or self.batch_rows, self.batch_rows)
            if not window.table.num_rows:
                yield window
            else:
                for offset in range(0, window.table.num_rows, rows):
                    self.check(context)
                    yield self._batch(window.table.slice(offset, rows), binding)
            self.check(context)
            return
        if not request['securities']:
            yield self._batch(pa.Table.from_batches([], schema=MARKET_SCHEMA), binding)
        if request['securities']:
            security = tuple(sorted(request['securities'])) if len(request['securities']) > 1 else request['securities'][0]
            if (self.replay_window_ns is not None and count is None and start < end
                    and binding.spec.key[0] == binding.fields.get('time_ns')
                    and pa.types.is_int64(binding.spec.schema.field(binding.spec.key[0]).type)):
                # Time-first replay uses finite ranges through the SAME reader,
                # locks, issues and generation gate. It does not repeatedly sort
                # every future file to produce one page. File bounds are hints,
                # never coverage or permission evidence.
                with store_errors(), self.store.catalog.transaction() as c:
                    bounds = c.execute(text('SELECT '
                        '(SELECT key_min FROM data_store_files WHERE dataset=:d ORDER BY key_min LIMIT 1),'
                        '(SELECT key_max FROM data_store_files WHERE dataset=:d ORDER BY key_max DESC LIMIT 1)'),
                        {'d':binding.spec.name}).one()
                if bounds[0] is not None:
                    first = int.from_bytes(bytes(bounds[0])[:8],'big')-2**63+binding.time_offset_ns
                    last = int.from_bytes(bytes(bounds[1])[:8],'big')-2**63+binding.time_offset_ns
                    begin, finish = max(start,first), min(end,last)
                    if begin <= finish:
                        while begin <= finish:
                            stop = min(finish,begin+self.replay_window_ns-1)
                            yield from self._pages(binding, security, begin, stop, context, request['fields'],
                                                   count=count, page_rows=page_rows)
                            begin = stop+1
                    else:
                        yield from self._pages(binding,security,start,end,context,request['fields'],page_rows=page_rows)
                else:
                    yield from self._pages(binding,security,start,end,context,request['fields'],page_rows=page_rows)
            else:
                yield from self._pages(binding, security, start, end, context, request['fields'],
                                       count=count, page_rows=page_rows)
        self.check(context)

    def lookback(self, name, request, context):
        """Bounded count window; same query reuses buffers, forward end reads delta."""
        binding, start, end, count = self._request(name, request, context)
        self.check(context)
        if count is None or count*len(request['securities']) > self.cache_rows:
            raise GatewayError('RESOURCE_LIMIT', '历史窗口超过缓存预算')
        parts = []
        groups = ([tuple(sorted(request['securities']))] if len(request['securities']) > 1 else request['securities'])
        for security in groups:
            key = (name, security, tuple(request['fields']))
            cached = self.cache.get(key)
            if cached is None and len(self.cache) >= self.cache_entries:
                self.cache.clear()
                raise GatewayError('RESOURCE_LIMIT', '历史窗口缓存条目超过预算')
            lower = start
            if cached is not None and cached[0] <= end and cached[2] >= count:
                previous_end, previous, _ = cached
                lower = previous_end+1
            else:
                previous = pa.Table.from_batches([], schema=MARKET_SCHEMA)
            joined = previous
            def compact(table):
                indices = pc.sort_indices(table, sort_keys=[('security','ascending'),('time_ns','ascending'),
                    ('source_session','ascending'),('channel','ascending'),('sequence','ascending'),
                    ('stable_input_sequence','ascending')])
                if type(security) is tuple:
                    # The admitted cache plus one bounded page is the entire
                    # input. Arrow identifies contiguous security runs; NumPy
                    # selects their tails without one Python object/scalar per
                    # historical event. Retain the same full identity ordering,
                    # including equal-ns events and shorter/missing groups.
                    if not len(indices):
                        return table.combine_chunks()
                    ends = pc.run_end_encode(table['security'].take(indices).combine_chunks()).run_ends.to_numpy()
                    starts = np.concatenate((np.array([0], dtype=ends.dtype), ends[:-1]))
                    lower = np.maximum(starts, ends-count)
                    keep = np.arange(len(indices)) >= np.repeat(lower, ends-starts)
                    indices = pa.array(indices.to_numpy()[keep], type=pa.uint64())
                else:
                    indices = indices.slice(max(0, len(indices)-count))
                return table.take(indices).combine_chunks()
            if lower <= end:
                # Compact/check each page rather than retaining an entire
                # lookback before finding out that its byte budget was exceeded.
                with closing(self._pages(binding, security, lower, end,
                        context, request['fields'], count=count)) as pages:
                    for batch in pages:
                        joined = compact(pa.concat_tables([joined, batch.table]))
                        other_bytes = sum(t.nbytes for k, (_, t, _) in self.cache.items() if k != key)
                        other_rows = sum(t.num_rows for k, (_, t, _) in self.cache.items() if k != key)
                        if (other_bytes+joined.nbytes > self.cache_bytes
                                or other_rows+joined.num_rows > self.cache_rows):
                            self.cache.clear()
                            raise GatewayError('RESOURCE_LIMIT', '历史窗口超过缓存内存预算')
            elif joined.num_rows > count:
                joined = compact(joined)
            self.cache[key] = (end, joined, count)
            parts.append(joined)
        if (sum(t.num_rows for _, t, _ in self.cache.values()) > self.cache_rows
                or sum(t.nbytes for _, t, _ in self.cache.values()) > self.cache_bytes):
            self.cache.clear()
            raise GatewayError('RESOURCE_LIMIT', '历史窗口超过缓存内存预算')
        self.stats['cache_rows'] = sum(t.num_rows for _, t, _ in self.cache.values())
        self.check(context)
        return self._batch(pa.concat_tables(parts) if parts else pa.Table.from_batches([], schema=MARKET_SCHEMA), binding)

    def close(self, context):
        self.replay_cache=None
        if context is self.context and context is not None:
            context.closed = True
            context.dependencies.clear()
            self.cache.clear()


def serve(channel, gateway, context, *, close_context=True, session_facts=None):
    """Run-private source/query streams; one credited response at a time.

    At most two generators retain buffers, with each emitted batch bounded by
    batch_rows/batch_bytes and the unchanged aggregate window-cache budget. The channel
    never queues Arrow responses or reads either stream without explicit credit.
    D12 must include these two stream allocations in its parent memory grant.
    """
    Frame, TransportError = channel.frame_type, channel.error_type
    streams = {}
    last_id = 0
    original_cancelled = gateway.cancelled
    gateway.cancelled = lambda: (original_cancelled() or channel.cancelled()
                                or channel.is_disconnected())
    try:
        while True:
            frame = channel.receive()
            if (frame.run_id != gateway.grant.run_id or frame.status != 'request'
                    or frame.payload or frame.request_id <= last_id):
                raise TransportError()
            last_id = frame.request_id
            try:
                body, payload, status = {}, b'', 'ok'
                if frame.op == 'open_stream':
                    if set(frame.body) != {'binding','request','max_rows'}:
                        raise GatewayError('INVALID_CONTRACT', '数据流参数无效')
                    max_rows = frame.body['max_rows']
                    if (type(max_rows) is not int or not 1 <= max_rows <= gateway.batch_rows
                            or len(streams) >= MAX_ACTIVE_STREAMS
                            or sum(credit for _, credit in streams.values())+max_rows
                                > MAX_ACTIVE_STREAMS*gateway.batch_rows):
                        raise GatewayError('RESOURCE_LIMIT', '流信用超过预算')
                    name = frame.body['binding']
                    gateway._request(name, frame.body['request'], context)
                    stream_id = uuid4().hex
                    streams[stream_id] = (gateway.read(name, frame.body['request'], context,
                                                     page_rows=max_rows), max_rows)
                    body = dict(stream_id=stream_id)
                elif frame.op in {'next', 'close_stream'}:
                    if set(frame.body) != {'stream_id'} or frame.body['stream_id'] not in streams:
                        raise GatewayError('INVALID_CONTRACT', '数据流不属于本运行')
                    stream_id = frame.body['stream_id']
                    if frame.op == 'close_stream':
                        streams.pop(stream_id)[0].close()
                    else:
                        try:
                            batch = next(streams[stream_id][0])
                            body, payload, status = batch.metadata, batch.ipc(gateway.batch_bytes), 'batch'
                            del batch
                        except StopIteration:
                            streams.pop(stream_id)[0].close()
                            gateway.check(context)
                            status = 'eof'
                elif frame.op == 'session_facts':
                    if (session_facts is None or set(frame.body) != {'session', 'offset'}
                            or type(frame.body['session']) is not str
                            or not 1 <= len(frame.body['session'].encode()) <= 128
                            or type(frame.body['offset']) is not int or not 0 <= frame.body['offset'] < 10000):
                        raise GatewayError('INVALID_CONTRACT', '会话事实请求无效')
                    gateway.check(context)
                    body = session_facts(frame.body['session'], frame.body['offset'], 256)
                    if (type(body) is not dict or set(body) != {'session', 'facts', 'next_offset'}
                            or body['session'] != frame.body['session'] or type(body['facts']) is not list
                            or not 1 <= len(body['facts']) <= 256
                            or body['next_offset'] is not None and body['next_offset'] != frame.body['offset'] + len(body['facts'])):
                        raise GatewayError('INVALID_CONTRACT', '会话事实信用或身份无效')
                    gateway.check(context)
                elif frame.op == 'check':
                    if frame.body:
                        raise GatewayError('INVALID_CONTRACT', '依赖检查不接受额外控制')
                    gateway.check(context)
                    body = dict(check='unchanged', context=context.to_dict(),
                                limits=dict(batch_rows=gateway.batch_rows, batch_bytes=gateway.batch_bytes,
                                    max_active_streams=MAX_ACTIVE_STREAMS,
                                    stream_row_credits=MAX_ACTIVE_STREAMS*gateway.batch_rows,
                                    stream_batch_bytes=MAX_ACTIVE_STREAMS*gateway.batch_bytes))
                elif frame.op in {'cancel','close'}:
                    if frame.body:
                        raise GatewayError('INVALID_CONTRACT', '关闭不接受额外控制')
                else:
                    raise GatewayError('INVALID_CONTRACT', '未知 IPC 操作')
                channel.send(Frame(frame.run_id, frame.request_id, frame.op, status, body, payload))
                if frame.op in {'cancel','close'}:
                    break
            except GatewayError as error:
                channel.send(Frame(frame.run_id, frame.request_id, frame.op, 'error', error.to_dict()))
                break  # errors are terminal; cannot ignore changes and keep reading
    finally:
        for stream, _ in streams.values():
            stream.close()
        if close_context:
            gateway.close(context)
        channel.close()
        gateway.cancelled = original_cancelled
