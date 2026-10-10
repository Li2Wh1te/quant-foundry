"""Bounded current reads and signed, request-bound change-detection cursors.

Only explicit catalog paths reach private DuckDB connections. Multi-dependency
requests acquire all read locks in the same order and construct every response
before releasing any lock. No historical generation is ever read or retained.
"""
from __future__ import annotations

import base64
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import hashlib
import hmac
import json
from typing import Any

import pyarrow as pa
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import text

from .budget import Metrics
from .errors import DataStoreError
from .schema import DatasetSpec, api_value, fingerprint, identifier
from .values import control_json


@dataclass(frozen=True)
class Query:
    partitions: tuple[str, ...] = ('default',)
    lower: tuple | None = None
    upper: tuple | None = None
    columns: tuple[str, ...] = ()
    page_size: int = 1000
    cursor: str | None = None
    release: str | None = None
    snapshot: str | None = None
    require_qualified: bool = True
    descending: bool = False
    filters: tuple[tuple[str, str, Any], ...] = ()

    def __post_init__(self):
        if self.release is not None or self.snapshot is not None:
            raise DataStoreError('HISTORY_UNSUPPORTED')
        if (type(self.partitions) not in (tuple,list) or not 1 <= len(self.partitions) <= 64
                or type(self.columns) not in (tuple,list) or len(self.columns) > 128
                or not self.partitions or len(set(self.partitions)) != len(self.partitions)
                or type(self.require_qualified) is not bool
                or type(self.descending) is not bool
                or type(self.filters) is not tuple or len(self.filters) > 16
                or type(self.page_size) is not int or self.page_size < 1
                or len(set(self.columns)) != len(self.columns)):
            raise DataStoreError('INVALID_VALUE')
        object.__setattr__(self, 'partitions', tuple(self.partitions))
        object.__setattr__(self, 'columns', tuple(self.columns))
        for value in (self.lower,self.upper):
            if value is not None and (type(value) is not tuple or not 1 <= len(value) <= 128):
                raise DataStoreError('INVALID_VALUE')
        for p in self.partitions:
            identifier(p)
        for item in self.filters:
            if (type(item) is not tuple or len(item) != 3 or type(item[0]) is not str
                    or item[1] not in ('=', '>=', '<=', '>', '<')):
                raise DataStoreError('INVALID_VALUE')


@dataclass(frozen=True)
class Page:
    dataset: str
    generation: int
    generations: dict[str, int]
    rows: list[dict[str, Any]]
    next_cursor: str | None
    schema_id: str
    semantics: dict[str, str]
    metrics: Metrics
    quality_status: str = 'available'
    unresolved_issues: int = 0

    def to_dict(self):
        """Exact internal transport contract for D04, not a registered endpoint."""
        return {'dataset': self.dataset, 'generation': self.generation,
                'generations': self.generations, 'rows': self.rows,
                'next_cursor': self.next_cursor, 'schema_id': self.schema_id,
                'semantics': self.semantics, 'quality_status': self.quality_status,
                'unresolved_issues': self.unresolved_issues}


@dataclass(frozen=True)
class ArrowPage:
    """Same admitted query as Page; owned buffers, no pinned source handles."""
    dataset: str
    generation: int
    generations: dict[str, int]
    table: pa.Table
    next_cursor: str | None
    schema_id: str
    semantics: dict[str, str]
    metrics: Metrics
    quality_status: str = 'available'
    unresolved_issues: int = 0


def _request(spec, query):
    request = {'dataset': spec.name, 'schema': spec.schema_id, 'rule': spec.rule,
               'partitions': sorted(query.partitions), 'lower': query.lower,
               'upper': query.upper, 'columns': query.columns, 'page_size': query.page_size,
               'require_qualified': query.require_qualified}
    # Existing default queries retain their signed cursor identity across this
    # additive projection change. New variants remain distinct and bound.
    if query.descending or query.filters:
        request.update(descending=query.descending, filters=query.filters)
    return fingerprint(request)


def _encode(store, request, generations, last):
    body = control_json({'request': request, 'generations': generations,
                         'last': last}, max_bytes=6144).encode()
    signed = body + hmac.digest(store.cursor_key, body, 'sha256')
    return base64.urlsafe_b64encode(signed).decode()


def _decode(store, token, request):
    if type(token) is not str or len(token) > 8192:
        raise DataStoreError('INVALID_CURSOR')
    try:
        raw = base64.b64decode(token, altchars=b'-_', validate=True)
        body, signature = raw[:-32], raw[-32:]
        if not hmac.compare_digest(signature, hmac.digest(store.cursor_key, body, 'sha256')):
            raise ValueError()
        item = json.loads(body)
        if (item['request'] != request or set(item) != {'request', 'generations', 'last'}
                or type(item['last']) is not list or type(item['generations']) is not dict
                or any(type(v) is not int or v < 0 for v in item['generations'].values())):
            raise ValueError()
        return item
    except (ValueError, TypeError, KeyError, RecursionError, UnicodeError):
        raise DataStoreError('INVALID_CURSOR') from None


