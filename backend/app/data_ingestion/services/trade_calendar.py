"""Trading calendar fetching, yearly planning, and incremental synchronization."""

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from collections.abc import Mapping
from uuid import UUID
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.orm import Session
import structlog

from app.data_ingestion.clients.tushare import TushareClient
from app.data_ingestion.repositories.sync_checkpoint import DataSyncCheckpointRepository
from app.data_ingestion.repositories.trading_calendar import TradingCalendarRepository
from app.data_ingestion.request_pacing import tushare_request_pacer
from app.data_ingestion.schemas.trading_calendar import (
    DataSyncCheckpointState,
    TradeCalendarDateRange,
    TradeCalendarSyncResult,
    TradeCalendarUpsertResult,
    TradingCalendarDayInput,
)
from app.core.config import get_settings
from app.data_ingestion.constants import (
    LEGACY_TRADE_CALENDAR_BACKFILL_SYNC_KEY,
    TRADE_CALENDAR_SYNC_KEY,
    TUSHARE_SOURCE,
)
from app.backtesting.calendar_axis import (
    CalendarDefinition,
    CalendarExchangeBinding,
    CalendarPITContext,
    CalendarRegistry,
    CalendarSourcePriority,
    CalendarSessionFact,
    _select_source_priority,
    normalize_calendar_id,
    select_pit_candidate,
)
from app.backtesting.data.requests import QueryBoundary
from app.backtesting.data.calendar_repository import CalendarFactRepository
from app.backtesting.calendar_models import CalendarReconciliationRangeRecord, CalendarSessionFactRecord
from app.backtesting.data.errors import LegacyExchangeAmbiguousError, CalendarIngestionRangeIncompleteError
from app.db.session import get_engine

if TYPE_CHECKING:
    from pandas import DataFrame


def fetch_trade_calendar(
    client: TushareClient,
    *,
    start_date: str,
    end_date: str,
    exchange: str = "",
) -> "DataFrame":
    """Fetch the Tushare trading calendar for the requested date range."""
    return client.pro.trade_cal(
        exchange=exchange,
        start_date=start_date,
        end_date=end_date,
    )


SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
logger = structlog.get_logger(__name__)


def sync_trade_calendar(
    client: TushareClient,
    *,
    exchange: str,
    initial_start_date: date,
    request_interval_ms: int | None = None,
    as_of_date: date | None = None,
) -> TradeCalendarSyncResult:
    """Incrementally synchronize one exchange's calendar in yearly requests."""
    settings = get_settings()
    effective_interval_ms = max(
        settings.ingestion_request_interval_ms,
        request_interval_ms or 0,
    )
    calendar_id = resolve_calendar_id(
        exchange,
        effective_day=initial_start_date,
        # Ingestion's source cutoff is independent from a backtest
        # QueryBoundary; it is used only to select the persisted binding.
        data_cutoff=datetime.now(timezone.utc),
    )
    # The checkpoint is scoped by canonical calendar identity.  The source API
    # still receives the caller's exchange alias for compatibility, but that
    # alias never becomes a fact or cache key.
    scope_key = f"calendar_id={calendar_id}"
    checkpoint = _load_checkpoint(TRADE_CALENDAR_SYNC_KEY, scope_key)
    ranges = plan_trade_calendar_year_ranges(
        checkpoint=checkpoint,
        initial_start_date=initial_start_date,
        as_of_date=as_of_date or datetime.now(SHANGHAI_TIMEZONE).date(),
    )
    checkpoint_before = (
        _checkpoint_synced_through_date(checkpoint).isoformat()
        if checkpoint is not None
        else None
    )
    logger.info(
        "trade_calendar_sync_planned",
        message=(
            f"交易日历采集计划：{exchange}，{ranges[0].start_date.isoformat() if ranges else initial_start_date.isoformat()} 至 "
            f"{ranges[-1].end_date.isoformat() if ranges else (as_of_date or datetime.now(SHANGHAI_TIMEZONE).date()).isoformat()}，"
            f"共 {len(ranges)} 个分段，"
            f"拉取 0 条，变更 0 条，未变更 0 条，失败 0 条，checkpoint 未推进。"
        ),
        title="交易日历采集计划",
        exchange=calendar_id,
        data_type="trading_calendar",
        calendar_id=calendar_id,
        start_date=ranges[0].start_date.isoformat() if ranges else initial_start_date.isoformat(),
        end_date=ranges[-1].end_date.isoformat() if ranges else (as_of_date or datetime.now(SHANGHAI_TIMEZONE).date()).isoformat(),
        fetched_count=0,
        changed_count=0,
        unchanged_count=0,
        failed_count=0,
        checkpoint_scope=scope_key,
        checkpoint_before=checkpoint_before,
        checkpoint_after=checkpoint_before,
        checkpoint_advanced=False,
        source=TUSHARE_SOURCE,
        source_revision=None,
        reconciliation_range=None,
        ranges_planned=len(ranges),
        request_interval_ms=effective_interval_ms,
        checkpoint_synced_through=(
            _checkpoint_synced_through_date(checkpoint).isoformat()
            if checkpoint is not None
            else None
        ),
    )

    received = changed = unchanged = 0
    for date_range in ranges:
        range_fields = {
            "exchange": calendar_id,
            "start_date": date_range.start_date.isoformat(),
            "end_date": date_range.end_date.isoformat(),
        }
        range_checkpoint_before = (
            _checkpoint_synced_through_date(checkpoint).isoformat()
            if checkpoint is not None
            else None
        )
        logger.info(
            "trade_calendar_range_started",
            message=(
                f"开始采集 {exchange} 交易日历："
                f"{date_range.start_date.isoformat()} 至 {date_range.end_date.isoformat()}，"
                "拉取 0 条，变更 0 条，未变更 0 条，失败 0 条，checkpoint 未推进。"
            ),
            title="开始交易日历采集",
            **range_fields,
            data_type="trading_calendar",
            calendar_id=calendar_id,
            fetched_count=0,
            changed_count=0,
            unchanged_count=0,
            failed_count=0,
            checkpoint_scope=scope_key,
            checkpoint_before=range_checkpoint_before,
            checkpoint_after=range_checkpoint_before,
            checkpoint_advanced=False,
            source=TUSHARE_SOURCE,
            source_revision=None,
            reconciliation_range=None,
        )
        try:
            tushare_request_pacer.wait_for_turn(effective_interval_ms)
            dataframe = fetch_trade_calendar(
                client,
                exchange=exchange,
                start_date=date_range.start_date.strftime("%Y%m%d"),
                end_date=date_range.end_date.strftime("%Y%m%d"),
            )
            days = normalize_trade_calendar(dataframe, calendar_id=calendar_id)
            _validate_trade_calendar_range(days, exchange=calendar_id, date_range=date_range)
            write_result, checkpoint = _commit_trade_calendar_range(
                days=days,
                sync_key=TRADE_CALENDAR_SYNC_KEY,
                scope_key=scope_key,
                expected_checkpoint=checkpoint,
                synced_through_date=date_range.end_date,
            )
        except Exception:
            logger.exception(
                "trade_calendar_range_failed",
                message=(
                    f"采集 {exchange} 交易日历失败："
                    f"{date_range.start_date.isoformat()} 至 {date_range.end_date.isoformat()}，"
                    "拉取 0 条，变更 0 条，未变更 0 条，失败 1 条，checkpoint 未推进。"
                ),
                title="交易日历采集失败",
                **range_fields,
                data_type="trading_calendar",
                calendar_id=calendar_id,
                fetched_count=0,
                changed_count=0,
                unchanged_count=0,
                failed_count=1,
                checkpoint_scope=scope_key,
                checkpoint_before=range_checkpoint_before,
                checkpoint_after=range_checkpoint_before,
                checkpoint_advanced=False,
                source=TUSHARE_SOURCE,
                source_revision=None,
                reconciliation_range=None,
            )
            raise
        received += write_result.received
        changed += write_result.changed
        unchanged += write_result.unchanged
        logger.info(
            "trade_calendar_range_succeeded",
            message=(
                f"完成采集 {exchange} 交易日历："
                f"{date_range.start_date.isoformat()} 至 {date_range.end_date.isoformat()}，"
                f"拉取 {write_result.received} 条，入库变更 {write_result.changed} 条，"
                f"未变更 {write_result.unchanged} 条，"
                f"失败 0 条，游标已推进至 {checkpoint.cursor['synced_through_date']}。"
            ),
            title="交易日历采集完成",
            **range_fields,
            data_type="trading_calendar",
            calendar_id=calendar_id,
            received=write_result.received,
            fetched_count=write_result.received,
            changed_count=write_result.changed,
            unchanged_count=write_result.unchanged,
            failed_count=0,
            checkpoint_scope=scope_key,
            checkpoint_before=range_checkpoint_before,
            checkpoint_after=checkpoint.cursor["synced_through_date"],
            checkpoint_advanced=True,
            source=TUSHARE_SOURCE,
            source_revision=None,
            reconciliation_range=None,
            changed=write_result.changed,
            unchanged=write_result.unchanged,
            synced_through_date=checkpoint.cursor["synced_through_date"],
        )

    return TradeCalendarSyncResult(
        ranges_completed=len(ranges),
        received=received,
        changed=changed,
        unchanged=unchanged,
        synced_through_date=(
            _checkpoint_synced_through_date(checkpoint) if checkpoint is not None else None
        ),
        start_date=ranges[0].start_date if ranges else None,
        end_date=ranges[-1].end_date if ranges else None,
        calendar_id=calendar_id,
    )


