"""Persistence and cursor-paginated queries for backtest results.

The repository owns three responsibilities:

1. writing validated result DTOs into the append-only result tables,
   rejecting duplicate business keys inside a run;
2. building stable keyset-paginated reads whose sort keys match the
   result specification exactly (time ties are broken by entity id or
   in-run sequence, never by mutable display fields);
3. issuing opaque cursors bound to a canonicalized query digest and a
   snapshot upper bound so appended rows never leak into an existing page
   walk.

The repository depends on validated DTOs and SQLAlchemy records only; it
never resolves instruments itself, so no market-data client can leak in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.backtesting.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    CursorPage,
    build_cursor,
    compute_query_digest,
    normalize_limit,
    parse_cursor,
)
from app.backtesting.result_models import (
    BacktestDataChunkRecord as BacktestDataChunkDto,
    BacktestDataPreflightRecord as BacktestDataPreflightDto,
    BacktestDecisionRecord as BacktestDecisionDto,
    BacktestEquityCurveRecord as BacktestEquityCurveDto,
    BacktestFillRecord as BacktestFillDto,
    BacktestMetricRecord as BacktestMetricDto,
    BacktestOrderRecord as BacktestOrderDto,
    BacktestOrderUpdateRecord as BacktestOrderUpdateDto,
    BacktestPositionRecord as BacktestPositionDto,
    BacktestStepRecord as BacktestStepDto,
)
from app.backtesting.result_records import (
    BacktestDataChunkRecord,
    BacktestDataPreflightResultRecord,
    BacktestDecisionRecord,
    BacktestEquityCurveRecord,
    BacktestFillResultRecord,
    BacktestMetricRecord,
    BacktestOrderResultRecord,
    BacktestOrderUpdateRecord,
    BacktestPositionResultRecord,
    BacktestStepRecord,
)


class ResultRepositoryError(ValueError):
    """Base class for repository usage errors."""


class UnknownResultKindError(ResultRepositoryError):
    """The requested result kind is not registered."""


class ResultFilterError(ResultRepositoryError):
    """A filter is not supported by the requested result kind."""


class ResultRecordConflictError(Exception):
    """The row violates the run-scoped uniqueness contract."""


# ---------------------------------------------------------------------------
# DTO -> record conversion helpers
# ---------------------------------------------------------------------------


def _thaw_json(value: Any) -> Any:
    """Convert frozen containers back into JSON-serializable structures."""

    if isinstance(value, MappingProxyType) or isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json(item) for item in value]
    return value


def _aware(value: datetime) -> datetime:
    """Reattach UTC to naive datetimes returned by non-tz databases.

    PostgreSQL ``timestamptz`` always returns aware values; the UTC fallback
    keeps SQLite-based tests deterministic without weakening production
    semantics.
    """

    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _step_record(dto: BacktestStepDto) -> dict[str, Any]:
    return {
        "run_id": dto.run_id,
        "step_sequence": dto.step_sequence,
        "time_start": dto.time_start,
        "time_end": dto.time_end,
        "data_cutoff_at": dto.data_cutoff_at,
        "phase": dto.phase.value,
        "data_quality": dto.data_quality.value,
    }


def _decision_record(dto: BacktestDecisionDto) -> dict[str, Any]:
    return {
        "run_id": dto.run_id,
        "decision_id": dto.decision_id,
        "step_sequence": dto.step_sequence,
        "decision_time": dto.decision_time,
        "mode": dto.mode,
        "targets": _thaw_json(dict(dto.targets)),
        "validation_status": dto.validation_status.value,
        "validation_issues": list(dto.validation_issues),
        "duration_ms": dto.duration_ms,
        "error": dto.error,
    }


def _order_record(dto: BacktestOrderDto) -> dict[str, Any]:
    return {
        "run_id": dto.run_id,
        "order_id": dto.order_id,
        "intent_id": dto.intent_id,
        "instrument_id": dto.instrument_id,
        "event_trading_code": dto.display.event_trading_code,
        "event_name": dto.display.event_name,
        "event_display_name": dto.display.event_display_name,
        "side": dto.side.value,
        "order_type": dto.order_type,
        "price": dto.price,
        "quantity": dto.quantity,
        "filled_quantity": dto.filled_quantity,
        "status": dto.status.value,
        "status_reason": dto.status_reason,
        "submitted_at": dto.submitted_at,
    }


def _order_update_record(dto: BacktestOrderUpdateDto) -> dict[str, Any]:
    return {
        "run_id": dto.run_id,
        "order_id": dto.order_id,
        "update_sequence": dto.update_sequence,
        "old_status": dto.old_status.value if dto.old_status else None,
        "new_status": dto.new_status.value,
        "updated_at": dto.updated_at,
        "reason": dto.reason,
    }


def _fill_record(dto: BacktestFillDto) -> dict[str, Any]:
    return {
        "run_id": dto.run_id,
        "fill_id": dto.fill_id,
        "order_id": dto.order_id,
        "instrument_id": dto.instrument_id,
        "event_trading_code": dto.display.event_trading_code,
        "event_name": dto.display.event_name,
        "event_display_name": dto.display.event_display_name,
        "side": dto.side.value,
        "timestamp": dto.timestamp,
        "reference_price": dto.reference_price,
        "price": dto.price,
        "quantity": dto.quantity,
        "fees": dto.fees,
        "slippage_bps": dto.slippage_bps,
        "slippage_amount": dto.slippage_amount,
        "slippage_model_key": dto.slippage_model_key,
        "slippage_model_version": dto.slippage_model_version,
    }


def _position_record(dto: BacktestPositionDto) -> dict[str, Any]:
    return {
        "run_id": dto.run_id,
        "as_of": dto.as_of,
        "instrument_id": dto.instrument_id,
        "event_trading_code": dto.display.event_trading_code,
        "event_name": dto.display.event_name,
        "event_display_name": dto.display.event_display_name,
        "side": dto.side.value,
        "quantity": dto.quantity,
        "available_quantity": dto.available_quantity,
        "average_price": dto.average_price,
        "mark_price": dto.mark_price,
        "realized_pnl": dto.realized_pnl,
        "unrealized_pnl": dto.unrealized_pnl,
    }


def _equity_record(dto: BacktestEquityCurveDto) -> dict[str, Any]:
    return {
        "run_id": dto.run_id,
        "sequence": dto.sequence,
        "as_of": dto.as_of,
        "valuation_status": dto.valuation_status.value,
        "valuation_reason": dto.valuation_reason,
        "cash": dto.cash,
        "market_value": dto.market_value,
        "equity": dto.equity,
        "cumulative_return": dto.cumulative_return,
        "drawdown": dto.drawdown,
        "cumulative_fees": dto.cumulative_fees,
    }


def _metric_record(dto: BacktestMetricDto) -> dict[str, Any]:
    return {
        "run_id": dto.run_id,
        "metric_key": dto.metric_key,
        "formula_version": dto.formula_version,
        "value": dto.value,
        "unit": dto.unit,
        "annualization_factor": dto.annualization_factor,
        "risk_free_rate_note": dto.risk_free_rate_note,
        "sample_count": dto.sample_count,
        "unavailable_reason": dto.unavailable_reason,
    }


def _preflight_record(dto: BacktestDataPreflightDto) -> dict[str, Any]:
    return {
        "run_id": dto.run_id,
        "phase": dto.phase.value,
        "status": dto.status,
        "report_hash": dto.report_hash,
        "capabilities": _thaw_json(dict(dto.capabilities)),
        "calendar_summary": _thaw_json(dict(dto.calendar_summary)),
        "session_summary": _thaw_json(dict(dto.session_summary)),
        "pit_status": dto.pit_status,
        "coverage": _thaw_json(dict(dto.coverage)),
        "source_revisions": _thaw_json(dict(dto.source_revisions)),
    }


def _chunk_record(dto: BacktestDataChunkDto) -> dict[str, Any]:
    return {
        "run_id": dto.run_id,
        "phase": dto.phase.value,
        "chunk_sequence": dto.chunk_sequence,
        "time_start": dto.time_start,
        "time_end": dto.time_end,
        "chunk_strategy_version": dto.chunk_strategy_version,
        "token_digest": dto.token_digest,
        "validation_status": dto.validation_status.value,
        "started_at": dto.started_at,
        "finished_at": dto.finished_at,
        "failure_reason": dto.failure_reason,
    }


# ---------------------------------------------------------------------------
# Result kind registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResultKindSpec:
    """Static description of one result type's persistence contract."""

    kind: str
    dto_cls: type
    record_cls: type
    to_record: Callable[[Any], dict[str, Any]]
    # Pagination sort keys: ORM column name plus wire element kind.
    sort_columns: tuple[str, ...]
    key_kinds: tuple[str, ...]
    # Business identity used for in-batch duplicate detection.
    identity_fields: tuple[str, ...]
    # Event timestamp used by inclusive start/end filters, when supported.
    time_column: str | None = None
    allowed_filters: frozenset[str] = frozenset()

    @property
    def upper_bound_columns(self) -> dict[str, str]:
        """Column-to-kind map validated inside every cursor token."""

        return dict(zip(self.sort_columns, self.key_kinds))


