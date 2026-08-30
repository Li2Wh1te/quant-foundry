"""SQL-backed named-calendar snapshot provider.

The strict SQL path has two deliberately separate phases.  ``prepare`` reads
only the materialized resolution-head index (open/closed metadata and one
revision watermark); it never reads definition or session-fact payloads.
``load`` performs one bounded set-based batch read on the very same
SQLAlchemy connection and transaction and freezes the result as a
:class:`CalendarSnapshot`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, InvalidRequestError
from sqlalchemy.orm import Session

from app.backtesting.calendar_axis import (
    CALENDAR_TIMEZONE_ASIA_SHANGHAI,
    CalendarAxisStatus,
    CalendarDefinition,
    CalendarExchangeBinding,
    CalendarPITContext,
    CalendarRegistry,
    CalendarSessionFact,
    CalendarSnapshot,
    CalendarSnapshotRequest,
    CalendarSourcePriority,
    CapabilityResolution,
    SessionPoint,
    _coverage_payload,
    _find_common_anchor,
    _find_common_open_history,
    _iterate_days,
    _materialize_calendar_index,
    _resolve_snapshot_range,
    _revision_payload,
    _selected_snapshot_definitions,
    _session_signature,
    _semantic_signature,
    _snapshot_request_from_object,
    _warmup_signature,
    canonical_hash,
    normalize_calendar_id,
    select_pit_candidate,
    select_capability_declaration,
)
from app.backtesting.calendar_models import (
    CalendarCapabilityDeclarationRecord,
    CalendarDefinitionRecord,
    CalendarExchangeBindingRecord,
    CalendarRegistryRecord,
    CalendarReconciliationRangeRecord,
    CalendarResolutionHeadRecord,
    CalendarSessionFactRecord,
    CalendarSourcePriorityRecord,
)
from app.backtesting.data.calendar_repository import (
    _binding,
    _capability,
    _definition,
    _priority,
    _registry,
    _session,
)
from app.backtesting.data.errors import (
    CalendarContractError,
    CalendarPreflightResourceLimitExceededError,
    CalendarSnapshotCoverageUnknownError,
    CalendarSnapshotRetryExhaustedError,
    CalendarSnapshotRevisionChangedError,
    InvalidDataRequestError,
    ProviderContractViolationError,
)

# These are count limits, not natural-day limits.  In particular, warmup does
# not imply a 10,000-day SQL envelope: the exact envelope is selected from
# contiguous resolution-head rows and the query is protected by a row budget.
MAX_SNAPSHOT_INDEX_ROWS = 1_000_000
MAX_SNAPSHOT_SESSION_ROWS = 1_000_000
MAX_SNAPSHOT_SOURCE_ROWS = 4_096

# SQLSTATE classes/codes whose failure is explicitly safe to retry for a
# read-only snapshot attempt.  Everything else is treated as a provider
# failure; retrying an unknown DBAPI error can repeat a broken query twice.
_TRANSIENT_SQLSTATES = frozenset({"40001", "40P01"})
_SQLITE_LOCK_ERROR_CODES = frozenset(
    {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
        getattr(sqlite3, "SQLITE_BUSY_RECOVERY", -1),
        getattr(sqlite3, "SQLITE_BUSY_SNAPSHOT", -1),
        getattr(sqlite3, "SQLITE_LOCKED_SHAREDCACHE", -1),
        getattr(sqlite3, "SQLITE_LOCKED_VTAB", -1),
    }
)
_SQLITE_LOCK_MESSAGES = (
    "database is locked",
    "database table is locked",
    "database schema is locked",
    "database is busy",
)


def _is_retryable_dbapi_error(exc: DBAPIError, *, dialect) -> bool:
    """Return whether SQLAlchemy identified this DBAPI failure as transient."""

    # SQLAlchemy sets this flag only after its dialect has identified a lost
    # connection.  Keep the dialect hook as a fallback for manually-created
    # DBAPIError instances used by integrations and tests.
    if exc.connection_invalidated:
        return True
    try:
        if dialect.is_disconnect(exc.orig, None, None):
            return True
    except Exception:
        # A dialect's classifier is third-party code; an exception there must
        # not turn an unknown database failure into an unsafe retry.
        pass

    orig = exc.orig
    sqlstate = next(
        (
            str(getattr(orig, attribute)).upper()
            for attribute in ("sqlstate", "pgcode")
            if getattr(orig, attribute, None) is not None
        ),
        None,
    )
    if sqlstate is not None and (
        sqlstate.startswith("08") or sqlstate in _TRANSIENT_SQLSTATES
    ):
        return True

    if dialect.name != "sqlite":
        return False
    error_code = getattr(orig, "sqlite_errorcode", None)
    if error_code in _SQLITE_LOCK_ERROR_CODES:
        return True
    message = str(orig).lower()
    return any(marker in message for marker in _SQLITE_LOCK_MESSAGES)


@dataclass(frozen=True, slots=True)
class _SnapshotIndexCell:
    """Metadata returned by the one prepare-stage index query."""

    calendar_id: str
    session_date: date
    is_open: bool
    selected_fact_id: UUID | None
    fact_version: int | None
    revision_watermark: str
    complete: bool = True


@dataclass(frozen=True, slots=True)
class _SnapshotTransaction:
    """Identity of the connection and transaction used by one attempt."""

    connection_id: int
    transaction_id: int | None
    dialect: str


@dataclass(frozen=True, slots=True)
class CalendarSnapshotPlan:
    """In-process plan bound to one connection/transaction and one index."""

    request: CalendarSnapshotRequest
    context: CalendarPITContext
    envelope_start: date
    envelope_end_exclusive: date
    anchor_candidate: date | None
    warmup_dates: tuple[date, ...]
    coverage: Mapping[str, object]
    revision_watermark: str
    transaction: _SnapshotTransaction
    open_session_index: Mapping[str, object] = field(default_factory=dict)
    index_cells: tuple[_SnapshotIndexCell, ...] = ()
    attempt_id: UUID = field(default_factory=uuid4)
    prepare_calls: int = 1
    batch_read_calls: int = 1

    @property
    def warmup_start(self) -> date:
        return self.envelope_start


class _BatchCalendarData:
    """Immutable in-process result of the SQL batch read.

    This is a pure lookup table for the already-read rows.  It is intentionally
    not ``InMemoryCalendarAxisDataProvider``: SQL snapshots must not obtain
    their semantics by opening a detached provider snapshot or by issuing a
    second prepare/read cycle.  The shared pure resolver consumes this table
    only after SQL loading has completed.
    """

    def __init__(
        self,
        definitions: Iterable[CalendarDefinition],
        facts: Iterable[CalendarSessionFact],
        *,
        registries: Iterable[CalendarRegistry] = (),
        bindings: Iterable[CalendarExchangeBinding] = (),
        capabilities: Iterable[object] = (),
        source_priorities: Iterable[CalendarSourcePriority] = (),
    ) -> None:
        self._definitions = tuple(definitions)
        self._facts = tuple(facts)
        self._registries = tuple(registries)
        self._bindings = tuple(bindings)
        self._capabilities = tuple(capabilities)
        self._source_priorities = tuple(source_priorities)
        definitions_by_calendar: dict[str, list[CalendarDefinition]] = {}
        facts_by_slot: dict[tuple[str, date], list[CalendarSessionFact]] = {}
        registries_by_calendar: dict[str, list[CalendarRegistry]] = {}
        for row in self._definitions:
            definitions_by_calendar.setdefault(row.calendar_id, []).append(row)
        for row in self._facts:
            facts_by_slot.setdefault((row.calendar_id, row.session_date), []).append(row)
        for row in self._registries:
            registries_by_calendar.setdefault(row.calendar_id, []).append(row)
        self._definitions_by_calendar = {
            key: tuple(value) for key, value in definitions_by_calendar.items()
        }
        self._facts_by_slot = {
            key: tuple(sorted(value, key=lambda row: (row.fact_version, str(row.fact_id))))
            for key, value in facts_by_slot.items()
        }
        self._registries_by_calendar = {
            key: tuple(value) for key, value in registries_by_calendar.items()
        }

    def definitions(self, calendar_id: str) -> tuple[CalendarDefinition, ...]:
        return self._definitions_by_calendar.get(normalize_calendar_id(calendar_id), ())

    def fact_candidates(self, calendar_id: str, day: date) -> tuple[CalendarSessionFact, ...]:
        return self._facts_by_slot.get((normalize_calendar_id(calendar_id), day), ())

    def fact(self, calendar_id: str, day: date) -> CalendarSessionFact | None:
        candidates = self.fact_candidates(calendar_id, day)
        return candidates[-1] if candidates else None

    def registries(self, calendar_id: str) -> tuple[CalendarRegistry, ...]:
        return self._registries_by_calendar.get(normalize_calendar_id(calendar_id), ())

    def bindings(self, alias: str) -> tuple[CalendarExchangeBinding, ...]:
        normalized = alias.strip().upper()
        return tuple(row for row in self._bindings if row.alias == normalized)

    def capabilities(self) -> tuple[object, ...]:
        return self._capabilities

    def source_priorities(self) -> tuple[CalendarSourcePriority, ...]:
        return self._source_priorities

    def resolve_registry(
        self,
        calendar_id: str,
        *,
        effective_day: date,
        pit_context: CalendarPITContext | None = None,
    ) -> CalendarRegistry:
        rows = [row for row in self.registries(calendar_id) if row.applies_to(effective_day)]
        if not rows:
            raise CalendarContractError("no visible calendar registry fact")
        selected = select_pit_candidate(
            rows,
            effective_day=effective_day,
            pit_context=pit_context,
            source_priorities=self._source_priorities,
            missing_code="calendar_registry_fact_missing",
            ambiguous_code="calendar_registry_ambiguous",
        )
        assert isinstance(selected, CalendarRegistry)
        if pit_context is not None:
            selected.strict_validate()
        return selected

    def resolve_binding(
        self,
        alias: str,
        *,
        effective_day: date,
        pit_context: CalendarPITContext | None = None,
    ) -> CalendarExchangeBinding:
        rows = [row for row in self.bindings(alias) if row.applies_to(effective_day)]
        if not rows:
            raise CalendarContractError("no visible calendar binding")
        selected = select_pit_candidate(
            rows,
            effective_day=effective_day,
            pit_context=pit_context,
            source_priorities=self._source_priorities,
            missing_code="calendar_binding_missing",
            ambiguous_code="calendar_binding_ambiguous",
        )
        assert isinstance(selected, CalendarExchangeBinding)
        if pit_context is not None:
            selected.strict_validate()
        return selected


class SqlCalendarAxisDataProvider:
    """Production SQL provider with a bounded two-phase snapshot protocol."""

    provider_key = "sql-calendar"
    protocol_version = "calendar_snapshot@1"

    def __init__(
        self,
        session: Session,
        *,
        provider_key: str = "sql-calendar",
        max_index_rows: int = MAX_SNAPSHOT_INDEX_ROWS,
        max_session_rows: int = MAX_SNAPSHOT_SESSION_ROWS,
        max_source_rows: int = MAX_SNAPSHOT_SOURCE_ROWS,
    ) -> None:
        if not isinstance(session, Session):
            raise InvalidDataRequestError("session must be a SQLAlchemy Session")
        self.session = session
        self.provider_key = provider_key
        self.max_index_rows = max_index_rows
        self.max_session_rows = max_session_rows
        self.max_source_rows = max_source_rows
        self.prepare_calls = 0
        self.batch_read_calls = 0
        self.fact_calls = 0
        self._snapshots: dict[UUID, CalendarSnapshot] = {}
        self._snapshot_connection: Connection | None = None
        self._snapshot_transaction: _SnapshotTransaction | None = None
        self._last_batch: _BatchCalendarData | None = None

    def _start_snapshot_transaction(self) -> _SnapshotTransaction:
        """Pin one SQLAlchemy connection and transaction for the attempt."""

        bind = self.session.get_bind()
        dialect = bind.dialect.name
        try:
            if dialect == "postgresql":
                connection = self.session.connection(
                    execution_options={"isolation_level": "REPEATABLE READ"}
                )
            else:
                # SQLite's transaction is the equivalent fixed read view.  It
                # remains open until the successful atomic open commits.
                connection = self.session.connection()
        except InvalidRequestError as exc:
            raise CalendarSnapshotRevisionChangedError(
                "calendar snapshot transaction cannot be pinned"
            ) from exc
        transaction = self.session.get_transaction()
        marker = _SnapshotTransaction(
            connection_id=id(connection),
            transaction_id=id(transaction) if transaction is not None else None,
            dialect=dialect,
        )
        self._snapshot_connection = connection
        self._snapshot_transaction = marker
        return marker

    def _assert_snapshot_transaction(self, marker: _SnapshotTransaction) -> None:
        """Reject a plan loaded through another connection or transaction."""

        connection = self.session.connection()
        transaction = self.session.get_transaction()
        transaction_id = id(transaction) if transaction is not None else None
        if (
            self._snapshot_connection is None
            or connection is not self._snapshot_connection
            or id(connection) != marker.connection_id
            or transaction_id != marker.transaction_id
        ):
            raise CalendarSnapshotRevisionChangedError(
                "calendar snapshot connection or transaction watermark changed"
            )

    def _execute(self, statement):
        connection = self._snapshot_connection
        if connection is None:
            raise CalendarSnapshotRevisionChangedError(
                "calendar snapshot connection is not pinned"
            )
        return self.session.execute(statement, bind_arguments={"bind": connection})

    def _bounded_rows(self, statement, *, limit: int, resource: str):
        rows = self._execute(statement.limit(limit + 1)).all()
        if len(rows) > limit:
            raise CalendarPreflightResourceLimitExceededError(
                f"calendar snapshot {resource} row limit exceeded",
                details={"resource": resource, "observed": len(rows), "limit": limit},
            )
        # ORM entity selects return one-element Row objects while the
        # metadata-only resolution-head select intentionally returns a
        # multi-column Row.  Normalize only the former so callers cannot
        # accidentally inspect Row.source instead of the model's source.
        return [row[0] if len(row._mapping) == 1 else row for row in rows]

    @staticmethod
    def _index_coverage(
        cells: Sequence[_SnapshotIndexCell], ids: tuple[str, ...]
    ) -> dict[str, object]:
        by_calendar: dict[str, list[date]] = {cid: [] for cid in ids}
        for cell in cells:
            by_calendar.setdefault(cell.calendar_id, []).append(cell.session_date)
        per_calendar: dict[str, object] = {}
        for cid in ids:
            days = sorted(set(by_calendar.get(cid, ())))
            gaps: list[tuple[str, str]] = []
            if days:
                # Track the next natural day so each gap stays half-open.
                cursor = days[0] + timedelta(days=1)
                for day in days[1:]:
                    if day != cursor:
                        gaps.append((cursor.isoformat(), day.isoformat()))
                    cursor = day + timedelta(days=1)
                floor, ceiling = days[0], days[-1] + timedelta(days=1)
            else:
                floor = ceiling = None
            per_calendar[cid] = {
                "range": [floor.isoformat() if floor else None, ceiling.isoformat() if ceiling else None],
                "gaps": gaps,
            }
        floors = [
            date.fromisoformat(value["range"][0])
            for value in per_calendar.values()
            if value["range"][0]
        ]
        ceilings = [
            date.fromisoformat(value["range"][1])
            for value in per_calendar.values()
            if value["range"][1]
        ]
        common_floor = max(floors) if floors else None
        common_ceiling = min(ceilings) if ceilings else None
        common_gaps: list[tuple[str, str]] = []
        if common_floor is not None and common_ceiling is not None:
            missing: set[date] = set()
            for cid in ids:
                dates = set(by_calendar.get(cid, ()))
                cursor = common_floor
                while cursor < common_ceiling:
                    if cursor not in dates:
                        missing.add(cursor)
                    cursor += timedelta(days=1)
            for day in sorted(missing):
                if common_gaps and date.fromisoformat(common_gaps[-1][1]) == day:
                    common_gaps[-1] = (common_gaps[-1][0], (day + timedelta(days=1)).isoformat())
                else:
                    common_gaps.append((day.isoformat(), (day + timedelta(days=1)).isoformat()))
        segments: list[tuple[str, str]] = []
        if common_floor is not None and common_ceiling is not None:
            cursor = common_floor
            for start_text, end_text in common_gaps:
                start, end = date.fromisoformat(start_text), date.fromisoformat(end_text)
                if cursor < start:
                    segments.append((cursor.isoformat(), start.isoformat()))
                cursor = max(cursor, end)
            if cursor < common_ceiling:
                segments.append((cursor.isoformat(), common_ceiling.isoformat()))
        return {
            "by_calendar": per_calendar,
            "common": {
                "floor": common_floor.isoformat() if common_floor else None,
                "ceiling": common_ceiling.isoformat() if common_ceiling else None,
                "gaps": common_gaps,
                "segments": segments,
            },
        }

    @staticmethod
    def _index_payload(
        cells: Sequence[_SnapshotIndexCell],
        ids: Sequence[str],
        *,
        start: date | None = None,
        end_exclusive: date | None = None,
    ) -> dict[str, object]:
        by_day: dict[date, list[_SnapshotIndexCell]] = {}
        for cell in cells:
            if start is not None and cell.session_date < start:
                continue
            if end_exclusive is not None and cell.session_date >= end_exclusive:
                continue
            by_day.setdefault(cell.session_date, []).append(cell)
        rows = []
        for day in sorted(by_day):
            cells_for_day = {cell.calendar_id: cell for cell in by_day[day]}
            rows.append(
                {
                    "date": day.isoformat(),
                    "is_open_by_calendar": {
                        cid: cells_for_day[cid].is_open if cid in cells_for_day else None
                        for cid in ids
                    },
                    "selected_fact_ids": {
                        cid: (
                            str(cells_for_day[cid].selected_fact_id)
                            if cid in cells_for_day and cells_for_day[cid].selected_fact_id
                            else None
                        )
                        for cid in ids
                    },
                    "fact_versions": {
                        cid: (
                            cells_for_day[cid].fact_version
                            if cid in cells_for_day
                            else None
                        )
                        for cid in ids
                    },
                }
            )
        return {"dates": rows}

    def _read_index(
        self,
        request: CalendarSnapshotRequest,
        context: CalendarPITContext,
    ) -> tuple[tuple[_SnapshotIndexCell, ...], str]:
        """Execute the sole prepare SQL query against resolution heads."""

        end_exclusive = request.formal_end + timedelta(days=1)
        index_start = (
            request.formal_start - timedelta(days=10_000)
            if request.warmup_sessions > 0
            else request.formal_start
        )
        # Resolution heads are PIT facts too.  In historical-cognition mode
        # the selected head must be visible at the requested knowledge
        # instant; using the physical data cutoff here could select a newer
        # head than the subsequent payload resolver is allowed to use.
        cutoff = context.knowledge_as_of or context.data_cutoff
        statement = select(
            CalendarResolutionHeadRecord.calendar_id,
            CalendarResolutionHeadRecord.effective_date,
            CalendarResolutionHeadRecord.is_open,
            CalendarResolutionHeadRecord.selected_fact_id,
            CalendarResolutionHeadRecord.selected_fact_version,
            CalendarResolutionHeadRecord.revision_digest,
        ).where(
            CalendarResolutionHeadRecord.calendar_id.in_(request.calendar_ids),
            CalendarResolutionHeadRecord.effective_date >= index_start,
            CalendarResolutionHeadRecord.effective_date < end_exclusive,
            # A resolution head is itself PIT evidence.  A head that has not
            # become known by the request cutoff cannot prove an open/closed
            # natural day, even when its selected fact is already present.
            CalendarResolutionHeadRecord.knowledge_from.is_not(None),
            CalendarResolutionHeadRecord.knowledge_from <= cutoff,
            or_(
                CalendarResolutionHeadRecord.knowledge_to.is_(None),
                CalendarResolutionHeadRecord.knowledge_to > cutoff,
            ),
            # A pending/blocked historical reconciliation invalidates the
            # affected date and its adjacency proof.  Excluding the head here
            # turns it into explicit UNKNOWN_COVERAGE rather than letting a
            # stale current head masquerade as complete.
            ~exists(
                select(CalendarReconciliationRangeRecord.id).where(
                    CalendarReconciliationRangeRecord.calendar_id
                    == CalendarResolutionHeadRecord.calendar_id,
                    CalendarReconciliationRangeRecord.status.in_(
                        ("pending", "running", "blocked")
                    ),
                    CalendarReconciliationRangeRecord.range_start
                    <= CalendarResolutionHeadRecord.effective_date,
                    CalendarReconciliationRangeRecord.range_end
                    > CalendarResolutionHeadRecord.effective_date,
                )
            ),
        )
        rows = self._bounded_rows(
            statement.order_by(
                CalendarResolutionHeadRecord.effective_date,
                CalendarResolutionHeadRecord.calendar_id,
            ),
            limit=self.max_index_rows,
            resource="index",
        )
        cells: list[_SnapshotIndexCell] = []
        seen: set[tuple[str, date]] = set()
        watermarks: set[str] = set()
        for row in rows:
            values = row._mapping
            cid = normalize_calendar_id(values["calendar_id"])
            day = values["effective_date"]
            key = (cid, day)
            if key in seen:
                raise ProviderContractViolationError(
                    "calendar resolution index contains duplicate natural-day cells"
                )
            seen.add(key)
            watermark = values["revision_digest"]
            if (
                not isinstance(watermark, str)
                or len(watermark) != 64
                or any(ch not in "0123456789abcdef" for ch in watermark)
            ):
                raise CalendarSnapshotCoverageUnknownError(
                    "calendar resolution index has no valid revision watermark"
                )
            watermarks.add(watermark)
            selected_id = values["selected_fact_id"]
            selected_version = values["selected_fact_version"]
            if selected_id is None or selected_version is None:
                raise CalendarSnapshotCoverageUnknownError(
                    "calendar resolution index cell has no selected explicit day fact",
                    details={"calendar_id": cid, "date": day.isoformat()},
                )
            cells.append(
                _SnapshotIndexCell(
                    calendar_id=cid,
                    session_date=day,
                    is_open=bool(values["is_open"]),
                    selected_fact_id=selected_id,
                    fact_version=selected_version,
                    revision_watermark=watermark,
                )
            )
        if not cells or not watermarks:
            raise CalendarSnapshotCoverageUnknownError(
                "calendar resolution index cannot prove a revision watermark",
                details={"watermarks": sorted(watermarks)},
            )
        # Different natural-day slices may legitimately point at different
        # immutable source revisions.  The attempt watermark is the canonical
        # digest of all selected head revision digests, not a lexical choice
        # of one row's revision string.
        watermark = canonical_hash(sorted(watermarks))
        by_day = {(cell.session_date, cell.calendar_id): cell for cell in cells}
        formal_days = _iterate_days(request.formal_start, request.formal_end)
        missing_formal = [
            (day.isoformat(), cid)
            for day in formal_days
            for cid in request.calendar_ids
            if (day, cid) not in by_day
        ]
        if missing_formal:
            raise CalendarSnapshotCoverageUnknownError(
                "calendar resolution index has an uncovered formal day",
                details={"missing": missing_formal[:32]},
            )
        index = {
            day: {cid: by_day[(day, cid)] for cid in request.calendar_ids if (day, cid) in by_day}
            for day in sorted({cell.session_date for cell in cells})
        }
        anchor = _find_common_anchor(index, request.calendar_ids, formal_days)
        if anchor is None:
            # Every formal cell is present.  If all are closed this is a
            # proved NO_FORMAL_SESSIONS case; otherwise the index is corrupt.
            if not all(not by_day[(day, cid)].is_open for day in formal_days for cid in request.calendar_ids):
                raise CalendarSnapshotCoverageUnknownError(
                    "calendar resolution index cannot distinguish an uncovered formal candidate"
                )
        return tuple(cells), watermark

    def prepare_calendar_snapshot(self, request: CalendarSnapshotRequest) -> CalendarSnapshotPlan:
        """Prepare one metadata-only index plan; no definition/fact SQL occurs."""

        if not isinstance(request, CalendarSnapshotRequest):
            raise InvalidDataRequestError("request must be a CalendarSnapshotRequest")
        self.prepare_calls += 1
        marker = self._start_snapshot_transaction()
        context = CalendarPITContext.from_query_boundary(
            request.query_boundary, CALENDAR_TIMEZONE_ASIA_SHANGHAI
        )
        cells, watermark = self._read_index(request, context)
        index = {
            day: {
                cid: cell
                for cell in cells
                if cell.session_date == day and (cid := cell.calendar_id) in request.calendar_ids
            }
            for day in sorted({cell.session_date for cell in cells})
        }
        formal_days = _iterate_days(request.formal_start, request.formal_end)
        anchor = _find_common_anchor(index, request.calendar_ids, formal_days)
        envelope_start = request.formal_start
        warmup_dates: tuple[date, ...] = ()
        if request.warmup_sessions > 0 and anchor is not None:
            history = _find_common_open_history(
                index, request.calendar_ids, anchor, request.warmup_sessions
            )
            if len(history) < request.warmup_sessions:
                raise CalendarSnapshotCoverageUnknownError(
                    "calendar resolution index cannot prove enough warmup sessions",
                    details={
                        "cause_code": "warmup_coverage_insufficient",
                        "requested_sessions": request.warmup_sessions,
                        "actual_sessions": len(history),
                    },
                )
            warmup_dates = tuple(history)
            envelope_start = warmup_dates[0]
            if (anchor - envelope_start).days > 10_000:
                raise CalendarSnapshotCoverageUnknownError(
                    "warmup index proof exceeds the natural-day search limit",
                    details={"cause_code": "calendar_date_span_limit_exceeded"},
                )
        coverage = self._index_coverage(cells, request.calendar_ids)
        return CalendarSnapshotPlan(
            request=request,
            context=context,
            envelope_start=envelope_start,
            envelope_end_exclusive=request.formal_end + timedelta(days=1),
            anchor_candidate=anchor,
            warmup_dates=warmup_dates,
            coverage=coverage,
            revision_watermark=watermark,
            transaction=marker,
            open_session_index=self._index_payload(cells, request.calendar_ids),
            index_cells=cells,
        )

    def _load_batch(self, request: CalendarSnapshotRequest, plan: CalendarSnapshotPlan) -> _BatchCalendarData:
        """Read all payload tables once, using set predicates and one pin."""

        ids = request.calendar_ids
        start, end = plan.envelope_start, plan.envelope_end_exclusive
        context = plan.context
        visible = lambda model: (
            model.quality_status == "accepted",
            model.known_at <= context.data_cutoff,
        )
        registry_rows = self._bounded_rows(
            select(CalendarRegistryRecord).where(
                CalendarRegistryRecord.calendar_id.in_(ids),
                CalendarRegistryRecord.valid_from < end,
                or_(CalendarRegistryRecord.valid_to.is_(None), CalendarRegistryRecord.valid_to > start),
                *visible(CalendarRegistryRecord),
            ),
            limit=self.max_session_rows,
            resource="registry",
        )
        definition_rows = self._bounded_rows(
            select(CalendarDefinitionRecord).where(
                CalendarDefinitionRecord.calendar_id.in_(ids),
                CalendarDefinitionRecord.valid_from < end,
                or_(CalendarDefinitionRecord.valid_to.is_(None), CalendarDefinitionRecord.valid_to > start),
                *visible(CalendarDefinitionRecord),
            ),
            limit=self.max_session_rows,
            resource="definition",
        )
        fact_rows = self._bounded_rows(
            select(CalendarSessionFactRecord).where(
                CalendarSessionFactRecord.calendar_id.in_(ids),
                CalendarSessionFactRecord.session_date >= start,
                CalendarSessionFactRecord.session_date < end,
                *visible(CalendarSessionFactRecord),
            ),
            limit=self.max_session_rows,
            resource="session",
        )
        binding_rows = self._bounded_rows(
            select(CalendarExchangeBindingRecord).where(
                CalendarExchangeBindingRecord.canonical_calendar_id.in_(ids),
                CalendarExchangeBindingRecord.valid_from < end,
                or_(CalendarExchangeBindingRecord.valid_to.is_(None), CalendarExchangeBindingRecord.valid_to > start),
                *visible(CalendarExchangeBindingRecord),
            ),
            limit=self.max_session_rows,
            resource="binding",
        )
        scope_predicates = [
            and_(
                CalendarCapabilityDeclarationRecord.scope_kind == "provider",
                CalendarCapabilityDeclarationRecord.scope_key.in_(
                    (f"provider:{request.provider_key or self.provider_key}",)
                ),
            ),
            and_(
                CalendarCapabilityDeclarationRecord.scope_kind == "calendar",
                CalendarCapabilityDeclarationRecord.scope_key.in_(
                    tuple(f"calendar:{calendar_id}" for calendar_id in ids)
                ),
            ),
        ]
        if request.package_key is not None:
            scope_predicates.append(
                and_(
                    CalendarCapabilityDeclarationRecord.scope_kind == "rule_package",
                    CalendarCapabilityDeclarationRecord.scope_key.in_(
                        (
                            f"rule_package:{request.package_key}@{request.package_version}",
                        )
                    ),
                )
            )
        if request.instrument_ids:
            scope_predicates.append(
                and_(
                    CalendarCapabilityDeclarationRecord.scope_kind == "instrument",
                    CalendarCapabilityDeclarationRecord.scope_key.in_(
                        tuple(f"instrument:{instrument_id}" for instrument_id in request.instrument_ids)
                    ),
                )
            )
        capability_rows = self._bounded_rows(
            select(CalendarCapabilityDeclarationRecord).where(
                # Capability declarations are single-scope facts.  Restrict
                # the batch to the frozen canonical scopes needed by this
                # request; loading the table-wide set would violate the
                # set-based query boundary and leak unrelated declarations.
                or_(*scope_predicates),
                CalendarCapabilityDeclarationRecord.valid_from < end,
                or_(CalendarCapabilityDeclarationRecord.valid_to.is_(None), CalendarCapabilityDeclarationRecord.valid_to > start),
                *visible(CalendarCapabilityDeclarationRecord),
            ),
            limit=self.max_session_rows,
            resource="capability",
        )
        sources = tuple(sorted({row.source for row in (*registry_rows, *definition_rows, *fact_rows, *binding_rows, *capability_rows)}))
        if len(sources) > self.max_source_rows:
            raise CalendarPreflightResourceLimitExceededError(
                "calendar snapshot source count exceeds its resource limit",
                details={"resource": "source", "observed": len(sources), "limit": self.max_source_rows},
            )
        priority_rows = self._bounded_rows(
            select(CalendarSourcePriorityRecord).where(
                CalendarSourcePriorityRecord.source.in_(sources)
                if sources
                else CalendarSourcePriorityRecord.source == "__no_calendar_source__",
                CalendarSourcePriorityRecord.valid_from < end,
                or_(CalendarSourcePriorityRecord.valid_to.is_(None), CalendarSourcePriorityRecord.valid_to > start),
                *visible(CalendarSourcePriorityRecord),
            ),
            limit=self.max_source_rows,
            resource="source_priority",
        )
        return _BatchCalendarData(
            tuple(_definition(row) for row in definition_rows),
            tuple(_session(row) for row in fact_rows),
            registries=tuple(_registry(row) for row in registry_rows),
            bindings=tuple(_binding(row) for row in binding_rows),
            capabilities=tuple(_capability(row) for row in capability_rows),
            source_priorities=tuple(_priority(row) for row in priority_rows),
        )

    @staticmethod
    def _assert_loaded_index(
        plan: CalendarSnapshotPlan,
        loaded_index: Mapping[date, Mapping[str, object]],
    ) -> None:
        expected = {
            (cell.session_date, cell.calendar_id): cell
            for cell in plan.index_cells
            if plan.envelope_start <= cell.session_date < plan.envelope_end_exclusive
        }
        for key, cell in expected.items():
            outcome = loaded_index.get(key[0], {}).get(key[1])
            if outcome is None:
                raise CalendarSnapshotRevisionChangedError(
                    "batch read no longer covers the prepare index envelope"
                )
            fact = getattr(outcome, "fact", None)
            if getattr(outcome, "is_open", None) is not cell.is_open:
                raise CalendarSnapshotRevisionChangedError(
                    "batch read open-session index differs from prepare"
                )
            if (
                getattr(fact, "fact_id", None) != cell.selected_fact_id
                or getattr(fact, "fact_version", None) != cell.fact_version
            ):
                raise CalendarSnapshotRevisionChangedError(
                    "batch read selected fact differs from prepare watermark"
                )

    def load_calendar_snapshot(self, plan: CalendarSnapshotPlan) -> CalendarSnapshot:
        """Perform one batch payload read; all later work is in memory."""

        if not isinstance(plan, CalendarSnapshotPlan):
            raise InvalidDataRequestError("plan must be a CalendarSnapshotPlan")
        self._assert_snapshot_transaction(plan.transaction)
        self.batch_read_calls += 1
        batch = self._load_batch(plan.request, plan)
        loaded_index = _materialize_calendar_index(
            batch,
            plan.request.calendar_ids,
            plan.envelope_start,
            plan.envelope_end_exclusive,
            plan.context,
        )
        self._assert_loaded_index(plan, loaded_index)
        formal_days = _iterate_days(plan.request.formal_start, plan.request.formal_end)
        actual_anchor = _find_common_anchor(loaded_index, plan.request.calendar_ids, formal_days)
        if actual_anchor != plan.anchor_candidate:
            raise CalendarSnapshotRevisionChangedError(
                "batch read anchor differs from prepare index"
            )
        resolution, warmup = _resolve_snapshot_range(
            batch,
            plan.request.calendar_ids,
            plan.request.formal_start,
            plan.request.formal_end,
            plan.envelope_start,
            plan.envelope_end_exclusive,
            plan.context,
            plan.request.instrument_ids,
            index=loaded_index,
            warmup_count=plan.request.warmup_sessions,
        )
        coverage = _coverage_payload(
            batch,
            plan.request.calendar_ids,
            plan.envelope_start,
            plan.envelope_end_exclusive,
            plan.context,
            index=loaded_index,
        )
        # The prepare watermark guards the metadata index.  Once the bounded
        # payload batch is loaded, compute the canonical full revision digest
        # from that same immutable batch so SQL and memory snapshots expose
        # the identical audit hash without rereading facts in prepare.
        batch_revision = canonical_hash(
            _revision_payload(
                batch,
                plan.request.calendar_ids,
                plan.envelope_start,
                plan.envelope_end_exclusive,
                plan.context,
            )
        )
        semantic_signature = _semantic_signature(
            resolution,
            plan.request.formal_start,
            plan.request.formal_end,
        )
        session_signature = _session_signature(
            resolution,
            plan.request.formal_start,
            plan.request.formal_end,
            batch_revision,
            plan.context,
        )
        warmup_signature = _warmup_signature(
            warmup,
            batch_revision,
            plan.anchor_candidate,
            plan.request.warmup_sessions,
        )
        resolution = resolution.__class__(
            policy_key=resolution.policy_key,
            policy_version=resolution.policy_version,
            start_date=resolution.start_date,
            end_date=resolution.end_date,
            calendar_ids=resolution.calendar_ids,
            session_signature=session_signature,
            timezone=resolution.timezone,
            resolved_sessions=resolution.resolved_sessions,
            status=resolution.status,
            differences=resolution.differences,
            pit_context=plan.context,
            selected_facts=resolution.selected_facts,
            resolved_calendar_definitions=resolution.resolved_calendar_definitions,
            calendar_semantic_signature=semantic_signature,
            calendar_revision_digest=batch_revision,
            warmup_sessions=warmup,
            warmup_session_signature=warmup_signature,
            coverage_envelope=coverage,
            non_strict_pit_capabilities=resolution.non_strict_pit_capabilities,
            non_strict_pit=resolution.non_strict_pit,
        )
        from app.backtesting.calendar_axis import _snapshot_request_payload

        payload = {
            "request": _snapshot_request_payload(plan.request),
            "pit_context": dict(plan.context.as_dict),
            "envelope": {
                "start_date": plan.envelope_start,
                "end_date_exclusive": plan.envelope_end_exclusive,
            },
            "calendar_revision_digest": batch_revision,
            "open_session_index": self._index_payload(
                plan.index_cells,
                plan.request.calendar_ids,
                start=plan.envelope_start,
                end_exclusive=plan.envelope_end_exclusive,
            ),
            "calendar_semantic_signature": semantic_signature,
            "calendar_session_signature": session_signature,
            "warmup_session_signature": warmup_signature,
            "coverage": coverage,
            "protocol_version": "calendar_snapshot@1",
        }
        binding_selections: dict[str, object] = {}
        for calendar_id in plan.request.calendar_ids:
            registry = batch.resolve_registry(
                calendar_id,
                effective_day=plan.request.formal_start,
                pit_context=plan.context,
            )
            alias_rows = [
                row
                for row in batch._bindings
                if row.canonical_calendar_id == calendar_id
                and row.alias == calendar_id
                and row.applies_to(plan.request.formal_start)
            ]
            if not alias_rows:
                binding_selections[calendar_id] = {
                    "alias": None,
                    "selected_fact_id": None,
                    "fact_version": None,
                    "binding_version": None,
                    "registry_fact_id": registry.fact_id,
                    "registry_version": registry.registry_version,
                    "missing_reason": "canonical_binding_not_required",
                }
                continue
            binding = select_pit_candidate(
                alias_rows,
                effective_day=plan.request.formal_start,
                pit_context=plan.context,
                source_priorities=batch.source_priorities(),
                missing_code="calendar_binding_missing",
                ambiguous_code="calendar_binding_ambiguous",
            )
            assert isinstance(binding, CalendarExchangeBinding)
            if (
                binding.registry_fact_id != registry.fact_id
                or binding.registry_version != registry.registry_version
            ):
                raise ProviderContractViolationError(
                    "binding registry reference does not match selected registry"
                )
            binding_selections[calendar_id] = {
                "alias": binding.alias,
                "selected_fact_id": binding.fact_id,
                "fact_version": binding.fact_version,
                "binding_version": binding.binding_version,
                "registry_fact_id": binding.registry_fact_id,
                "registry_version": binding.registry_version,
            }
        payload["registry_selection"] = {
            calendar_id: {
                "fact_id": registry.fact_id,
                "registry_version": registry.registry_version,
            }
            for calendar_id, registry in (
                (
                    calendar_id,
                    batch.resolve_registry(
                        calendar_id,
                        effective_day=plan.request.formal_start,
                        pit_context=plan.context,
                    ),
                )
                for calendar_id in plan.request.calendar_ids
            )
        }
        payload["binding_selection"] = binding_selections
        fingerprint = canonical_hash(payload)
        snapshot = CalendarSnapshot(
            snapshot_id=uuid4(),
            request=plan.request,
            pit_context=plan.context,
            resolution=resolution,
            warmup_sessions=warmup,
            envelope_start=plan.envelope_start,
            envelope_end_exclusive=plan.envelope_end_exclusive,
            coverage=coverage,
            revision_watermark=batch_revision,
            snapshot_fingerprint=fingerprint,
            attempt_id=plan.attempt_id,
            open_session_index=payload["open_session_index"],
            resolved_calendar_definitions=_selected_snapshot_definitions(
                batch,
                plan.request.calendar_ids,
                resolution,
                plan.envelope_start,
                plan.envelope_end_exclusive,
                plan.context,
            ),
            resolved_calendar_bindings=binding_selections,
        )
        self._last_batch = batch
        return snapshot

    def open_calendar_snapshot(
        self,
        request: CalendarSnapshotRequest | object,
        *,
        query_boundary: object | None = None,
    ) -> CalendarSnapshot:
        """Open atomically, retrying one whole attempt at most once."""

        if not isinstance(request, CalendarSnapshotRequest):
            request = _snapshot_request_from_object(request, query_boundary=query_boundary)
        elif query_boundary is not None and query_boundary != request.query_boundary:
            raise InvalidDataRequestError("snapshot query_boundary must match the request boundary")
        last_failure: CalendarContractError | None = None
        try:
            for attempt in range(2):
                try:
                    plan = self.prepare_calendar_snapshot(request)
                    snapshot = self.load_calendar_snapshot(plan)
                    self._snapshots[snapshot.snapshot_id] = snapshot
                    self.session.commit()
                    return snapshot
                except DBAPIError as exc:
                    self.session.rollback()
                    dialect = self.session.get_bind().dialect
                    if not _is_retryable_dbapi_error(exc, dialect=dialect):
                        raise ProviderContractViolationError(
                            "calendar snapshot database operation failed",
                            details={
                                "cause_type": type(exc.orig).__name__,
                                "dialect": dialect.name,
                            },
                        ) from exc
                    last_failure = CalendarSnapshotRevisionChangedError(
                        "calendar snapshot transaction failed before a stable watermark was obtained"
                    )
                except CalendarSnapshotRevisionChangedError:
                    self.session.rollback()
                # Revision mismatches are stable evidence from this attempt
                # (including prepare/load index, anchor, and transaction
                # watermark checks).  Retrying them would repeat the same
                # semantic failure and could hide the original cause behind
                # ``calendar_snapshot_retry_exhausted``; only DBAPI failures
                # below are eligible for the existing transient retry path.
                    raise
                except CalendarContractError:
                    self.session.rollback()
                    raise
                except Exception:
                    # Ensure unexpected query failures cannot leave a short
                    # transaction/connection pinned to this provider.
                    self.session.rollback()
                    raise
                if attempt == 1:
                    raise CalendarSnapshotRetryExhaustedError(
                        "calendar snapshot retry exhausted after one complete retry",
                        details={"attempts": 2, "cause_code": getattr(last_failure, "code", "calendar_snapshot_revision_changed")},
                    ) from last_failure
                self._snapshot_connection = None
                self._snapshot_transaction = None
            raise AssertionError("unreachable")
        finally:
            self._snapshot_connection = None
            self._snapshot_transaction = None

    def snapshot(self, snapshot_id: UUID) -> CalendarSnapshot | None:
        return self._snapshots.get(snapshot_id)

    # Legacy diagnostic reads remain available, but strict snapshot resolution
    # never calls them.  After a successful batch they read the immutable
    # batch, not the database, so formal/warmup cannot drift.
    def definitions(self, calendar_id: str) -> tuple[CalendarDefinition, ...]:
        canonical = normalize_calendar_id(calendar_id)
        if self._last_batch is not None:
            return self._last_batch.definitions(canonical)
        rows = self.session.scalars(
            select(CalendarDefinitionRecord).where(
                CalendarDefinitionRecord.calendar_id == canonical
            )
        ).all()
        return tuple(_definition(row) for row in rows)

    def fact_candidates(self, calendar_id: str, day: date) -> tuple[CalendarSessionFact, ...]:
        canonical = normalize_calendar_id(calendar_id)
        self.fact_calls += 1
        if self._last_batch is not None:
            return self._last_batch.fact_candidates(canonical, day)
        rows = self.session.scalars(
            select(CalendarSessionFactRecord).where(
                CalendarSessionFactRecord.calendar_id == canonical,
                CalendarSessionFactRecord.session_date == day,
            )
        ).all()
        return tuple(_session(row) for row in rows)

    def fact(self, calendar_id: str, day: date) -> CalendarSessionFact | None:
        candidates = self.fact_candidates(calendar_id, day)
        return candidates[-1] if candidates else None

    def registries(self, calendar_id: str) -> tuple[CalendarRegistry, ...]:
        canonical = normalize_calendar_id(calendar_id)
        if self._last_batch is not None:
            return self._last_batch.registries(canonical)
        rows = self.session.scalars(
            select(CalendarRegistryRecord).where(
                CalendarRegistryRecord.calendar_id == canonical
            )
        ).all()
        return tuple(_registry(row) for row in rows)

    def bindings(self, alias: str) -> tuple[CalendarExchangeBinding, ...]:
        if self._last_batch is not None:
            return self._last_batch.bindings(alias)
        rows = self.session.scalars(
            select(CalendarExchangeBindingRecord).where(
                CalendarExchangeBindingRecord.alias == alias.strip().upper()
            )
        ).all()
        return tuple(_binding(row) for row in rows)

    def source_priorities(self) -> tuple[CalendarSourcePriority, ...]:
        if self._last_batch is not None:
            return self._last_batch.source_priorities()
        rows = self.session.scalars(select(CalendarSourcePriorityRecord)).all()
        return tuple(_priority(row) for row in rows)

    def resolve_capability(
        self,
        capability: str,
        *,
        effective_day: date,
        pit_context: CalendarPITContext | None = None,
        provider_key: str | None = None,
        package_key: str | None = None,
        package_version: int | str | None = None,
        calendar_id: str | None = None,
        instrument_id: UUID | None = None,
    ) -> CapabilityResolution:
        """Resolve a declaration from the immutable post-batch payload.

        Strict session preflight invokes this method after ``load``.  Reading
        the detached batch keeps SQL and memory capability evidence on the
        same snapshot and avoids an out-of-band declaration query.
        """

        declarations = self._last_batch.capabilities() if self._last_batch is not None else ()
        priorities = self._last_batch.source_priorities() if self._last_batch is not None else self.source_priorities()
        return select_capability_declaration(
            declarations,
            capability=capability,
            effective_day=effective_day,
            pit_context=pit_context,
            provider_key=provider_key,
            package_key=package_key,
            package_version=package_version,
            calendar_id=calendar_id,
            instrument_id=instrument_id,
            source_priorities=priorities,
        )


# Public compatibility aliases used by existing integrations.
SnapshotPlan = CalendarSnapshotPlan
SqlCalendarProvider = SqlCalendarAxisDataProvider

__all__ = [
    "CalendarSnapshotPlan",
    "SnapshotPlan",
    "SqlCalendarAxisDataProvider",
    "SqlCalendarProvider",
    "MAX_SNAPSHOT_INDEX_ROWS",
    "MAX_SNAPSHOT_SESSION_ROWS",
    "MAX_SNAPSHOT_SOURCE_ROWS",
]