def plan_trade_calendar_year_ranges(
    *,
    checkpoint: DataSyncCheckpointState | None,
    initial_start_date: date,
    as_of_date: date,
) -> list[TradeCalendarDateRange]:
    """Plan inclusive ranges that end at calendar-year boundaries."""
    start_date = (
        _checkpoint_synced_through_date(checkpoint) + timedelta(days=1)
        if checkpoint is not None
        else initial_start_date
    )
    if start_date > as_of_date:
        return []

    ranges: list[TradeCalendarDateRange] = []
    while start_date <= as_of_date:
        end_date = min(date(start_date.year, 12, 31), as_of_date)
        ranges.append(
            TradeCalendarDateRange(start_date=start_date, end_date=end_date)
        )
        start_date = end_date + timedelta(days=1)
    return ranges


def resolve_calendar_id(
    exchange_or_alias: str,
    *,
    effective_day: date | None = None,
    data_cutoff: datetime | None = None,
) -> str:
    """Resolve a legacy exchange only through a persisted binding fact.

    Canonical ``SSE``/``SZSE`` values are already calendar identities and do
    not need an alias conversion.  Every other legacy spelling must resolve
    through the versioned binding/source-priority chain; a hard-coded alias
    dictionary would bypass effective/PIT evidence and is therefore forbidden.
    """

    if not isinstance(exchange_or_alias, str) or not exchange_or_alias.strip():
        raise LegacyExchangeAmbiguousError("exchange alias is empty")
    alias = exchange_or_alias.strip().upper()
    if alias in {"SSE", "SZSE"}:
        return normalize_calendar_id(alias)
    if effective_day is None or data_cutoff is None:
        raise LegacyExchangeAmbiguousError(
            "legacy exchange binding resolution requires explicit effective_day and data_cutoff",
            details={"alias": alias},
        )
    day = effective_day
    cutoff = data_cutoff
    from app.backtesting.data.requests import QueryBoundary

    context = CalendarPITContext.from_query_boundary(
        QueryBoundary(data_cutoff=cutoff, include_cutoff_day=True)
    )
    with Session(get_engine()) as session:
        repository = CalendarFactRepository(session)
        candidates = tuple(
            row for row in repository.list_bindings((alias,)) if row.applies_to(day)
        )
        priorities = repository.list_source_priorities()
    if not candidates:
        raise LegacyExchangeAmbiguousError(
            "exchange alias has no explicit canonical calendar binding",
            details={"alias": alias, "effective_day": day.isoformat()},
        )
    try:
        selected = select_pit_candidate(
            candidates,
            effective_day=day,
            pit_context=context,
            source_priorities=priorities,
            missing_code="calendar_binding_missing",
            ambiguous_code="calendar_binding_ambiguous",
        )
    except Exception as exc:
        raise LegacyExchangeAmbiguousError(
            "exchange alias binding is ambiguous at the requested PIT",
            details={"alias": alias, "effective_day": day.isoformat()},
        ) from exc
    if not isinstance(selected, CalendarExchangeBinding):
        raise LegacyExchangeAmbiguousError("exchange alias did not resolve to a binding fact")
    return selected.canonical_calendar_id


@dataclass(frozen=True, slots=True)
class NamedTradeCalendarSyncContext:
    """PIT-resolved metadata required by the canonical calendar ingester."""

    calendar_id: str
    registry: CalendarRegistry
    definition: CalendarDefinition
    source_priority: CalendarSourcePriority
    source_revision: str


def resolve_named_trade_calendar_context(
    exchange_or_alias: str,
    *,
    effective_day: date,
    data_cutoff: datetime,
) -> NamedTradeCalendarSyncContext:
    """Resolve a legacy exchange through persisted canonical calendar facts.

    The scheduler still accepts its historical ``exchange`` parameter, but
    production ingestion must select every piece of canonical metadata at one
    PIT boundary before writing session facts.  The selected registry and
    definition are checked against the binding's explicit references rather
    than inferred from the alias or a hard-coded exchange map.
    """

    if not isinstance(exchange_or_alias, str) or not exchange_or_alias.strip():
        raise LegacyExchangeAmbiguousError("exchange alias is empty")
    alias = exchange_or_alias.strip().upper()
    context = CalendarPITContext.from_query_boundary(
        QueryBoundary(data_cutoff=data_cutoff, include_cutoff_day=True)
    )
    day = effective_day
    with Session(get_engine()) as session:
        repository = CalendarFactRepository(session)
        source_priorities = repository.list_source_priorities()
        bindings = tuple(
            item for item in repository.list_bindings((alias,)) if item.applies_to(day)
        )
        try:
            selected_binding = select_pit_candidate(
                bindings,
                effective_day=day,
                pit_context=context,
                source_priorities=source_priorities,
                missing_code="calendar_binding_missing",
                ambiguous_code="calendar_binding_ambiguous",
            )
        except Exception as exc:
            raise LegacyExchangeAmbiguousError(
                "exchange alias binding is ambiguous at the requested PIT",
                details={"alias": alias, "effective_day": day.isoformat()},
            ) from exc
        if not isinstance(selected_binding, CalendarExchangeBinding):
            raise LegacyExchangeAmbiguousError("exchange alias did not resolve to a binding fact")

        canonical = normalize_calendar_id(selected_binding.canonical_calendar_id)
        registries = repository.list_registries((canonical,))
        selected_registry = select_pit_candidate(
            registries,
            effective_day=day,
            pit_context=context,
            source_priorities=source_priorities,
            missing_code="calendar_registry_fact_missing",
            ambiguous_code="calendar_registry_ambiguous",
        )
        if not isinstance(selected_registry, CalendarRegistry):
            raise LegacyExchangeAmbiguousError("calendar binding did not resolve to a registry fact")
        if (
            selected_registry.fact_id != selected_binding.registry_fact_id
            or selected_registry.registry_version != selected_binding.registry_version
        ):
            raise LegacyExchangeAmbiguousError(
                "calendar binding registry reference is not PIT-resolved",
                details={"alias": alias, "calendar_id": canonical},
            )

        definitions = repository.list_definitions((canonical,))
        definition_candidates = tuple(
            item
            for item in definitions
            if item.registry_fact_id == selected_registry.fact_id
            and item.registry_version == selected_registry.registry_version
        )
        selected_definition = select_pit_candidate(
            definition_candidates,
            effective_day=day,
            pit_context=context,
            source_priorities=source_priorities,
            missing_code="calendar_definition_missing",
            ambiguous_code="calendar_definition_ambiguous",
        )
        if not isinstance(selected_definition, CalendarDefinition):
            raise LegacyExchangeAmbiguousError("calendar registry did not resolve to a definition fact")

        source_priority = _select_source_priority(
            selected_binding.source,
            source_priorities,
            day=day,
            context=context,
        )
        return NamedTradeCalendarSyncContext(
            calendar_id=canonical,
            registry=selected_registry,
            definition=selected_definition,
            source_priority=source_priority,
            # The source-priority registry is the persisted revision authority
            # used by strict facts; no wall-clock value is used as a revision.
            source_revision=source_priority.source_revision,
        )