_RESULT_KINDS: dict[str, ResultKindSpec] = {
    spec.kind: spec
    for spec in (
        ResultKindSpec(
            kind="steps",
            dto_cls=BacktestStepDto,
            record_cls=BacktestStepRecord,
            to_record=_step_record,
            sort_columns=("step_sequence",),
            key_kinds=("int",),
            identity_fields=("step_sequence",),
            allowed_filters=frozenset({"phase"}),
        ),
        ResultKindSpec(
            kind="decisions",
            dto_cls=BacktestDecisionDto,
            record_cls=BacktestDecisionRecord,
            to_record=_decision_record,
            sort_columns=("step_sequence", "decision_time", "decision_id"),
            key_kinds=("int", "ts", "uuid"),
            identity_fields=("decision_id",),
            time_column="decision_time",
            allowed_filters=frozenset({"mode", "start_time", "end_time"}),
        ),
        ResultKindSpec(
            kind="orders",
            dto_cls=BacktestOrderDto,
            record_cls=BacktestOrderResultRecord,
            to_record=_order_record,
            sort_columns=("submitted_at", "order_id"),
            key_kinds=("ts", "uuid"),
            identity_fields=("order_id",),
            time_column="submitted_at",
            allowed_filters=frozenset(
                {"instrument_id", "status", "side", "start_time", "end_time"}
            ),
        ),
        ResultKindSpec(
            kind="order_updates",
            dto_cls=BacktestOrderUpdateDto,
            record_cls=BacktestOrderUpdateRecord,
            to_record=_order_update_record,
            sort_columns=("updated_at", "order_id", "update_sequence"),
            key_kinds=("ts", "uuid", "int"),
            identity_fields=("order_id", "update_sequence"),
            time_column="updated_at",
            allowed_filters=frozenset({"status", "start_time", "end_time"}),
        ),
        ResultKindSpec(
            kind="fills",
            dto_cls=BacktestFillDto,
            record_cls=BacktestFillResultRecord,
            to_record=_fill_record,
            sort_columns=("timestamp", "fill_id"),
            key_kinds=("ts", "uuid"),
            identity_fields=("fill_id",),
            time_column="timestamp",
            allowed_filters=frozenset(
                {"instrument_id", "side", "start_time", "end_time"}
            ),
        ),
        ResultKindSpec(
            kind="positions",
            dto_cls=BacktestPositionDto,
            record_cls=BacktestPositionResultRecord,
            to_record=_position_record,
            sort_columns=("as_of", "instrument_id", "side"),
            key_kinds=("ts", "uuid", "str"),
            identity_fields=("as_of", "instrument_id", "side"),
            time_column="as_of",
            allowed_filters=frozenset(
                {"instrument_id", "side", "start_time", "end_time"}
            ),
        ),
        ResultKindSpec(
            kind="equity_curve",
            dto_cls=BacktestEquityCurveDto,
            record_cls=BacktestEquityCurveRecord,
            to_record=_equity_record,
            sort_columns=("as_of", "sequence"),
            key_kinds=("ts", "int"),
            identity_fields=("sequence",),
            time_column="as_of",
            allowed_filters=frozenset({"start_time", "end_time"}),
        ),
        ResultKindSpec(
            kind="metrics",
            dto_cls=BacktestMetricDto,
            record_cls=BacktestMetricRecord,
            to_record=_metric_record,
            sort_columns=("metric_key", "formula_version"),
            key_kinds=("str", "str"),
            identity_fields=("metric_key", "formula_version"),
            allowed_filters=frozenset(),
        ),
        ResultKindSpec(
            kind="data_preflight",
            dto_cls=BacktestDataPreflightDto,
            record_cls=BacktestDataPreflightResultRecord,
            to_record=_preflight_record,
            sort_columns=("phase",),
            key_kinds=("str",),
            identity_fields=("phase",),
            allowed_filters=frozenset(),
        ),
        ResultKindSpec(
            kind="data_chunks",
            dto_cls=BacktestDataChunkDto,
            record_cls=BacktestDataChunkRecord,
            to_record=_chunk_record,
            sort_columns=("phase", "chunk_sequence"),
            key_kinds=("str", "int"),
            identity_fields=("phase", "chunk_sequence"),
            allowed_filters=frozenset({"phase"}),
        ),
    )
}