def _restore(value, t):
    try:
        if pa.types.is_decimal(t):
            return Decimal(value)
        if pa.types.is_date32(t):
            return date.fromisoformat(value)
        if pa.types.is_timestamp(t):
            return datetime.fromisoformat(value)
        if pa.types.is_integer(t):
            if type(value) not in (int, str):
                raise ValueError()
            return int(value)
        return value
    except (TypeError, ValueError):
        raise DataStoreError('INVALID_CURSOR') from None


def _predicate(spec, values, operator):
    """Scalar lexicographic bounds avoid DuckDB struct-BETWEEN rewrites.

    Parameters remain bound values, not rendered literals. The disjunction is
    bounded by the static key length, and never treats a prefix as a timestamp.
    """
    if len(values)>len(spec.key) or not values or operator not in ('>=','>','<','<='):
        raise DataStoreError('INVALID_VALUE')
    strict='>' if operator.startswith('>') else '<'
    terms=[];params=[]
    for index in range(len(values)):
        term=[]
        for earlier in range(index):
            term.append('"'+spec.key[earlier]+'"=?');params.append(values[earlier])
        term.append('"'+spec.key[index]+'" '+(operator if index==len(values)-1 else strict)+' ?')
        params.append(values[index]);terms.append('('+' AND '.join(term)+')')
    return '('+' OR '.join(terms)+')', params