def normalize_trade_calendar(
    dataframe: "DataFrame", *, calendar_id: str | None = None
) -> list[TradingCalendarDayInput]:
    """Convert Tushare rows while preserving the raw exchange audit value."""

    resolved = normalize_calendar_id(calendar_id) if calendar_id is not None else None
    return [
        TradingCalendarDayInput(
            exchange=resolved or str(row["exchange"]),
            calendar_date=_parse_date(row["cal_date"]),
            is_open=str(row["is_open"]) == "1",
            previous_trading_date=_parse_optional_date(row.get("pretrade_date")),
            calendar_id=resolved,
        )
        for row in dataframe.to_dict(orient="records")
    ]


def normalize_named_calendar_facts(
    dataframe: "DataFrame",
    *,
    calendar_id: str,
    definition: CalendarDefinition,
    registry: CalendarRegistry,
    source_priority: CalendarSourcePriority,
    source: str,
    source_revision: str,
    observed_at: datetime,
    known_at: datetime,
) -> list[CalendarSessionFact]:
    """Materialize a complete natural-day batch as append-only facts.

    The caller supplies reviewed registry/definition/priority facts.  This
    function never infers sessions from an absent source row and never creates
    a default time window for an open day.
    """

    canonical = normalize_calendar_id(calendar_id)
    if definition.calendar_id != canonical or registry.calendar_id != canonical:
        raise LegacyExchangeAmbiguousError("calendar fact references disagree with its registry")
    if source_priority.source != source:
        raise LegacyExchangeAmbiguousError("calendar fact source has no matching priority registry row")
    observed_at = observed_at if observed_at.tzinfo is not None else observed_at.replace(tzinfo=timezone.utc)
    known_at = known_at if known_at.tzinfo is not None else known_at.replace(tzinfo=timezone.utc)
    records = dataframe.to_dict(orient="records")
    if not records:
        raise CalendarIngestionRangeIncompleteError("calendar source returned no natural-day facts")
    result: list[CalendarSessionFact] = []
    seen_dates: set[date] = set()
    for row in records:
        day = _parse_date(row.get("cal_date"))
        if day in seen_dates:
            raise CalendarIngestionRangeIncompleteError(
                "calendar source returned duplicate natural-day facts",
                details={"calendar_id": canonical, "date": day.isoformat()},
            )
        seen_dates.add(day)
        is_open_value = row.get("is_open")
        if isinstance(is_open_value, bool):
            is_open = is_open_value
        elif str(is_open_value) in {"0", "1"}:
            is_open = str(is_open_value) == "1"
        else:
            raise CalendarIngestionRangeIncompleteError("calendar source returned an invalid is_open value")
        result.append(
            CalendarSessionFact(
                calendar_id=canonical,
                session_date=day,
                is_open=is_open,
                definition_version=definition.definition_version,
                source=source,
                registry_fact_id=registry.fact_id,
                registry_version=registry.registry_version,
                definition_fact_id=definition.fact_id,
                source_revision=source_revision,
                evidence={"source": source, "calendar_id": canonical, "source_revision": source_revision},
                observed_at=observed_at,
                known_at=known_at,
                source_priority_fact_id=source_priority.fact_id,
                source_priority_version=source_priority.source_priority_version,
                source_priority=source_priority.source_priority,
                source_revision_order=source_priority.source_revision_order,
                bootstrap_seed_id=source_priority.bootstrap_seed_id,
                bootstrap_seed_version=source_priority.bootstrap_seed_version,
                bootstrap_seed_hash=source_priority.bootstrap_seed_hash,
            )
        )
    return result


@dataclass(frozen=True, slots=True)
class LegacyTradeCalendarBackfillReport:
    """Auditable result of the explicit legacy-table migration boundary."""

    source_revision: str
    fetched: int
    changed: int
    unchanged: int
    failed: int
    gaps: tuple[dict[str, object], ...]
    audit: tuple[dict[str, object], ...]
    checkpoints: tuple[DataSyncCheckpointState, ...]
    checkpoint_advanced: bool
    status: str

    @property
    def input_count(self) -> int:
        """Compatibility spelling for migration reports and operator output."""

        return self.fetched

    @property
    def output_count(self) -> int:
        """Number of immutable facts written by this backfill."""

        return self.changed

    @property
    def blocked_count(self) -> int:
        """Number of rows/ranges held for reconciliation instead of fabricated."""

        return len(self.gaps)

    @property
    def audit_rows(self) -> tuple[dict[str, object], ...]:
        """Stable per-row exchange/binding mapping for migration review."""

        return self.audit

    @property
    def checkpoint(self) -> DataSyncCheckpointState | None:
        """Latest canonical checkpoint, if any batch completed."""

        return self.checkpoints[-1] if self.checkpoints else None


def backfill_legacy_trading_calendar_days(
    session: Session | None = None,
    *,
    source_revision: str = "legacy-trading-calendar-days-v1",
    data_cutoff: datetime | None = None,
    known_at: datetime | None = None,
    observed_at: datetime | None = None,
    batch_size: int = 500,
    start_date: date | None = None,
    end_date: date | None = None,
    source: str | None = None,
) -> LegacyTradeCalendarBackfillReport:
    """Backfill legacy rows into named facts through an explicit audit boundary.

    The old ``trading_calendar_days`` table is read-only here.  Every row is
    resolved through persisted binding, registry and definition facts at one
    PIT cutoff; unresolved rows become reconciliation ranges and never turn
    into guessed closed days or guessed session windows.
    """

    if not isinstance(source_revision, str) or not source_revision.strip():
        raise ValueError("source_revision must be non-blank")
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
        raise ValueError("batch_size must be a positive integer")

    def aware(value: datetime | None, fallback: datetime) -> datetime:
        if value is None:
            return fallback
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    now = datetime.now(timezone.utc)
    cutoff = aware(data_cutoff, now)
    knowledge_time = aware(known_at, cutoff)
    observed_time = aware(observed_at, knowledge_time)
    if session is None:
        with Session(get_engine()) as owned_session:
            try:
                report = _backfill_legacy_trading_calendar_days(
                    owned_session,
                    source_revision=source_revision.strip(),
                    data_cutoff=cutoff,
                    known_at=knowledge_time,
                    observed_at=observed_time,
                    batch_size=batch_size,
                    start_date=start_date,
                    end_date=end_date,
                    source=source,
                )
                owned_session.commit()
                return report
            except Exception:
                owned_session.rollback()
                raise
    try:
        report = _backfill_legacy_trading_calendar_days(
            session,
            source_revision=source_revision.strip(),
            data_cutoff=cutoff,
            known_at=knowledge_time,
            observed_at=observed_time,
            batch_size=batch_size,
            start_date=start_date,
            end_date=end_date,
            source=source,
        )
        session.commit()
        return report
    except Exception:
        session.rollback()
        raise