def get_result_kind_spec(kind: str) -> ResultKindSpec:
    try:
        return _RESULT_KINDS[kind]
    except KeyError as exc:
        raise UnknownResultKindError(f"unknown result kind: {kind!r}") from exc


# ---------------------------------------------------------------------------
# Query canonicalization
# ---------------------------------------------------------------------------


def _normalize_instant(value: datetime) -> str:
    """Canonical string form of a filter boundary for digest purposes."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ResultFilterError("start_time/end_time must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _normalize_filter_value(name: str, value: Any) -> str | None:
    """Normalize one raw filter argument into its digest representation."""

    if value is None:
        return None
    if name == "instrument_id":
        return str(_require_uuid(name, value))
    if name == "start_time" or name == "end_time":
        return _normalize_instant(value)
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or None
    raise ResultFilterError(f"unsupported filter value type for {name}")


def _require_uuid(name: str, value: Any) -> UUID:
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError as exc:
            raise ResultFilterError(f"{name} must be a valid UUID") from exc
    if not isinstance(value, UUID):
        raise ResultFilterError(f"{name} must be a UUID")
    return value


def build_query_payload(
    spec: ResultKindSpec,
    *,
    run_id: UUID,
    limit: int,
    filters: Mapping[str, str],
) -> dict[str, Any]:
    """Canonical query description feeding the cursor digest.

    Every condition that can change the result set participates: run id,
    each filter, the fixed ascending sort keys, the page-size policy, and
    the concrete limit requested by the client.
    """

    return {
        "kind": spec.kind,
        "run_id": str(run_id),
        "filters": dict(sorted(filters.items())),
        "direction": "asc",
        "sort_keys": list(spec.sort_columns),
        "page_size_policy": {"default": DEFAULT_PAGE_SIZE, "max": MAX_PAGE_SIZE},
        "limit": limit,
    }


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class BacktestResultRepository:
    """Write result facts and read them back through stable cursor pages."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # -- writes ------------------------------------------------------------

    def append(self, kind: str, *dtos: Any) -> int:
        """Persist validated DTOs; return the number of rows written.

        Duplicate business keys inside the batch or already persisted in the
        same run raise :class:`ResultRecordConflictError`; the caller owns
        the surrounding transaction.
        """

        spec = get_result_kind_spec(kind)
        if not dtos:
            return 0
        seen: set[tuple[Any, ...]] = set()
        payloads: list[dict[str, Any]] = []
        for dto in dtos:
            if not isinstance(dto, spec.dto_cls):
                raise ResultRepositoryError(
                    f"{kind} expects {spec.dto_cls.__name__}, got "
                    f"{type(dto).__name__}"
                )
            identity = tuple(getattr(dto, name) for name in spec.identity_fields)
            if identity in seen:
                raise ResultRecordConflictError(
                    f"duplicate {kind} identity {identity} within the batch"
                )
            seen.add(identity)
            payloads.append(spec.to_record(dto))
        try:
            self.session.add_all([spec.record_cls(**payload) for payload in payloads])
            self.session.flush()
        except IntegrityError as exc:
            raise ResultRecordConflictError(
                f"{kind} row violates the run-scoped uniqueness contract"
            ) from exc
        return len(payloads)

    # -- reads -------------------------------------------------------------

    def read_page(
        self,
        kind: str,
        *,
        run_id: UUID | str,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
        **raw_filters: Any,
    ) -> CursorPage:
        """Return one stable page of results for a run.

        The first call captures the maximum visible sort-key tuple as the
        query's upper bound; every later page reached through the returned
        cursor stays inside that bound, so rows appended while a client is
        paging never create duplicates or shift earlier pages.
        """

        spec = get_result_kind_spec(kind)
        run_uuid = _require_uuid("run_id", run_id)
        checked_limit = normalize_limit(limit)

        filters: dict[str, str] = {}
        for name, value in raw_filters.items():
            if name not in spec.allowed_filters:
                raise ResultFilterError(f"{kind} does not support filter {name!r}")
            normalized = _normalize_filter_value(name, value)
            if normalized is not None:
                filters[name] = normalized

        payload = build_query_payload(spec, run_id=run_uuid, limit=checked_limit, filters=filters)
        digest = compute_query_digest(payload)

        statement = self._base_statement(spec, run_uuid, filters)
        parsed = None
        if cursor is not None:
            parsed = self._parse_cursor(spec, cursor, digest)
            statement = statement.where(
                self._keyset_predicate(spec, parsed.last_sort_key, mode="after")
            ).where(
                self._keyset_predicate(
                    spec,
                    tuple(parsed.query_upper_bound[column] for column in spec.sort_columns),
                    # Inclusive bound: the maximum row visible at snapshot time
                    # must stay readable; appended rows sort strictly beyond it.
                    mode="at_or_before",
                )
            )

        rows = self._fetch_page(statement, spec, checked_limit)
        has_more = len(rows) > checked_limit
        items = tuple(rows[:checked_limit])
        if not items:
            return CursorPage(items=(), next_cursor=None, has_more=False)

        next_cursor: str | None = None
        if has_more:
            last_key = self._row_sort_key(spec, items[-1])
            if parsed is not None:
                # Continuation pages must keep the snapshot upper bound of the
                # original first page; recomputing it would admit rows that
                # were appended after the walk began.
                bound = tuple(
                    parsed.query_upper_bound[column] for column in spec.sort_columns
                )
            else:
                bound = self._upper_bound(spec, run_uuid, filters)
            next_cursor = build_cursor(
                query_digest=digest,
                key_kinds=spec.key_kinds,
                last_sort_key=last_key,
                upper_bound_columns=spec.upper_bound_columns,
                query_upper_bound=dict(zip(spec.sort_columns, bound)),
            )
        return CursorPage(items=items, next_cursor=next_cursor, has_more=has_more)

    # -- internals ---------------------------------------------------------

    def _base_statement(
        self,
        spec: ResultKindSpec,
        run_id: UUID,
        filters: Mapping[str, str],
        *,
        descending: bool = False,
    ):
        record_cls = spec.record_cls
        statement = select(record_cls).where(record_cls.run_id == run_id)
        for name, value in filters.items():
            statement = statement.where(self._filter_condition(spec, name, value))
        directions = (
            [getattr(record_cls, column).desc() for column in spec.sort_columns]
            if descending
            else [getattr(record_cls, column).asc() for column in spec.sort_columns]
        )
        statement = statement.order_by(*directions)
        return statement

    def _filter_condition(self, spec: ResultKindSpec, name: str, value: str):
        record_cls = spec.record_cls
        if name == "instrument_id":
            return record_cls.instrument_id == UUID(value)
        if name == "start_time":
            assert spec.time_column is not None
            return getattr(record_cls, spec.time_column) >= datetime.fromisoformat(value)
        if name == "end_time":
            assert spec.time_column is not None
            return getattr(record_cls, spec.time_column) <= datetime.fromisoformat(value)
        # Remaining filters target a column named after the filter itself,
        # except the order-update alias `status` -> `new_status`.
        column_name = "new_status" if name == "status" and spec.kind == "order_updates" else name
        return getattr(record_cls, column_name) == value

    def _keyset_predicate(
        self,
        spec: ResultKindSpec,
        values: Sequence[Any],
        *,
        mode: str,
    ):
        """Tuple comparison expanded into OR-of-ANDS for portability.

        ``mode="after"`` selects tuples strictly greater than ``values``
        (page continuation).  ``mode="at_or_before"`` selects tuples less
        than or equal to ``values`` (the snapshot upper bound), so the
        maximum row visible when pagination started stays readable.
        """

        columns = [getattr(spec.record_cls, column) for column in spec.sort_columns]
        inclusive = mode == "at_or_before"
        conditions = []
        last_position = len(columns) - 1
        for position in range(len(columns)):
            conjunction = [columns[index] == values[index] for index in range(position)]
            column = columns[position]
            value = values[position]
            if inclusive:
                conjunction.append(
                    column <= value if position == last_position else column < value
                )
            else:
                conjunction.append(column > value)
            conditions.append(and_(*conjunction))
        return or_(*conditions)

    def _fetch_page(self, statement: Any, spec: ResultKindSpec, limit: int):
        rows = list(self.session.scalars(statement.limit(limit + 1)))
        # Re-sort defensively: SQLite stores timestamps lexicographically and
        # multi-column ordering matches the SQL ORDER BY, but keeping the
        # Python-side guarantee explicit makes tie handling observable.
        rows.sort(key=lambda row: self._row_sort_key(spec, row))
        return rows

    def _row_sort_key(self, spec: ResultKindSpec, row: Any) -> tuple[Any, ...]:
        """Typed, comparable sort-key tuple of one ORM row."""

        key: list[Any] = []
        for column, kind in zip(spec.sort_columns, spec.key_kinds):
            value = getattr(row, column)
            if kind == "ts":
                value = _aware(value)
            elif kind == "uuid":
                value = value if isinstance(value, UUID) else UUID(str(value))
            elif kind == "dec":
                value = value if isinstance(value, Decimal) else Decimal(str(value))
            key.append(value)
        return tuple(key)

    def _upper_bound(
        self,
        spec: ResultKindSpec,
        run_id: UUID,
        filters: Mapping[str, str],
    ) -> tuple[Any, ...]:
        """Maximum sort-key tuple currently visible for this exact query."""

        # A fresh statement is required: appending a second order_by() would
        # combine both orderings instead of reversing the first one.
        statement = self._base_statement(
            spec, run_id, filters, descending=True
        ).limit(1)
        top = self.session.scalar(statement)
        if top is None:
            return ()
        return self._row_sort_key(spec, top)

    def _parse_cursor(self, spec: ResultKindSpec, token: str, digest: str):
        # parse_cursor raises the specific CursorError subclasses directly;
        # the router maps them to an explicit parameter error.
        return parse_cursor(
            token,
            expected_query_digest=digest,
            key_kinds=spec.key_kinds,
            upper_bound_columns=spec.upper_bound_columns,
        )


__all__ = [
    "BacktestResultRepository",
    "ResultFilterError",
    "ResultRecordConflictError",
    "ResultRepositoryError",
    "UnknownResultKindError",
    "build_query_payload",
    "get_result_kind_spec",
]
