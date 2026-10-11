"""Retained legacy results: authorization and bounded reads only."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping
from uuid import UUID
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session
from .storage_models import BacktestRunRecord
from .result_records import *
from .pagination import *
from .pagination import hmac_compare

class ResultRepositoryError(ValueError):
    """Base class for repository usage errors."""

class UnknownResultKindError(ResultRepositoryError):
    """The requested result kind is not registered."""

class ResultFilterError(ResultRepositoryError):
    """A filter is not supported by the requested result kind."""

class InternalResultNotVisibleError(ResultFilterError):
    """A Phase 2a internal run was addressed through a formal read path."""
    code = 'internal_result_not_visible'

class IndeterminateResultNotVisibleError(ResultFilterError):
    """A formal run whose completion evidence is not provably determinate."""
    code = 'indeterminate_result_not_visible'

@dataclass(frozen=True, slots=True)
class ResultKindSpec:
    """Static description of one result type's persistence contract."""
    kind: str
    record_cls: type
    sort_columns: tuple[str, ...]
    key_kinds: tuple[str, ...]
    time_column: str | None = None
    allowed_filters: frozenset[str] = frozenset()

    @property
    def upper_bound_columns(self) -> dict[str, str]:
        """Column-to-kind map validated inside every cursor token."""
        return dict(zip(self.sort_columns, self.key_kinds))

_RESULT_KINDS: dict[str, ResultKindSpec] = {spec.kind: spec for spec in (ResultKindSpec(kind='steps', record_cls=BacktestStepRecord, sort_columns=('step_sequence',), key_kinds=('int',), allowed_filters=frozenset({'phase'})), ResultKindSpec(kind='events', record_cls=BacktestEventResultRecord, sort_columns=('event_sequence',), key_kinds=('int',), allowed_filters=frozenset({'event_type', 'start_time', 'end_time'}), time_column='event_time'), ResultKindSpec(kind='decisions', record_cls=BacktestDecisionRecord, sort_columns=('step_sequence', 'decision_time', 'decision_id'), key_kinds=('int', 'ts', 'uuid'), time_column='decision_time', allowed_filters=frozenset({'mode', 'start_time', 'end_time'})), ResultKindSpec(kind='orders', record_cls=BacktestOrderResultRecord, sort_columns=('submitted_at', 'order_id'), key_kinds=('ts', 'uuid'), time_column='submitted_at', allowed_filters=frozenset({'instrument_id', 'status', 'side', 'start_time', 'end_time'})), ResultKindSpec(kind='order_updates', record_cls=BacktestOrderUpdateRecord, sort_columns=('updated_at', 'order_id', 'update_sequence'), key_kinds=('ts', 'uuid', 'int'), time_column='updated_at', allowed_filters=frozenset({'status', 'start_time', 'end_time'})), ResultKindSpec(kind='fills', record_cls=BacktestFillResultRecord, sort_columns=('timestamp', 'fill_sequence', 'fill_id'), key_kinds=('ts', 'int', 'uuid'), time_column='timestamp', allowed_filters=frozenset({'instrument_id', 'side', 'start_time', 'end_time'})), ResultKindSpec(kind='positions', record_cls=BacktestPositionResultRecord, sort_columns=('as_of', 'instrument_id', 'side'), key_kinds=('ts', 'uuid', 'str'), time_column='as_of', allowed_filters=frozenset({'instrument_id', 'side', 'start_time', 'end_time'})), ResultKindSpec(kind='equity_curve', record_cls=BacktestEquityCurveRecord, sort_columns=('as_of', 'sequence'), key_kinds=('ts', 'int'), time_column='as_of', allowed_filters=frozenset({'start_time', 'end_time'})), ResultKindSpec(kind='metrics', record_cls=BacktestMetricRecord, sort_columns=('metric_key', 'formula_version'), key_kinds=('str', 'str'), allowed_filters=frozenset()), ResultKindSpec(kind='data_preflight', record_cls=BacktestDataPreflightResultRecord, sort_columns=('phase',), key_kinds=('str',), allowed_filters=frozenset()), ResultKindSpec(kind='data_chunks', record_cls=BacktestDataChunkRecord, sort_columns=('phase', 'chunk_sequence'), key_kinds=('str', 'int'), allowed_filters=frozenset({'phase'})))}

def get_result_kind_spec(kind: str) -> ResultKindSpec:
    try:
        return _RESULT_KINDS[kind]
    except KeyError as exc:
        raise UnknownResultKindError(f'unknown result kind: {kind!r}') from exc