def _backfill_legacy_trading_calendar_days(
    session: Session,
    *,
    source_revision: str,
    data_cutoff: datetime,
    known_at: datetime,
    observed_at: datetime,
    batch_size: int,
    start_date: date | None,
    end_date: date | None,
    source: str | None,
) -> LegacyTradeCalendarBackfillReport:
    """Implementation kept separate so callers can inject a SQLite session."""

    if start_date is not None and end_date is not None and end_date < start_date:
        raise ValueError("end_date must not precede start_date")
    # ``valid_to`` was not present in the original table, but some deployments
    # carried the old closed-end column.  Selecting the physical row keeps the
    # migration compatible with both schemas without changing the ORM model.
    query = "SELECT * FROM trading_calendar_days"
    predicates: list[str] = []
    params: dict[str, object] = {}
    if start_date is not None:
        predicates.append("calendar_date >= :start_date")
        params["start_date"] = start_date
    if end_date is not None:
        predicates.append("calendar_date <= :end_date")
        params["end_date"] = end_date
    if predicates:
        query += " WHERE " + " AND ".join(predicates)
    query += " ORDER BY exchange ASC, calendar_date ASC"
    statement = text(query).bindparams(**params)
    legacy_rows = [dict(row) for row in session.execute(statement).mappings()]
    context = CalendarPITContext.from_query_boundary(
        QueryBoundary(data_cutoff=data_cutoff, include_cutoff_day=True)
    )
    repository = CalendarFactRepository(session)
    priorities = repository.list_source_priorities()
    registry_rows = repository.list_registries()
    definitions = repository.list_definitions(
        tuple(registry.calendar_id for registry in registry_rows)
    ) if registry_rows else ()
    definitions_by_calendar: dict[str, tuple[CalendarDefinition, ...]] = {}
    for definition in definitions:
        definitions_by_calendar.setdefault(definition.calendar_id, ())
        definitions_by_calendar[definition.calendar_id] = (*definitions_by_calendar[definition.calendar_id], definition)
    registries_by_calendar: dict[str, tuple[CalendarRegistry, ...]] = {}
    for registry in registry_rows:
        registries_by_calendar.setdefault(registry.calendar_id, ())
        registries_by_calendar[registry.calendar_id] = (*registries_by_calendar[registry.calendar_id], registry)
    bindings_by_alias: dict[str, tuple[CalendarExchangeBinding, ...]] = {}
    # Loading all bindings once avoids opening a new transaction per legacy row.
    for binding in repository.list_bindings():
        bindings_by_alias.setdefault(binding.alias, ())
        bindings_by_alias[binding.alias] = (*bindings_by_alias[binding.alias], binding)

    fetched = changed = unchanged = failed = 0
    gap_items: list[dict[str, object]] = []
    audit_items: list[dict[str, object]] = []
    checkpoint_states: list[DataSyncCheckpointState] = []
    checkpoint_before_by_calendar: dict[str, str | None] = {}
    checkpoint_after_by_calendar: dict[str, str | None] = {}
    checkpoint_advanced = False
    grouped: dict[str, list[dict[str, object]]] = {}

    def gap(
        *,
        calendar_id: str | None,
        row: Mapping[str, object] | None,
        reason: str,
        range_start: date,
        range_end: date,
        alias: str | None = None,
    ) -> None:
        item: dict[str, object] = {
            "calendar_id": calendar_id,
            "legacy_exchange": alias or (str(row.get("exchange")) if row else None),
            "session_date": range_start.isoformat(),
            "range_start": range_start.isoformat(),
            "range_end": range_end.isoformat(),
            "reason": reason,
        }
        gap_items.append(item)
        audit_items.append({**item, "status": "blocked"})
        if calendar_id is None:
            return
        existing = session.scalars(
            select(CalendarReconciliationRangeRecord).where(
                CalendarReconciliationRangeRecord.calendar_id == calendar_id,
                CalendarReconciliationRangeRecord.range_start == range_start,
                CalendarReconciliationRangeRecord.range_end == range_end,
                CalendarReconciliationRangeRecord.source_revision == source_revision,
                CalendarReconciliationRangeRecord.reason == reason,
            )
        ).first()
        if existing is None:
            repository.enqueue_reconciliation(
                calendar_id=calendar_id,
                range_start=range_start,
                range_end=range_end,
                source_revision=source_revision,
                reason=reason,
            )

    for row in legacy_rows:
        fetched += 1
        raw_alias = str(row.get("exchange", "")).strip().upper()
        raw_date = row.get("calendar_date")
        try:
            day = raw_date if isinstance(raw_date, date) and not isinstance(raw_date, datetime) else date.fromisoformat(str(raw_date))
        except (TypeError, ValueError):
            failed += 1
            continue
        candidates = bindings_by_alias.get(raw_alias, ())
        canonical: str | None = None
        try:
            selected_binding = select_pit_candidate(
                candidates,
                effective_day=day,
                pit_context=context,
                source_priorities=priorities,
                missing_code="calendar_binding_missing",
                ambiguous_code="calendar_binding_ambiguous",
            )
            if not isinstance(selected_binding, CalendarExchangeBinding):
                raise LegacyExchangeAmbiguousError("binding candidate is not a calendar binding")
            canonical = normalize_calendar_id(selected_binding.canonical_calendar_id)
            if source is not None and source != selected_binding.source:
                raise LegacyExchangeAmbiguousError(
                    "legacy source does not match the persisted binding priority source"
                )
            registry_candidates = tuple(
                item
                for item in registries_by_calendar.get(canonical, ())
                if item.fact_id == selected_binding.registry_fact_id
                and item.registry_version == selected_binding.registry_version
            )
            selected_registry = select_pit_candidate(
                registry_candidates,
                effective_day=day,
                pit_context=context,
                source_priorities=priorities,
                missing_code="calendar_registry_fact_missing",
                ambiguous_code="calendar_registry_ambiguous",
            )
            if not isinstance(selected_registry, CalendarRegistry):
                raise LegacyExchangeAmbiguousError("registry candidate is not a calendar registry")
            definition_candidates = tuple(
                item
                for item in definitions_by_calendar.get(canonical, ())
                if item.registry_fact_id == selected_registry.fact_id
                and item.registry_version == selected_registry.registry_version
            )
            selected_definition = select_pit_candidate(
                definition_candidates,
                effective_day=day,
                pit_context=context,
                source_priorities=priorities,
                missing_code="calendar_definition_missing",
                ambiguous_code="calendar_definition_ambiguous",
            )
            if not isinstance(selected_definition, CalendarDefinition):
                raise LegacyExchangeAmbiguousError("definition candidate is not a calendar definition")
            try:
                legacy_range_start, legacy_range_end, range_conversion = _legacy_valid_range(
                    day, row.get("valid_to")
                )
            except ValueError:
                failed += 1
                gap(
                    calendar_id=canonical,
                    row=row,
                    reason="legacy_fact_invalid",
                    range_start=day,
                    range_end=day + timedelta(days=1),
                    alias=raw_alias,
                )
                continue
            # A legacy row can cover several natural days, but the canonical
            # fact contract remains one ``CalendarSessionFact`` per day.  Do
            # the closed-to-half-open conversion once, then expand the range
            # so SQL and in-memory ``applies_to`` see identical daily cells.
            if not (
                selected_binding.applies_to(legacy_range_start)
                and selected_binding.applies_to(legacy_range_end - timedelta(days=1))
                and selected_registry.applies_to(legacy_range_start)
                and selected_registry.applies_to(legacy_range_end - timedelta(days=1))
                and selected_definition.applies_to(legacy_range_start)
                and selected_definition.applies_to(legacy_range_end - timedelta(days=1))
            ):
                failed += 1
                gap(
                    calendar_id=canonical,
                    row=row,
                    reason="legacy_fact_invalid",
                    range_start=legacy_range_start,
                    range_end=legacy_range_end,
                    alias=raw_alias,
                )
                continue
            covered_end = (
                min(legacy_range_end, end_date + timedelta(days=1))
                if end_date is not None
                else legacy_range_end
            )
            covered_day = legacy_range_start
            while covered_day < covered_end:
                grouped.setdefault(canonical, []).append({
                    "row": row,
                    "day": covered_day,
                    "legacy_range_start": legacy_range_start,
                    "legacy_range_end": legacy_range_end,
                    "range_conversion": range_conversion,
                    "binding": selected_binding,
                    "registry": selected_registry,
                    "definition": selected_definition,
                })
                covered_day += timedelta(days=1)
            audit_items.append({
                "legacy_exchange": raw_alias,
                "calendar_id": canonical,
                "session_date": day.isoformat(),
                "range_start": legacy_range_start.isoformat(),
                "range_end": legacy_range_end.isoformat(),
                "range_conversion": range_conversion,
                "binding_fact_id": str(selected_binding.fact_id),
                "binding_version": selected_binding.binding_version,
                "registry_fact_id": str(selected_registry.fact_id),
                "registry_version": selected_registry.registry_version,
                "status": "resolved",
            })
        except Exception as exc:
            gap(
                calendar_id=canonical,
                row=row,
                reason=getattr(exc, "code", "legacy_binding_or_registry_unresolved"),
                range_start=day,
                range_end=day + timedelta(days=1),
                alias=raw_alias,
            )

    # Rows whose alias cannot resolve have no canonical reconciliation key and
    # therefore remain a stable blocked report item without touching facts.
    for canonical, rows in grouped.items():
        rows.sort(key=lambda item: item["day"])
        # When the caller bounds the migration explicitly, the requested
        # interval—not merely the rows returned by the old table—is the
        # coverage contract.  A missing prefix or suffix is a real natural
        # day gap; hold the whole canonical group until reconciliation rather
        # than advancing a checkpoint past an incomplete boundary.
        boundary_gap = False
        first_day = rows[0]["day"]
        last_day = rows[-1]["day"]
        if start_date is not None and first_day > start_date:
            gap(
                calendar_id=canonical,
                row=rows[0]["row"],
                reason="missing_natural_day",
                range_start=start_date,
                range_end=first_day,
                alias=str(rows[0]["row"].get("exchange", "")),
            )
            boundary_gap = True
        if end_date is not None and last_day < end_date:
            gap(
                calendar_id=canonical,
                row=rows[-1]["row"],
                reason="missing_natural_day",
                range_start=last_day + timedelta(days=1),
                range_end=end_date + timedelta(days=1),
                alias=str(rows[-1]["row"].get("exchange", "")),
            )
            boundary_gap = True
        if boundary_gap:
            continue
        checkpoint = DataSyncCheckpointRepository(session).get(
            LEGACY_TRADE_CALENDAR_BACKFILL_SYNC_KEY,
            f"calendar_id={canonical}",
        )
        checkpoint_before_by_calendar[canonical] = (
            _checkpoint_synced_through_date(checkpoint).isoformat()
            if checkpoint is not None
            else None
        )
        checkpoint_after_by_calendar[canonical] = checkpoint_before_by_calendar[canonical]
        stopped = False
        previous_group_day: date | None = None
        for offset in range(0, len(rows), batch_size):
            batch = rows[offset : offset + batch_size]
            facts: list[CalendarSessionFact] = []
            batch_end = batch[-1]["day"]
            previous_day = previous_group_day
            for item in batch:
                day = item["day"]
                if previous_day is not None and day > previous_day + timedelta(days=1):
                    missing_start = previous_day + timedelta(days=1)
                    gap(
                        calendar_id=canonical,
                        row=item["row"],
                        reason="missing_natural_day",
                        range_start=missing_start,
                        range_end=day,
                        alias=str(item["row"].get("exchange", "")),
                    )
                    stopped = True
                    break
                row = item["row"]
                definition = item["definition"]
                registry = item["registry"]
                binding = item["binding"]
                try:
                    is_open = _legacy_is_open(row.get("is_open"))
                    fact = CalendarSessionFact(
                        calendar_id=canonical,
                        session_date=day,
                        is_open=is_open,
                        definition_version=definition.definition_version,
                        registry_fact_id=registry.fact_id,
                        registry_version=registry.registry_version,
                        definition_fact_id=definition.fact_id,
                        valid_from=day,
                        valid_to=day + timedelta(days=1),
                        source=source or binding.source,
                        source_revision=source_revision,
                        evidence={
                            "migration": "legacy_trading_calendar_days",
                            "legacy_exchange": str(row.get("exchange", "")),
                            "calendar_id": canonical,
                            "binding_fact_id": str(binding.fact_id),
                            "binding_version": binding.binding_version,
                            "registry_fact_id": str(registry.fact_id),
                            "registry_version": registry.registry_version,
                            "range_conversion": item["range_conversion"],
                            "legacy_range_start": item["legacy_range_start"].isoformat(),
                            "legacy_range_end": item["legacy_range_end"].isoformat(),
                            "legacy_created_at": str(row.get("created_at")) if row.get("created_at") is not None else None,
                            "legacy_updated_at": str(row.get("updated_at")) if row.get("updated_at") is not None else None,
                        },
                        known_at=known_at,
                        observed_at=observed_at,
                        source_priority_fact_id=binding.source_priority_fact_id,
                        source_priority_version=binding.source_priority_version,
                        source_priority=binding.source_priority,
                        source_revision_order=binding.source_revision_order,
                        bootstrap_seed_id=binding.bootstrap_seed_id,
                        bootstrap_seed_version=binding.bootstrap_seed_version,
                        bootstrap_seed_hash=binding.bootstrap_seed_hash,
                        sessions_override=None if is_open else (),
                        override_mode="inherit" if is_open else "explicit",
                    )
                except Exception as exc:
                    failed += 1
                    gap(
                        calendar_id=canonical,
                        row=row,
                        reason=getattr(exc, "code", "legacy_fact_invalid"),
                        range_start=day,
                        range_end=day + timedelta(days=1),
                        alias=str(row.get("exchange", "")),
                    )
                    stopped = True
                    break
                facts.append(fact)
                previous_day = day
            if facts:
                # A legacy row can be corrected without changing the caller's
                # migration timestamp.  Keep the append-only PIT history
                # ordered by moving only a conflicting revision one microsecond
                # past its predecessor; identical replays retain their hash.
                ordered_facts: list[CalendarSessionFact] = []
                for fact in facts:
                    latest = session.scalars(
                        select(CalendarSessionFactRecord)
                        .where(
                            CalendarSessionFactRecord.logical_fact_key == fact.logical_fact_key
                        )
                        .order_by(CalendarSessionFactRecord.fact_version.desc())
                    ).first()
                    if latest is not None and latest.content_hash != fact.content_hash:
                        latest_known = latest.known_at
                        if latest_known is not None and latest_known.tzinfo is None:
                            latest_known = latest_known.replace(tzinfo=timezone.utc)
                        if latest_known is not None and latest_known >= fact.known_at:
                            bumped = latest_known + timedelta(microseconds=1)
                            fact = replace(fact, known_at=bumped, knowledge_from=bumped)
                    ordered_facts.append(fact)
                facts = ordered_facts
                _received, batch_changed, batch_unchanged = repository.append_session_facts_idempotent(facts)
                changed += batch_changed
                unchanged += batch_unchanged
                repository.rebuild_resolution_heads(
                    calendar_id=canonical,
                    start_date=facts[0].session_date,
                    end_date=facts[-1].session_date,
                )
            if stopped:
                session.commit()
                break
            # A replay still writes/validates facts above, but it must not
            # mutate a checkpoint that already covers this batch.  Keeping
            # the cursor monotonic also avoids needless version churn when a
            # filtered backfill range is rerun.
            if (
                checkpoint is not None
                and _checkpoint_synced_through_date(checkpoint) >= batch_end
            ):
                session.commit()
                previous_group_day = batch_end
                continue
            next_state = DataSyncCheckpointRepository(session).advance(
                sync_key=LEGACY_TRADE_CALENDAR_BACKFILL_SYNC_KEY,
                scope_key=f"calendar_id={canonical}",
                cursor={
                    "synced_through_date": batch_end.isoformat(),
                    "calendar_id": canonical,
                    "source_revision": source_revision,
                },
                expected_version=checkpoint.version if checkpoint is not None else None,
            )
            checkpoint = next_state
            checkpoint_states.append(next_state)
            checkpoint_after_by_calendar[canonical] = batch_end.isoformat()
            checkpoint_advanced = True
            previous_group_day = batch_end
            session.commit()
        if stopped:
            continue

    status = "blocked" if gap_items or failed else "completed"
    logger.info(
        "legacy_trade_calendar_backfill_completed",
        message=(
            f"旧交易日历回填完成：{start_date.isoformat() if start_date else '不适用'} 至 "
            f"{end_date.isoformat() if end_date else '不适用'}，来源修订 {source_revision}，拉取 {fetched} 条，"
            f"变更 {changed} 条，未变更 {unchanged} 条，失败 {failed} 条，"
            f"缺口 {len(gap_items)} 条，checkpoint {'已推进' if checkpoint_advanced else '未推进'}。"
        ),
        title="旧交易日历回填完成",
        data_type="trading_calendar",
        calendar_id=None,
        start_date=start_date.isoformat() if start_date is not None else None,
        end_date=end_date.isoformat() if end_date is not None else None,
        migration="legacy_trading_calendar_days",
        source=source or TUSHARE_SOURCE,
        source_revision=source_revision,
        fetched_count=fetched,
        changed_count=changed,
        unchanged_count=unchanged,
        failed_count=failed,
        gap_count=len(gap_items),
        checkpoint_scope="calendar_id=*",
        checkpoint_before=checkpoint_before_by_calendar,
        checkpoint_after=checkpoint_after_by_calendar,
        checkpoint_advanced=checkpoint_advanced,
        reconciliation_range=(
            tuple(
                {
                    "calendar_id": item.get("calendar_id"),
                    "range_start": item.get("range_start"),
                    "range_end": item.get("range_end"),
                }
                for item in gap_items
            )
            if gap_items
            else None
        ),
    )
    if gap_items:
        logger.info(
            "calendar_reconciliation_blocked",
            message=(
                f"旧交易日历修订校验阻断：{start_date.isoformat() if start_date else '不适用'} 至 "
                f"{end_date.isoformat() if end_date else '不适用'}，拉取 {fetched} 条，变更 {changed} 条，"
                f"未变更 {unchanged} 条，失败 {failed} 条，checkpoint 未推进。"
            ),
            title="旧日历修订校验阻断",
            data_type="trading_calendar",
            calendar_id=None,
            start_date=start_date.isoformat() if start_date is not None else None,
            end_date=end_date.isoformat() if end_date is not None else None,
            fetched_count=fetched,
            changed_count=changed,
            unchanged_count=unchanged,
            failed_count=failed,
            checkpoint_scope="calendar_id=*",
            checkpoint_before=checkpoint_before_by_calendar,
            checkpoint_after=checkpoint_after_by_calendar,
            checkpoint_advanced=False,
            source=source or TUSHARE_SOURCE,
            source_revision=source_revision,
            reconciliation_range=tuple(
                {
                    "calendar_id": item.get("calendar_id"),
                    "range_start": item.get("range_start"),
                    "range_end": item.get("range_end"),
                }
                for item in gap_items
            ),
            gap_count=len(gap_items),
        )
    return LegacyTradeCalendarBackfillReport(
        source_revision=source_revision,
        fetched=fetched,
        changed=changed,
        unchanged=unchanged,
        failed=failed,
        gaps=tuple(gap_items),
        audit=tuple(audit_items),
        checkpoints=tuple(checkpoint_states),
        checkpoint_advanced=checkpoint_advanced,
        status=status,
    )