def read_many(store, queries, *, expected_generations=None, cancelled=None, arrow=False):
    bounded_queries = []
    for i, item in enumerate(queries):
        if i >= 16:
            raise DataStoreError('QUERY_BUDGET_EXCEEDED')
        bounded_queries.append(item)
    queries = tuple(bounded_queries)
    if not 1 <= len(queries) <= 16:
        raise DataStoreError('QUERY_BUDGET_EXCEEDED')
    for spec, query in queries:
        if (query.page_size > store.limits.query_rows
                or len(query.partitions) > store.limits.query_partitions
                or any(c not in spec.schema.names for c in query.columns)
                or any(c not in spec.schema.names for c, _, _ in query.filters)):
            raise DataStoreError('QUERY_BUDGET_EXCEEDED')
    metrics = Metrics()
    try:
        with store.budget.reserve('read', cancelled=cancelled, metrics=metrics) as space:
            with store.locks.read_many([s.name for s, _ in queries],
                                      timeout_ms=store.limits.lock_timeout_ms, cancelled=cancelled):
                states = {s.name: store.catalog.dataset(s.name) for s, _ in queries}
                generations = {name: s['generation'] for name, s in states.items()}
                if expected_generations is not None and expected_generations != generations:
                    raise DataStoreError('DATA_CHANGED')
                pages, output_bytes = [], 0
                for spec, query in queries:
                    from .issue_scope import relevant_issue_count
                    issue_count = relevant_issue_count(store, spec, query, space.check)
                    if issue_count and query.require_qualified:
                        raise DataStoreError('DATA_RESTRICTED')
                    store._spec_current(spec, states[spec.name])
                    request, last = _request(spec, query), None
                    if query.cursor:
                        cursor = _decode(store, query.cursor, request)
                        if cursor['generations'] != generations:
                            raise DataStoreError('DATA_CHANGED')
                        if len(cursor['last']) != len(spec.key):
                            raise DataStoreError('INVALID_CURSOR')
                        last = tuple(_restore(v, spec.schema.field(k).type)
                                     for k, v in zip(spec.key, cursor['last']))
                        spec.key_bytes(last)
                    lower = spec.key_bytes(query.lower) if query.lower is not None else None
                    upper = spec.key_bytes(query.upper) if query.upper is not None else None
                    if last is not None and not query.descending:
                        lower = max(lower or b'', spec.key_bytes(last))
                    elif last is not None:
                        upper = min(upper or b'\xff'*2048, spec.key_bytes(last))
                    if lower is not None and upper is not None and lower >= upper:
                        raise DataStoreError('INVALID_VALUE')
                    refs = []
                    for p in query.partitions:
                        refs.extend(store.catalog.files(spec.name, p, lower, upper,
                                                        cap=store.limits.query_files))
                        if len(refs) > store.limits.query_files:
                            raise DataStoreError('QUERY_BUDGET_EXCEEDED')
                    if sum(f['byte_count'] for f in refs) > store.limits.query_scan_bytes:
                        raise DataStoreError('QUERY_BUDGET_EXCEEDED')
                    columns = query.columns or tuple(spec.schema.names)
                    selected = tuple(dict.fromkeys((*columns, *spec.key)))
                    rows, next_cursor, arrow_parts, row_count = [], None, [], 0
                    with ExitStack() as stack:
                        paths = store._pin_files(spec, refs, stack, space)
                        con = stack.enter_context(space.connection())
                        con.register('_layout', pa.Table.from_batches([], schema=spec.schema))
                        if paths:
                            con.read_parquet(paths, hive_partitioning=False, union_by_name=True).create_view('raw')
                            con.execute('CREATE TEMP VIEW current_data AS '
                                        'SELECT * FROM raw UNION ALL BY NAME SELECT * FROM _layout')
                        else:
                            con.execute('CREATE TEMP VIEW current_data AS SELECT * FROM _layout')
                        where, params = [], []
                        if spec.semantics.get('tombstones') == 'explicit-current-state' and query.require_qualified:
                            where.append("basis_state='valid'")
                        for values, operator in ((query.lower, '>='), (query.upper, '<'),
                                                 (last, '<' if query.descending else '>')):
                            if values is not None:
                                spec.key_bytes(values)
                                predicate, bound_values = _predicate(spec, values, operator)
                                where.append(predicate)
                                params.extend(bound_values)
                        for column, operator, value in query.filters:
                            where.append('"'+column+'" '+operator+' ?')
                            params.append(value)
                        fields = ','.join('"'+f+'"' for f in selected)
                        order = ','.join('"'+k+'"'+(' DESC' if query.descending else '') for k in spec.key)
                        sql = 'SELECT ' + fields + ' FROM current_data'
                        if where:
                            sql += ' WHERE ' + ' AND '.join(where)
                        sql += ' ORDER BY ' + order + ' LIMIT ?'
                        params.append(query.page_size+1)
                        reader = con.execute(sql, params).fetch_record_batch(
                            spec.bounded_rows(min(1024, query.page_size+1), store.limits.query_bytes))
                        raw_last = None
                        for batch in reader:
                            space.check()
                            if batch.nbytes > store.limits.query_bytes:
                                raise DataStoreError('QUERY_BUDGET_EXCEEDED')
                            if arrow:
                                take = min(batch.num_rows, query.page_size-row_count)
                                if take:
                                    part = batch.slice(0, take).select(columns)
                                    output_bytes += part.nbytes
                                    if output_bytes > store.limits.query_bytes:
                                        raise DataStoreError('QUERY_BUDGET_EXCEEDED')
                                    arrow_parts.append(part)
                                    row_count += take
                                    raw_last = tuple(batch.column(batch.schema.get_field_index(k))[take-1].as_py()
                                                     for k in spec.key)
                                if take < batch.num_rows:
                                    next_cursor = _encode(store, request, generations, raw_last)
                                    break
                                continue
                            for row in batch.to_pylist():
                                if len(rows) == query.page_size:
                                    next_cursor = _encode(store, request, generations, raw_last)
                                    break
                                raw_last = tuple(row[k] for k in spec.key)
                                encoded = {k: api_value(row[k]) for k in columns}
                                output_bytes += len(control_json(encoded, max_bytes=store.limits.query_bytes).encode())
                                if output_bytes > store.limits.query_bytes:
                                    raise DataStoreError('QUERY_BUDGET_EXCEEDED')
                                rows.append(encoded)
                            if next_cursor:
                                break
                    space.check()
                    if arrow:
                        schema = pa.schema([spec.schema.field(c) for c in columns], metadata=spec.schema.metadata)
                        # DuckDB returns its physical schema without field metadata.
                        table = pa.Table.from_batches(arrow_parts) if arrow_parts else pa.Table.from_batches([], schema=schema)
                        table = table.cast(schema, safe=True)
                        pages.append(ArrowPage(spec.name, generations[spec.name], dict(generations), table,
                                               next_cursor, spec.schema_id, dict(spec.semantics), metrics,
                                               'restricted' if issue_count else 'available', issue_count))
                        continue
                    pages.append(Page(spec.name, generations[spec.name], dict(generations), rows,
                                      next_cursor, spec.schema_id, dict(spec.semantics), metrics,
                                      'restricted' if issue_count else 'available', issue_count))
                # Response construction/serialization remains inside ALL read locks.
                if not arrow:
                    for page in pages:
                        json.dumps(page.to_dict(), ensure_ascii=False, separators=(',', ':'), allow_nan=False)
                return pages
    except SQLAlchemyError:
        raise DataStoreError('CATALOG_UNAVAILABLE') from None
    except OSError:
        raise DataStoreError('FILE_INVALID') from None