def _normalize_instant(value: datetime) -> str:
    """Canonical string form of a filter boundary for digest purposes."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ResultFilterError('start_time/end_time must be timezone-aware')
    return value.astimezone(timezone.utc).isoformat()

def _normalize_filter_value(name: str, value: Any) -> str | None:
    """Normalize one raw filter argument into its digest representation."""
    if value is None:
        return None
    if name == 'instrument_id':
        return str(_require_uuid(name, value))
    if name == 'start_time' or name == 'end_time':
        return _normalize_instant(value)
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or None
    raise ResultFilterError(f'unsupported filter value type for {name}')

def _require_uuid(name: str, value: Any) -> UUID:
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError as exc:
            raise ResultFilterError(f'{name} must be a valid UUID') from exc
    if not isinstance(value, UUID):
        raise ResultFilterError(f'{name} must be a UUID')
    return value

def build_query_payload(spec: ResultKindSpec, *, run_id: UUID, limit: int, filters: Mapping[str, str], query_context: Mapping[str, Any] | None=None) -> dict[str, Any]:
    """Canonical query description feeding the cursor digest.

    Every condition that can change the result set participates: run id,
    each filter, the fixed ascending sort keys, the page-size policy, and
    the concrete limit requested by the client.
    """
    cursor_kind = 'backtest_data_preflight' if spec.kind == 'data_preflight' else spec.kind
    payload = {'kind': cursor_kind, 'run_id': str(run_id), 'filters': dict(sorted(filters.items())), 'direction': 'asc', 'sort_keys': list(spec.sort_columns), 'page_size_policy': {'default': DEFAULT_PAGE_SIZE, 'max': MAX_PAGE_SIZE}, 'limit': limit}
    if query_context is not None:
        if not isinstance(query_context, Mapping):
            raise ResultFilterError('query_context must be a mapping')
        payload['query_context'] = dict(sorted(query_context.items()))
    return payload

class BacktestResultRepository:

    def __init__(self, session: Session, *, cursor_signing_key: str) -> None:
        if not isinstance(cursor_signing_key, str) or not cursor_signing_key.strip():
            raise ValueError('cursor_signing_key must be non-blank text')
        self.session = session
        self._signing_key = cursor_signing_key

    def get_run_root(self, run_id: UUID | str, *, owner_scope: str | None=None) -> BacktestRunRecord:
        """Load one owner-visible root for server-side visibility decisions."""
        run_uuid = _require_uuid('run_id', run_id)
        root = self.session.get(BacktestRunRecord, run_uuid)
        if root is None or (owner_scope is not None and getattr(root, 'idempotency_scope', getattr(root, 'tenant_id', None)) != owner_scope):
            raise UnknownResultKindError('backtest run root not found')
        return root

    def assert_result_visible(self, run_id: UUID | str, *, expected_kind: str='backtest_run', owner_scope: str | None=None, allow_indeterminate: bool=False) -> BacktestRunRecord:
        """Apply root kind and determinate-result guards in one place.

        Internal results are never made visible through a public flag.  An
        explicit internal diagnostic handler may call this method with
        ``expected_kind='internal_link_acceptance'`` after its own capability
        check; formal handlers retain the default.
        """
        root = self.get_run_root(run_id, owner_scope=owner_scope)
        expected_profile = 'formal@1' if expected_kind == 'backtest_run' else 'internal_link_acceptance@1' if expected_kind == 'internal_link_acceptance' else None
        if expected_profile is None or root.run_kind != expected_kind or root.profile != expected_profile:
            raise InternalResultNotVisibleError('该运行不属于请求的结果范围')
        if expected_kind == 'backtest_run' and (not allow_indeterminate) and (root.status == 'indeterminate' or root.terminal_status == 'indeterminate'):
            raise IndeterminateResultNotVisibleError('不确定终态运行不进入正式结果或比较')
        return root

    def _assert_visible_run(self, run_id: UUID, *, include_internal: bool, owner_scope: str | None=None) -> None:
        """Enforce formal-default visibility for every result kind."""
        if include_internal:
            self.assert_result_visible(run_id, expected_kind='internal_link_acceptance', owner_scope=owner_scope, allow_indeterminate=True)
            return
        self.assert_result_visible(run_id, expected_kind='backtest_run', owner_scope=owner_scope)

    def get_analysis_summary(self, run_id: UUID | str, *, include_internal: bool=False, owner_scope: str | None=None) -> BacktestAnalysisSummaryRecord | None:
        """Read the single analysis summary bound to one run."""
        run_uuid = _require_uuid('run_id', run_id)
        if include_internal:
            raise InternalResultNotVisibleError('公开结果接口不支持 include_internal')
        self.assert_result_visible(run_uuid, expected_kind='backtest_run', owner_scope=owner_scope)
        record = self.session.scalars(select(BacktestAnalysisSummaryRecord).where(BacktestAnalysisSummaryRecord.run_id == run_uuid)).first()
        if record is None:
            return None
        return record

    def read_page(self, kind: str, *, run_id: UUID | str, limit: int=DEFAULT_PAGE_SIZE, cursor: str | None=None, query_context: Mapping[str, Any] | None=None, include_internal: bool=False, owner_scope: str | None=None, **raw_filters: Any) -> CursorPage:
        """Return one stable page of results for a run.

        The first call captures the maximum visible sort-key tuple as the
        query's upper bound inside the SAME SQL statement that reads the
        page (window functions), so under READ COMMITTED the page and the
        bound always come from one snapshot.  Every later page reached
        through the returned cursor stays inside that bound, so rows
        appended while a client is paging never create duplicates or shift
        earlier pages.
        """
        spec = get_result_kind_spec(kind)
        run_uuid = _require_uuid('run_id', run_id)
        if not isinstance(include_internal, bool):
            raise ResultFilterError('include_internal must be a boolean')
        self._assert_visible_run(run_uuid, include_internal=include_internal, owner_scope=owner_scope)
        checked_limit = normalize_limit(limit)
        filters: dict[str, str] = {}
        for name, value in raw_filters.items():
            if name not in spec.allowed_filters:
                raise ResultFilterError(f'{kind} does not support filter {name!r}')
            normalized = _normalize_filter_value(name, value)
            if normalized is not None:
                filters[name] = normalized
        payload = build_query_payload(spec, run_id=run_uuid, limit=checked_limit, filters=filters, query_context=query_context)
        if include_internal:
            payload['include_internal'] = True
        if cursor is None:
            return self._read_first_page(spec, payload, run_uuid, filters, checked_limit)
        return self._read_continuation_page(spec, payload, run_uuid, filters, checked_limit, cursor)

    def _read_first_page(self, spec: ResultKindSpec, payload: Mapping[str, Any], run_id: UUID, filters: Mapping[str, str], limit: int) -> CursorPage:
        """Read the first page and its upper bound from one statement.

        ``first_value(...) OVER (ORDER BY <sort> DESC)`` exposes the maximum
        visible sort-key columns next to every row.  Because window
        functions are evaluated over the full filtered row set before LIMIT,
        the page and the bound cannot come from different snapshots.
        """
        statement = self._first_page_statement(spec, run_id, filters)
        executed = list(self.session.execute(statement.limit(limit + 1)))
        rows = [row[0] for row in executed]
        rows.sort(key=lambda row: self._row_sort_key(spec, row))
        has_more = len(rows) > limit
        items = tuple(rows[:limit])
        if not items:
            return CursorPage(items=(), next_cursor=None, has_more=False)
        next_cursor: str | None = None
        if has_more:
            bound = self._bound_from_window_row(spec, executed[0])
            encoded_bound = self._encode_bound(spec, dict(zip(spec.sort_columns, bound)))
            digest = compute_query_digest({**payload, 'query_upper_bound': encoded_bound})
            next_cursor = build_cursor(signing_key=self._signing_key, query_digest=digest, key_kinds=spec.key_kinds, last_sort_key=self._row_sort_key(spec, items[-1]), upper_bound_columns=spec.upper_bound_columns, query_upper_bound=dict(zip(spec.sort_columns, bound)))
        return CursorPage(items=items, next_cursor=next_cursor, has_more=has_more)

    def _read_continuation_page(self, spec: ResultKindSpec, payload: Mapping[str, Any], run_id: UUID, filters: Mapping[str, str], limit: int, cursor: str) -> CursorPage:
        parsed = parse_cursor(cursor, signing_key=self._signing_key, key_kinds=spec.key_kinds, upper_bound_columns=spec.upper_bound_columns)
        bound_values = tuple((parsed.query_upper_bound[column] for column in spec.sort_columns))
        encoded_bound = self._encode_bound(spec, dict(zip(spec.sort_columns, bound_values)))
        expected_digest = compute_query_digest({**payload, 'query_upper_bound': encoded_bound})
        if not hmac_compare(parsed.query_digest, expected_digest):
            raise CursorQueryMismatchError('cursor belongs to a different query; restart from the first page')
        statement = self._base_statement(spec, run_id, filters).where(self._keyset_predicate(spec, parsed.last_sort_key, mode='after')).where(self._keyset_predicate(spec, bound_values, mode='at_or_before'))
        rows = list(self.session.scalars(statement.limit(limit + 1)))
        rows.sort(key=lambda row: self._row_sort_key(spec, row))
        has_more = len(rows) > limit
        items = tuple(rows[:limit])
        if not items:
            return CursorPage(items=(), next_cursor=None, has_more=False)
        next_cursor: str | None = None
        if has_more:
            next_cursor = build_cursor(signing_key=self._signing_key, query_digest=expected_digest, key_kinds=spec.key_kinds, last_sort_key=self._row_sort_key(spec, items[-1]), upper_bound_columns=spec.upper_bound_columns, query_upper_bound=dict(zip(spec.sort_columns, bound_values)))
        return CursorPage(items=items, next_cursor=next_cursor, has_more=has_more)

    def _first_page_statement(self, spec: ResultKindSpec, run_id: UUID, filters: Mapping[str, str]):
        record_cls = spec.record_cls
        conditions: list[Any] = [record_cls.run_id == run_id]
        conditions.extend((self._filter_condition(spec, name, value) for name, value in filters.items()))
        window_order = [getattr(record_cls, column_name).desc() for column_name in spec.sort_columns]
        window_columns = []
        for position, column_name in enumerate(spec.sort_columns):
            column = getattr(record_cls, column_name)
            first_value = func.first_value(column, type_=column.type)
            window_columns.append(first_value.over(order_by=window_order).label(f'__upper_bound_{position}'))
        return select(record_cls, *window_columns).where(*conditions).order_by(*[getattr(record_cls, column).asc() for column in spec.sort_columns])

    def _encode_bound(self, spec: ResultKindSpec, bound: Mapping[str, Any]) -> dict[str, Any]:
        """Canonical wire form of an upper bound, feeding the query digest.

        Binding the digest to the bound means a cursor can only be paired
        with the exact query state it was issued for.
        """
        return {column: encode_sort_element(spec.upper_bound_columns[column], bound[column]) for column in sorted(bound)}

    def _bound_from_window_row(self, spec: ResultKindSpec, row: Any) -> tuple[Any, ...]:
        values: list[Any] = []
        for position, kind in enumerate(spec.key_kinds):
            raw = getattr(row, f'__upper_bound_{position}')
            values.append(_normalize_sort_value(kind, raw))
        return tuple(values)

    def _base_statement(self, spec: ResultKindSpec, run_id: UUID, filters: Mapping[str, str], *, descending: bool=False):
        record_cls = spec.record_cls
        statement = select(record_cls).where(record_cls.run_id == run_id)
        for name, value in filters.items():
            statement = statement.where(self._filter_condition(spec, name, value))
        directions = [getattr(record_cls, column).desc() for column in spec.sort_columns] if descending else [getattr(record_cls, column).asc() for column in spec.sort_columns]
        statement = statement.order_by(*directions)
        return statement

    def _filter_condition(self, spec: ResultKindSpec, name: str, value: str):
        record_cls = spec.record_cls
        if name == 'instrument_id':
            return record_cls.instrument_id == UUID(value)
        if name == 'start_time':
            assert spec.time_column is not None
            return getattr(record_cls, spec.time_column) >= datetime.fromisoformat(value)
        if name == 'end_time':
            assert spec.time_column is not None
            return getattr(record_cls, spec.time_column) <= datetime.fromisoformat(value)
        column_name = 'new_status' if name == 'status' and spec.kind == 'order_updates' else name
        return getattr(record_cls, column_name) == value

    def _keyset_predicate(self, spec: ResultKindSpec, values: Sequence[Any], *, mode: str):
        """Tuple comparison expanded into OR-of-ANDS for portability.

        ``mode="after"`` selects tuples strictly greater than ``values``
        (page continuation).  ``mode="at_or_before"`` selects tuples less
        than or equal to ``values`` (the snapshot upper bound), so the
        maximum row visible when pagination started stays readable.
        """
        columns = [getattr(spec.record_cls, column) for column in spec.sort_columns]
        inclusive = mode == 'at_or_before'
        conditions = []
        last_position = len(columns) - 1
        for position in range(len(columns)):
            conjunction = [columns[index] == values[index] for index in range(position)]
            column = columns[position]
            value = values[position]
            if inclusive:
                conjunction.append(column <= value if position == last_position else column < value)
            else:
                conjunction.append(column > value)
            conditions.append(and_(*conjunction))
        return or_(*conditions)

    def _row_sort_key(self, spec: ResultKindSpec, row: Any) -> tuple[Any, ...]:
        """Typed, comparable sort-key tuple of one ORM row."""
        return tuple((_normalize_sort_value(kind, getattr(row, column)) for column, kind in zip(spec.sort_columns, spec.key_kinds)))


def _aware(value: datetime) -> datetime:
    """Reattach UTC to naive datetimes returned by non-tz databases.

    PostgreSQL ``timestamptz`` always returns aware values; the UTC fallback
    keeps SQLite-based tests deterministic without weakening production
    semantics.
    """

    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value

def _normalize_sort_value(kind: str, value: Any) -> Any:
    """Coerce a raw column value into its typed, comparable sort-key form."""

    if kind == "ts":
        return _aware(value)
    if kind == "uuid":
        return value if isinstance(value, UUID) else UUID(str(value))
    if kind == "dec":
        return value if isinstance(value, Decimal) else Decimal(str(value))
    return value