def sync_named_trade_calendar(
    client: TushareClient,
    *,
    calendar_id: str,
    initial_start_date: date,
    registry: CalendarRegistry,
    definition: CalendarDefinition,
    source_priority: CalendarSourcePriority,
    source_revision: str,
    request_interval_ms: int | None = None,
    as_of_date: date,
    observed_at: datetime,
    known_at: datetime,
) -> TradeCalendarSyncResult:
    """Synchronize one canonical calendar into append-only facts.

    The legacy ``sync_trade_calendar`` task key remains available, while this
    strict entry point scopes checkpoint and reconciliation state by the
    canonical calendar id rather than an exchange string.
    """

    canonical = normalize_calendar_id(calendar_id)
    if registry.calendar_id != canonical or definition.calendar_id != canonical:
        raise LegacyExchangeAmbiguousError("calendar_id does not match registry/definition")
    scope_key = f"calendar_id={canonical}"
    checkpoint = _load_checkpoint(TRADE_CALENDAR_SYNC_KEY, scope_key)
    ranges = plan_trade_calendar_year_ranges(
        checkpoint=checkpoint,
        initial_start_date=initial_start_date,
        as_of_date=as_of_date,
    )
    effective_interval = max(get_settings().ingestion_request_interval_ms, request_interval_ms or 0)
    source_name = source_priority.source
    plan_checkpoint = (
        _checkpoint_synced_through_date(checkpoint).isoformat()
        if checkpoint is not None
        else None
    )
    plan_start = ranges[0].start_date if ranges else initial_start_date
    plan_end = ranges[-1].end_date if ranges else as_of_date
    logger.info(
        "trade_calendar_named_sync_planned",
        message=(
            f"具名交易日历采集计划：{canonical}，{plan_start.isoformat()} 至 {plan_end.isoformat()}，"
            "拉取 0 条，变更 0 条，未变更 0 条，失败 0 条，checkpoint 未推进。"
        ),
        title="具名交易日历采集计划",
        data_type="trading_calendar",
        calendar_id=canonical,
        start_date=plan_start.isoformat(),
        end_date=plan_end.isoformat(),
        fetched_count=0,
        changed_count=0,
        unchanged_count=0,
        failed_count=0,
        checkpoint_scope=scope_key,
        checkpoint_before=plan_checkpoint,
        checkpoint_after=plan_checkpoint,
        checkpoint_advanced=False,
        source=source_name,
        source_revision=source_revision,
        reconciliation_range=None,
        ranges_planned=len(ranges),
    )
    received = changed = unchanged = 0
    for date_range in ranges:
        range_fields = {
            "data_type": "trading_calendar",
            "calendar_id": canonical,
            "start_date": date_range.start_date.isoformat(),
            "end_date": date_range.end_date.isoformat(),
            "checkpoint_scope": scope_key,
        }
        range_checkpoint_before = (
            _checkpoint_synced_through_date(checkpoint).isoformat()
            if checkpoint is not None
            else None
        )
        logger.info(
            "trade_calendar_named_range_started",
            message=(
                f"开始采集 {canonical} 交易日历：{range_fields['start_date']} 至 "
                f"{range_fields['end_date']}，拉取 0 条，变更 0 条，未变更 0 条，失败 0 条，checkpoint 未推进。"
            ),
            title="开始具名交易日历采集",
            **range_fields,
            fetched_count=0,
            changed_count=0,
            unchanged_count=0,
            failed_count=0,
            checkpoint_before=range_checkpoint_before,
            checkpoint_after=range_checkpoint_before,
            checkpoint_advanced=False,
            source=source_name,
            source_revision=source_revision,
        )
        try:
            tushare_request_pacer.wait_for_turn(effective_interval)
            dataframe = fetch_trade_calendar(
                client,
                exchange=canonical,
                start_date=date_range.start_date.strftime("%Y%m%d"),
                end_date=date_range.end_date.strftime("%Y%m%d"),
            )
            days = normalize_named_calendar_facts(
                dataframe,
                calendar_id=canonical,
                definition=definition,
                registry=registry,
                source_priority=source_priority,
                source=source_name,
                source_revision=source_revision,
                observed_at=observed_at,
                known_at=known_at,
            )
            expected = {
                date_range.start_date + timedelta(days=index)
                for index in range((date_range.end_date - date_range.start_date).days + 1)
            }
            actual = {fact.session_date for fact in days}
            if actual != expected:
                raise CalendarIngestionRangeIncompleteError(
                    "calendar source did not return every natural day",
                    details={"missing": sorted(day.isoformat() for day in expected - actual)},
                )
        except CalendarIngestionRangeIncompleteError as exc:
            # Persist a blocked reconciliation range for a missing natural
            # day.  It is not a closed fact and the checkpoint must not move.
            with Session(get_engine()) as session:
                try:
                    repository = CalendarFactRepository(session)
                    reconciliation = repository.enqueue_reconciliation(
                        calendar_id=canonical,
                        range_start=date_range.start_date,
                        range_end=date_range.end_date + timedelta(days=1),
                        source_revision=source_revision,
                        reason="missing_natural_day",
                    )
                    reconciliation.status = "blocked"
                    session.commit()
                except Exception:
                    session.rollback()
                    raise
            logger.exception(
                "trade_calendar_named_range_failed",
                message=(
                    f"采集 {canonical} 交易日历失败：{range_fields['start_date']} 至 "
                    f"{range_fields['end_date']}，拉取 0 条，变更 0 条，未变更 0 条，失败 1 条，checkpoint 未推进。"
                ),
                title="具名交易日历采集失败",
                **range_fields,
                fetched_count=0,
                changed_count=0,
                unchanged_count=0,
                failed_count=1,
                checkpoint_before=range_checkpoint_before,
                checkpoint_after=range_checkpoint_before,
                checkpoint_advanced=False,
                source=source_name,
                source_revision=source_revision,
                reconciliation_range={
                    "range_start": date_range.start_date.isoformat(),
                    "range_end": (date_range.end_date + timedelta(days=1)).isoformat(),
                },
                reconciliation_status="blocked",
            )
            logger.info(
                "calendar_reconciliation_blocked",
                message=(
                    f"交易日历修订校验阻断：{canonical}，{date_range.start_date.isoformat()} 至 "
                    f"{date_range.end_date.isoformat()}，拉取 0 条，变更 0 条，未变更 0 条，失败 1 条，checkpoint 未推进。"
                ),
                title="日历修订校验阻断",
                data_type="trading_calendar",
                calendar_id=canonical,
                start_date=date_range.start_date.isoformat(),
                end_date=date_range.end_date.isoformat(),
                fetched_count=0,
                changed_count=0,
                unchanged_count=0,
                failed_count=1,
                checkpoint_scope=scope_key,
                checkpoint_before=range_checkpoint_before,
                checkpoint_after=range_checkpoint_before,
                checkpoint_advanced=False,
                source=source_name,
                source_revision=source_revision,
                reconciliation_range={
                    "range_start": date_range.start_date.isoformat(),
                    "range_end": (date_range.end_date + timedelta(days=1)).isoformat(),
                },
                reconciliation_status="blocked",
            )
            raise
        except Exception:
            logger.exception(
                "trade_calendar_named_range_failed",
                message=(
                    f"采集 {canonical} 交易日历失败：{range_fields['start_date']} 至 "
                    f"{range_fields['end_date']}，拉取 0 条，变更 0 条，未变更 0 条，失败 1 条，checkpoint 未推进。"
                ),
                title="具名交易日历采集失败",
                **range_fields,
                fetched_count=0,
                changed_count=0,
                unchanged_count=0,
                failed_count=1,
                checkpoint_before=range_checkpoint_before,
                checkpoint_after=range_checkpoint_before,
                checkpoint_advanced=False,
                source=source_name,
                source_revision=source_revision,
            )
            raise
        with Session(get_engine()) as session:
            try:
                repository = CalendarFactRepository(session)
                had_existing_facts = bool(
                    repository.list_session_facts(
                        (canonical,),
                        date_range.start_date,
                        date_range.end_date + timedelta(days=1),
                    )
                )
                fetched, batch_changed, batch_unchanged = repository.append_session_facts_idempotent(days)
                # Publish the replaceable PIT head only after the immutable
                # fact batch has been validated in the same transaction.  A
                # checkpoint never advances ahead of its resolution index.
                repository.rebuild_resolution_heads(
                    calendar_id=canonical,
                    start_date=date_range.start_date,
                    end_date=date_range.end_date,
                )
                # Keep the legacy exchange table as a compatibility mirror;
                # strict readers never use it as the calendar source.
                TradingCalendarRepository(session).upsert_days(
                    [
                        TradingCalendarDayInput(
                            exchange=canonical,
                            calendar_date=fact.session_date,
                            is_open=fact.is_open,
                            previous_trading_date=None,
                            calendar_id=canonical,
                        )
                        for fact in days
                    ]
                )
                reconciliation = None
                reconciliation_range = None
                if had_existing_facts and batch_changed:
                    reconciliation_range = {
                        "range_start": date_range.start_date.isoformat(),
                        "range_end": (date_range.end_date + timedelta(days=1)).isoformat(),
                    }
                    logger.info(
                        "calendar_reconciliation_started",
                        message=(
                            f"开始交易日历修订校验：{canonical}，{date_range.start_date.isoformat()} 至 "
                            f"{date_range.end_date.isoformat()}，拉取 {fetched} 条，变更 {batch_changed} 条，"
                            f"未变更 {batch_unchanged} 条，失败 0 条，checkpoint 未推进。"
                        ),
                        title="开始交易日历修订校验",
                        data_type="trading_calendar",
                        calendar_id=canonical,
                        start_date=date_range.start_date.isoformat(),
                        end_date=date_range.end_date.isoformat(),
                        fetched_count=fetched,
                        changed_count=batch_changed,
                        unchanged_count=batch_unchanged,
                        failed_count=0,
                        checkpoint_scope=scope_key,
                        checkpoint_before=range_checkpoint_before,
                        checkpoint_after=range_checkpoint_before,
                        checkpoint_advanced=False,
                        source=source_name,
                        source_revision=source_revision,
                        reconciliation_range=reconciliation_range,
                    )
                    reconciliation = repository.enqueue_reconciliation(
                        calendar_id=canonical,
                        range_start=date_range.start_date,
                        range_end=date_range.end_date + timedelta(days=1),
                        source_revision=source_revision,
                        reason="source_revision_changed",
                    )
                next_checkpoint = DataSyncCheckpointRepository(session).advance(
                    sync_key=TRADE_CALENDAR_SYNC_KEY,
                    scope_key=scope_key,
                    cursor={"synced_through_date": date_range.end_date.isoformat(), "calendar_id": canonical},
                    expected_version=checkpoint.version if checkpoint is not None else None,
                )
                session.commit()
            except Exception:
                session.rollback()
                raise
        received += fetched
        changed += batch_changed
        unchanged += batch_unchanged
        checkpoint = next_checkpoint
        checkpoint_after = _checkpoint_synced_through_date(checkpoint).isoformat()
        logger.info(
            "trade_calendar_named_range_succeeded",
            message=(
                f"完成 {canonical} 交易日历采集：{date_range.start_date.isoformat()} 至 {date_range.end_date.isoformat()}，"
                f"拉取 {fetched} 条，变更 {batch_changed} 条，未变更 {batch_unchanged} 条，失败 0 条，checkpoint 已推进至 {date_range.end_date.isoformat()}。"
            ),
            title="具名交易日历采集完成",
            data_type="trading_calendar",
            calendar_id=canonical,
            start_date=date_range.start_date.isoformat(),
            end_date=date_range.end_date.isoformat(),
            fetched_count=fetched,
            changed_count=batch_changed,
            unchanged_count=batch_unchanged,
            failed_count=0,
            checkpoint_scope=scope_key,
            checkpoint_before=range_checkpoint_before,
            checkpoint_after=checkpoint_after,
            checkpoint_advanced=True,
            source=source_name,
            source_revision=source_revision,
            reconciliation_range=reconciliation_range,
            reconciliation_id=str(reconciliation.id) if reconciliation is not None else None,
            reconciliation_status=reconciliation.status if reconciliation is not None else None,
        )
        if reconciliation is not None:
            logger.info(
                "calendar_reconciliation_completed",
                message=(
                    f"完成交易日历修订校验：{canonical}，{date_range.start_date.isoformat()} 至 "
                    f"{date_range.end_date.isoformat()}，拉取 {fetched} 条，变更 {batch_changed} 条，"
                    f"未变更 {batch_unchanged} 条，失败 0 条，checkpoint 已推进至 {checkpoint_after}。"
                ),
                title="完成交易日历修订校验",
                data_type="trading_calendar",
                calendar_id=canonical,
                start_date=date_range.start_date.isoformat(),
                end_date=date_range.end_date.isoformat(),
                fetched_count=fetched,
                changed_count=batch_changed,
                unchanged_count=batch_unchanged,
                failed_count=0,
                checkpoint_scope=scope_key,
                checkpoint_before=range_checkpoint_before,
                checkpoint_after=checkpoint_after,
                checkpoint_advanced=True,
                source=source_name,
                source_revision=source_revision,
                reconciliation_range=reconciliation_range,
                reconciliation_id=str(reconciliation.id),
                reconciliation_status=reconciliation.status,
            )
    return TradeCalendarSyncResult(
        ranges_completed=len(ranges),
        received=received,
        changed=changed,
        unchanged=unchanged,
        synced_through_date=_checkpoint_synced_through_date(checkpoint) if checkpoint is not None else None,
        start_date=ranges[0].start_date if ranges else None,
        end_date=ranges[-1].end_date if ranges else None,
        calendar_id=canonical,
    )


def _parse_date(value: Any) -> date:
    return date.fromisoformat(str(value))


def _legacy_is_open(value: object) -> bool:
    """Normalize legacy boolean encodings without treating ``"0"`` as true."""

    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip() in {"0", "1"}:
        return value.strip() == "1"
    raise ValueError("legacy trading calendar is_open must be a boolean or 0/1")


def _legacy_valid_range(
    session_date: date, closed_valid_to: object
) -> tuple[date, date, str]:
    """Convert an optional legacy closed end into a half-open coverage range."""

    if closed_valid_to is None or not str(closed_valid_to).strip():
        return session_date, session_date + timedelta(days=1), "single_day_default"
    closed_end = date.fromisoformat(str(closed_valid_to))
    valid_to = closed_end + timedelta(days=1)
    if valid_to <= session_date:
        raise ValueError("legacy valid_to must not precede session_date")
    return session_date, valid_to, "closed_to_half_open"


def _parse_optional_date(value: Any) -> date | None:
    if value is None or str(value).strip() in {"", "nan", "NaT"}:
        return None
    return _parse_date(value)


def _load_checkpoint(
    sync_key: str, scope_key: str
) -> DataSyncCheckpointState | None:
    with Session(get_engine()) as session:
        return DataSyncCheckpointRepository(session).get(sync_key, scope_key)


def _commit_trade_calendar_range(
    *,
    days: list[TradingCalendarDayInput],
    sync_key: str,
    scope_key: str,
    expected_checkpoint: DataSyncCheckpointState | None,
    synced_through_date: date,
) -> tuple[TradeCalendarUpsertResult, DataSyncCheckpointState]:
    with Session(get_engine()) as session:
        try:
            write_result = TradingCalendarRepository(session).upsert_days(days)
            checkpoint = DataSyncCheckpointRepository(session).advance(
                sync_key=sync_key,
                scope_key=scope_key,
                cursor={"synced_through_date": synced_through_date.isoformat()},
                expected_version=(
                    expected_checkpoint.version
                    if expected_checkpoint is not None
                    else None
                ),
            )
            session.commit()
            return write_result, checkpoint
        except Exception:
            session.rollback()
            raise


def _checkpoint_synced_through_date(checkpoint: DataSyncCheckpointState) -> date:
    value = checkpoint.cursor.get("synced_through_date")
    if not isinstance(value, str):
        raise ValueError("trade calendar checkpoint has no synced_through_date")
    return date.fromisoformat(value)


def _validate_trade_calendar_range(
    days: list[TradingCalendarDayInput],
    *,
    exchange: str,
    date_range: TradeCalendarDateRange,
) -> None:
    expected_dates = {
        date_range.start_date + timedelta(days=offset)
        for offset in range((date_range.end_date - date_range.start_date).days + 1)
    }
    if any(day.exchange != exchange for day in days):
        raise ValueError("Tushare returned a trade calendar row for another exchange")
    if {day.calendar_date for day in days} != expected_dates:
        raise ValueError("Tushare returned an incomplete trade calendar range")
