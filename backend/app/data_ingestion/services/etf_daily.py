"""ETF daily-bar retrieval and incremental whole-market synchronization."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Callable, Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session
import structlog

from app.core.config import get_settings
from app.data_ingestion.clients.tushare import TushareClient
from app.data_ingestion.constants import (
    ETF_DAILY_FULL_SYNC_KEY,
    ETF_DAILY_INCREMENTAL_SYNC_KEY,
    TUSHARE_SOURCE,
)
from app.data_ingestion.repositories.etf_daily import EtfDailyBarRepository
from app.data_ingestion.repositories.etf import EtfCodeRepository
from app.data_ingestion.models.etf import EtfCode
from app.instruments.models import InstrumentIdentityFactRecord
from app.data_ingestion.repositories.sync_checkpoint import DataSyncCheckpointRepository
from app.data_ingestion.request_pacing import tushare_request_pacer
from app.data_ingestion.schemas.etf_daily import (
    EtfDailyBarInput,
    EtfDailyBarUpsertResult,
    EtfDailySyncResult,
)
from app.data_ingestion.schemas.trading_calendar import DataSyncCheckpointState
from app.backtesting.calendar_axis import normalize_calendar_id
from app.backtesting.data.calendar_repository import CalendarFactRepository
from app.backtesting.data.errors import InstrumentCalendarUnresolvedError
from app.db.session import get_engine

if TYPE_CHECKING:
    from pandas import DataFrame


# Keep this aligned with Tushare's documented single-ETF ``fund_daily`` example.
ETF_DAILY_FIELDS = "trade_date,open,high,low,close,vol,amount"
# The whole-market request needs the provider code to form the database key.
ETF_DAILY_MARKET_FIELDS = f"ts_code,{ETF_DAILY_FIELDS}"
ETF_DAILY_SCOPE_KEY = "market=CN"
TUSHARE_SOURCE = "tushare"
MAX_ETF_DAILY_ROWS = 5_000
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
logger = structlog.get_logger(__name__)


def fetch_etf_daily(
    client: TushareClient,
    *,
    ts_code: str,
    start_date: str,
    end_date: str,
) -> "DataFrame":
    """Fetch daily ETF data for one code and compact date range.

    This small helper preserves the official Tushare example contract for manual
    investigation and targeted repair. The scheduled synchronization uses the
    date-wide helper below so it needs only one normal request per trading day.
    """
    return client.pro.fund_daily(
        ts_code=ts_code,
        start_date=start_date,
        end_date=end_date,
        fields=ETF_DAILY_FIELDS,
    )


def fetch_etf_daily_for_trade_date(
    client: TushareClient, *, trade_date: str
) -> "DataFrame":
    """Fetch every ETF daily bar reported by Tushare for one trade date."""
    return client.pro.fund_daily(
        trade_date=trade_date, fields=ETF_DAILY_MARKET_FIELDS
    )


def sync_etf_daily_incremental(
    client: TushareClient,
    *,
    request_interval_ms: int | None = None,
    as_of_date: date | None = None,
    calendar_ids: Iterable[str] | None = None,
    calendar_for_code: Callable[[str], str | None] | None = None,
    data_cutoff: datetime | None = None,
) -> EtfDailySyncResult:
    """Synchronize newly completed whole-market ETF sessions.

    ``as_of_date`` is inclusive and represents the last date known to have
    completed trading. Omitting it deliberately uses yesterday in Shanghai time,
    preventing a manually triggered task from treating an in-progress session as
    an end-of-day bar.
    """
    if data_cutoff is None:
        # The former market-wide entry point had an implicit SSE calendar.  It
        # is now a stable blocked migration boundary; production callers must
        # provide a PIT cutoff so the identity resolver can select each ETF's
        # InstrumentIdentityFact.calendar_id.
        raise InstrumentCalendarUnresolvedError(
            "strict ETF daily synchronization requires data_cutoff"
        )
    completed_through_date = as_of_date or (
        datetime.now(SHANGHAI_TIMEZONE).date() - timedelta(days=1)
    )
    return sync_etf_daily_by_calendar(
        client,
        calendar_ids=calendar_ids,
        calendar_for_code=calendar_for_code,
        request_interval_ms=request_interval_ms,
        as_of_date=completed_through_date,
        data_cutoff=data_cutoff,
    )


def sync_etf_daily_full(
    client: TushareClient,
    *,
    request_interval_ms: int | None = None,
    as_of_date: date | None = None,
    calendar_ids: Iterable[str] | None = None,
    calendar_for_code: Callable[[str], str | None] | None = None,
    data_cutoff: datetime | None = None,
) -> EtfDailySyncResult:
    """Run or resume one full historical ETF daily-bar verification cycle.

    A new cycle freezes its terminal date before the first request. A failed run
    resumes from its own checkpoint and keeps that terminal date, so it cannot
    chase newly completed trading days indefinitely.
    """
    if data_cutoff is None:
        # Do not initialize or advance the legacy market-wide checkpoint when
        # identity/PIT context is absent.  This is an immediate stable block.
        raise InstrumentCalendarUnresolvedError(
            "strict ETF daily synchronization requires data_cutoff"
        )
    completed_through_date = as_of_date or (
        datetime.now(SHANGHAI_TIMEZONE).date() - timedelta(days=1)
    )
    # Full and incremental collection share the same identity partitioner;
    # each calendar receives an independent checkpoint scope.
    return sync_etf_daily_by_calendar(
        client,
        calendar_ids=calendar_ids,
        calendar_for_code=calendar_for_code,
        request_interval_ms=request_interval_ms,
        as_of_date=completed_through_date,
        data_cutoff=data_cutoff,
        sync_key=ETF_DAILY_FULL_SYNC_KEY,
        initial_start_date=_load_earliest_etf_list_date(),
    )


def _load_identity_calendar_by_code(
    *,
    effective_date: date,
    data_cutoff: datetime,
) -> dict[str, str]:
    """Resolve each ETF source code from one visible identity fact.

    Identity facts are folded by ``logical_fact_key`` only after applying the
    effective-day and ``known_at <= data_cutoff`` predicates.  A source code
    cannot safely address bars when it belongs to multiple instruments,
    logical identity assertions, or calendars at the same PIT, so every such
    case fails with the stable ``instrument_calendar_unresolved`` code.
    """

    if (
        not isinstance(effective_date, date)
        or isinstance(effective_date, datetime)
        or not isinstance(data_cutoff, datetime)
        or data_cutoff.tzinfo is None
        or data_cutoff.utcoffset() is None
    ):
        raise InstrumentCalendarUnresolvedError(
            "ETF calendar resolution requires a calendar effective date and aware data_cutoff"
        )
    cutoff_utc = data_cutoff.astimezone(UTC)
    with Session(get_engine()) as session:
        rows = session.execute(
            select(
                EtfCode.ts_code,
                EtfCode.etf_id,
                InstrumentIdentityFactRecord.logical_fact_key,
                InstrumentIdentityFactRecord.calendar_id,
                InstrumentIdentityFactRecord.fact_version,
                InstrumentIdentityFactRecord.id,
                InstrumentIdentityFactRecord.known_at,
            )
            .join(
                InstrumentIdentityFactRecord,
                InstrumentIdentityFactRecord.instrument_id == EtfCode.etf_id,
            )
            .where(
                EtfCode.source == TUSHARE_SOURCE,
                InstrumentIdentityFactRecord.asset_class == "etf",
                InstrumentIdentityFactRecord.valid_from <= effective_date,
                or_(
                    InstrumentIdentityFactRecord.valid_to.is_(None),
                    InstrumentIdentityFactRecord.valid_to > effective_date,
                ),
                InstrumentIdentityFactRecord.known_at <= data_cutoff,
            )
            .order_by(
                EtfCode.ts_code,
                EtfCode.etf_id,
                InstrumentIdentityFactRecord.logical_fact_key,
                InstrumentIdentityFactRecord.known_at.desc(),
                InstrumentIdentityFactRecord.fact_version.desc(),
                InstrumentIdentityFactRecord.id,
            )
        ).all()

    grouped: dict[tuple[str, object, str], list[tuple[str, datetime, int, object]]] = {}
    for row in rows:
        try:
            code, instrument_id, logical_key, calendar_id, fact_version, fact_id, known_at = row
        except (TypeError, ValueError) as exc:
            raise InstrumentCalendarUnresolvedError(
                "ETF identity query returned an invalid row shape"
            ) from exc
        if not isinstance(code, str) or not code.strip():
            raise InstrumentCalendarUnresolvedError(
                "ETF identity fact has no source code"
            )
        if not isinstance(logical_key, str) or not logical_key.strip():
            logical_key = f"identity:{instrument_id}:{code}"
        if not isinstance(calendar_id, str) or not calendar_id.strip():
            raise InstrumentCalendarUnresolvedError(
                "ETF InstrumentIdentityFact has no calendar_id",
                details={"ts_code": code, "effective_date": effective_date.isoformat()},
            )
        if not isinstance(known_at, datetime):
            raise InstrumentCalendarUnresolvedError(
                "ETF InstrumentIdentityFact has no valid known_at",
                details={"ts_code": code},
            )
        if known_at.tzinfo is None or known_at.utcoffset() is None:
            # SQLite drops the timezone marker from DateTime values.  The
            # persisted identity contract is UTC-aware, so restore that
            # marker only at this read boundary before PIT comparison.
            known_at = known_at.replace(tzinfo=UTC)
        known_at_utc = known_at.astimezone(UTC)
        if known_at_utc > cutoff_utc:
            continue
        try:
            canonical_calendar = normalize_calendar_id(calendar_id)
        except Exception as exc:
            raise InstrumentCalendarUnresolvedError(
                "ETF InstrumentIdentityFact has an invalid calendar_id",
                details={"ts_code": code, "calendar_id": calendar_id},
            ) from exc
        if isinstance(fact_version, bool) or not isinstance(fact_version, int) or fact_version < 1:
            raise InstrumentCalendarUnresolvedError(
                "ETF InstrumentIdentityFact has an invalid fact_version",
                details={"ts_code": code},
            )
        grouped.setdefault((code, instrument_id, logical_key), []).append(
            (canonical_calendar, known_at_utc, fact_version, fact_id)
        )

    selected: dict[str, list[tuple[object, str, object]]] = {}
    for (code, instrument_id, logical_key), candidates in grouped.items():
        highest_rank = max((item[1], item[2]) for item in candidates)
        top = [item for item in candidates if (item[1], item[2]) == highest_rank]
        calendars = {item[0] for item in top}
        fact_ids = {item[3] for item in top if item[3] is not None}
        if len(calendars) != 1 or len(fact_ids) > 1:
            raise InstrumentCalendarUnresolvedError(
                "ETF source code has ambiguous identity facts at the requested PIT",
                details={
                    "ts_code": code,
                    "instrument_id": str(instrument_id),
                    "logical_fact_key": logical_key,
                    "calendar_ids": sorted(calendars),
                },
            )
        selected.setdefault(code, []).append((instrument_id, logical_key, top[0][0]))

    resolved: dict[str, str] = {}
    for code, candidates in selected.items():
        instrument_ids = {item[0] for item in candidates}
        logical_keys = {item[1] for item in candidates}
        calendars = {item[2] for item in candidates}
        if len(instrument_ids) != 1 or len(logical_keys) != 1 or len(calendars) != 1:
            raise InstrumentCalendarUnresolvedError(
                "ETF source code has ambiguous identity facts at the requested PIT",
                details={
                    "ts_code": code,
                    "calendar_ids": sorted(calendars),
                    "instrument_ids": sorted(str(item) for item in instrument_ids),
                },
            )
        resolved[code] = next(iter(calendars))
    if not resolved:
        raise InstrumentCalendarUnresolvedError(
            "no ETF InstrumentIdentityFact.calendar_id rows are visible at the requested cutoff",
            details={
                "effective_date": effective_date.isoformat(),
                "data_cutoff": data_cutoff.isoformat(),
            },
        )
    return resolved


def sync_etf_daily_by_calendar(
    client: TushareClient,
    *,
    calendar_ids: Iterable[str] | None = None,
    calendar_for_code: Callable[[str], str | None] | None = None,
    request_interval_ms: int | None = None,
    as_of_date: date,
    data_cutoff: datetime,
    sync_key: str = ETF_DAILY_INCREMENTAL_SYNC_KEY,
    initial_start_date: date | None = None,
) -> EtfDailySyncResult:
    """Synchronize ETF bars with one independent checkpoint per calendar.

    ``calendar_for_code`` is intentionally a test-only seam.  Production
    calls omit it and resolve every source code through the PIT identity query
    above; when a callback is supplied, the finite calendar set must also be
    explicit so it cannot widen a production run implicitly.
    """

    if (
        not isinstance(as_of_date, date)
        or isinstance(as_of_date, datetime)
        or not isinstance(data_cutoff, datetime)
        or data_cutoff.tzinfo is None
        or data_cutoff.utcoffset() is None
    ):
        raise InstrumentCalendarUnresolvedError(
            "strict ETF daily synchronization requires a calendar date and aware data_cutoff"
        )
    if calendar_for_code is not None and calendar_ids is None:
        raise InstrumentCalendarUnresolvedError(
            "test calendar_for_code injection requires explicit calendar_ids"
        )
    identity_code_map = (
        _load_identity_calendar_by_code(
            effective_date=as_of_date,
            data_cutoff=data_cutoff,
        )
        if calendar_for_code is None
        else {}
    )
    resolver = calendar_for_code or identity_code_map.get
    requested_ids = calendar_ids if calendar_ids is not None else identity_code_map.values()
    try:
        canonical_ids = tuple(sorted({normalize_calendar_id(value) for value in requested_ids}))
    except Exception as exc:
        raise InstrumentCalendarUnresolvedError(
            "ETF identity resolution returned an invalid calendar_id"
        ) from exc
    if not canonical_ids:
        raise InstrumentCalendarUnresolvedError("ETF calendar set is empty")
    totals = {"days": 0, "received": 0, "changed": 0, "unchanged": 0,
              "inserted": 0, "corrected": 0, "metadata_backfilled": 0}
    batch_revisions: list[str] = []
    first_date: date | None = None
    last_date: date | None = None
    bars_by_date: dict[date, list[EtfDailyBarInput]] = {}
    identity_maps_by_date: dict[date, dict[str, str]] = {}
    for calendar_id in canonical_ids:
        checkpoint = _load_checkpoint(sync_key, calendar_id=calendar_id)
        start_date = (
            _checkpoint_synced_through_date(checkpoint) + timedelta(days=1)
            if checkpoint is not None
            else (initial_start_date or as_of_date)
        )
        dates = _load_open_dates(
            calendar_id=calendar_id,
            start_date=start_date,
            end_date=as_of_date,
            data_cutoff=data_cutoff,
        )
        checkpoint_before = (
            _checkpoint_synced_through_date(checkpoint).isoformat()
            if checkpoint is not None
            else None
        )
        plan_start = dates[0] if dates else start_date
        plan_end = dates[-1] if dates else as_of_date
        checkpoint_scope = f"calendar_id={calendar_id}"
        logger.info(
            "etf_daily_calendar_planned",
            message=(
                f"ETF 日线采集计划：{calendar_id}，{plan_start.isoformat()} 至 {plan_end.isoformat()}，"
                f"拉取 0 条，变更 0 条，未变更 0 条，失败 0 条，checkpoint 未推进。"
            ),
            title="ETF 日线采集计划",
            data_type="etf_daily",
            calendar_id=calendar_id,
            start_date=plan_start.isoformat(),
            end_date=plan_end.isoformat(),
            fetched_count=0,
            changed_count=0,
            unchanged_count=0,
            failed_count=0,
            checkpoint_scope=checkpoint_scope,
            checkpoint_before=checkpoint_before,
            checkpoint_after=checkpoint_before,
            checkpoint_advanced=False,
            source=TUSHARE_SOURCE,
            source_revision=None,
            reconciliation_range=None,
            days_planned=len(dates),
        )
        for trading_date in dates:
            date_text = trading_date.isoformat()
            day_checkpoint_before = checkpoint_before
            logger.info(
                "etf_daily_calendar_started",
                message=(
                    f"开始采集 ETF 日线：{calendar_id}，{date_text} 至 {date_text}，"
                    "拉取 0 条，变更 0 条，未变更 0 条，失败 0 条，checkpoint 未推进。"
                ),
                title="开始 ETF 日线采集",
                data_type="etf_daily",
                calendar_id=calendar_id,
                start_date=date_text,
                end_date=date_text,
                fetched_count=0,
                changed_count=0,
                unchanged_count=0,
                failed_count=0,
                checkpoint_scope=checkpoint_scope,
                checkpoint_before=day_checkpoint_before,
                checkpoint_after=day_checkpoint_before,
                checkpoint_advanced=False,
                source=TUSHARE_SOURCE,
                source_revision=None,
                reconciliation_range=None,
            )
            try:
                if trading_date not in bars_by_date:
                    tushare_request_pacer.wait_for_turn(_effective_interval(request_interval_ms))
                    bars_by_date[trading_date] = normalize_etf_daily(
                        fetch_etf_daily_for_trade_date(
                            client,
                            trade_date=trading_date.strftime("%Y%m%d"),
                        ),
                        expected_trade_date=trading_date,
                    )
                source_bars = bars_by_date[trading_date]
                bars: list[EtfDailyBarInput] = []
                # Identity is effective-day PIT data.  Resolve it for the day
                # being committed rather than reusing the map from the run's
                # terminal date when a historical calendar assignment changed.
                effective_day_map = None
                if calendar_for_code is None:
                    effective_day_map = identity_maps_by_date.get(trading_date)
                    if effective_day_map is None:
                        effective_day_map = _load_identity_calendar_by_code(
                            effective_date=trading_date,
                            data_cutoff=data_cutoff,
                        )
                        identity_maps_by_date[trading_date] = effective_day_map
                for bar in source_bars:
                    resolved = (
                        resolver(bar.ts_code)
                        if calendar_for_code is not None
                        else (effective_day_map or {}).get(bar.ts_code)
                    )
                    if resolved is None:
                        raise InstrumentCalendarUnresolvedError(
                            "ETF daily response contains a source code without an identity calendar",
                            details={"ts_code": bar.ts_code},
                        )
                    try:
                        resolved = normalize_calendar_id(resolved)
                    except Exception as exc:
                        raise InstrumentCalendarUnresolvedError(
                            "ETF source code resolver returned an invalid calendar_id",
                            details={"ts_code": bar.ts_code},
                        ) from exc
                    if resolved == calendar_id:
                        bars.append(bar)
                if not bars:
                    raise InstrumentCalendarUnresolvedError(
                        "no ETF daily bars resolved for an identity-derived calendar",
                        details={"calendar_id": calendar_id, "trade_date": date_text},
                    )
                write_result, checkpoint = _commit_etf_daily_date(
                    bars=bars,
                    expected_checkpoint=checkpoint,
                    synced_through_date=trading_date,
                    sync_key=sync_key,
                    calendar_id=calendar_id,
                )
            except Exception:
                logger.exception(
                    "etf_daily_calendar_failed",
                    message=(
                        f"ETF 日线采集失败：{calendar_id}，{date_text} 至 {date_text}，"
                        "拉取 0 条，变更 0 条，未变更 0 条，失败 1 条，checkpoint 未推进。"
                    ),
                    title="ETF 日线采集失败",
                    data_type="etf_daily",
                    calendar_id=calendar_id,
                    start_date=date_text,
                    end_date=date_text,
                    fetched_count=0,
                    changed_count=0,
                    unchanged_count=0,
                    failed_count=1,
                    checkpoint_scope=checkpoint_scope,
                    checkpoint_before=day_checkpoint_before,
                    checkpoint_after=day_checkpoint_before,
                    checkpoint_advanced=False,
                    source=TUSHARE_SOURCE,
                    source_revision=None,
                    reconciliation_range=None,
                )
                raise
            totals["days"] += 1
            totals["received"] += write_result.received
            totals["changed"] += write_result.changed
            totals["unchanged"] += write_result.unchanged
            totals["inserted"] += write_result.inserted
            totals["corrected"] += write_result.corrected
            totals["metadata_backfilled"] += write_result.metadata_backfilled
            if write_result.batch_revision:
                batch_revisions.append(write_result.batch_revision)
            first_date = trading_date if first_date is None else min(first_date, trading_date)
            last_date = trading_date
            logger.info(
                "etf_daily_calendar_succeeded",
                message=(
                    f"完成 {calendar_id} ETF 日线采集：{date_text} 至 {date_text}，拉取 {write_result.received} 条，"
                    f"变更 {write_result.changed} 条，未变更 {write_result.unchanged} 条，失败 0 条，"
                    f"revision {write_result.batch_revision or '无'}，影响范围 "
                    f"{(write_result.affected_start_date or trading_date).isoformat()} 至 "
                    f"{(write_result.affected_end_date or trading_date).isoformat()}，"
                    f"checkpoint 已推进至 {date_text}。"
                ),
                title="ETF 日线采集完成",
                data_type="etf_daily",
                calendar_id=calendar_id,
                start_date=date_text,
                end_date=date_text,
                fetched_count=write_result.received,
                changed_count=write_result.changed,
                inserted_count=write_result.inserted,
                corrected_count=write_result.corrected,
                metadata_backfilled_count=write_result.metadata_backfilled,
                unchanged_count=write_result.unchanged,
                failed_count=0,
                checkpoint_scope=checkpoint_scope,
                checkpoint_before=day_checkpoint_before,
                checkpoint_after=_checkpoint_synced_through_date(checkpoint).isoformat(),
                checkpoint_advanced=True,
                source=TUSHARE_SOURCE,
                source_revision=write_result.batch_revision,
                batch_revision=write_result.batch_revision,
                reconciliation_range={
                    "start_date": (write_result.affected_start_date or trading_date).isoformat(),
                    "end_date": (write_result.affected_end_date or trading_date).isoformat(),
                },
            )
            checkpoint_before = _checkpoint_synced_through_date(checkpoint).isoformat()
    return EtfDailySyncResult(
        days_completed=totals["days"],
        received=totals["received"],
        changed=totals["changed"],
        unchanged=totals["unchanged"],
        synced_through_date=last_date,
        start_date=first_date,
        end_date=last_date,
        calendar_ids=canonical_ids,
        inserted=totals["inserted"],
        corrected=totals["corrected"],
        metadata_backfilled=totals["metadata_backfilled"],
        batch_revision=batch_revisions[-1] if batch_revisions else None,
        affected_start_date=first_date,
        affected_end_date=last_date,
    )


def _effective_interval(request_interval_ms: int | None) -> int:
    """Apply the configured request-pacing floor."""

    return max(get_settings().ingestion_request_interval_ms, request_interval_ms or 0)


def sync_etf_daily(
    client: TushareClient,
    *,
    request_interval_ms: int | None = None,
    as_of_date: date | None = None,
) -> EtfDailySyncResult:
    """Backward-compatible alias for incremental ETF daily synchronization."""
    return sync_etf_daily_incremental(
        client,
        request_interval_ms=request_interval_ms,
        as_of_date=as_of_date,
    )


def normalize_etf_daily(
    dataframe: "DataFrame", *, expected_trade_date: date
) -> list[EtfDailyBarInput]:
    """Validate one full-market API response before any database write occurs."""
    rows = dataframe.to_dict(orient="records")
    if len(rows) >= MAX_ETF_DAILY_ROWS:
        raise ValueError(
            f"Tushare ETF daily response may be truncated at {MAX_ETF_DAILY_ROWS} rows"
        )
    bars = [_normalize_etf_daily_row(row) for row in rows]
    if any(bar.trade_date != expected_trade_date for bar in bars):
        raise ValueError("Tushare ETF daily response contains another trade date")
    _reject_duplicate_bar_keys(bars)
    return bars


def _load_checkpoint(sync_key: str, *, calendar_id: str | None = None) -> DataSyncCheckpointState | None:
    with Session(get_engine()) as session:
        scope = ETF_DAILY_SCOPE_KEY if calendar_id is None else f"calendar_id={normalize_calendar_id(calendar_id)}"
        return DataSyncCheckpointRepository(session).get(sync_key, scope)


def _load_open_dates(
    *,
    calendar_id: str | None = None,
    start_date: date,
    end_date: date,
    data_cutoff: datetime | None = None,
) -> list[date]:
    """Load open dates from the identity-bound named-calendar fact chain."""

    if calendar_id is None or data_cutoff is None:
        raise InstrumentCalendarUnresolvedError(
            "ETF synchronization requires an identity-derived calendar_id and data_cutoff"
        )
    if start_date > end_date:
        return []
    with Session(get_engine()) as session:
        return CalendarFactRepository(session).list_open_dates(
            calendar_id=normalize_calendar_id(calendar_id),
            start_date=start_date,
            end_date=end_date,
            data_cutoff=data_cutoff,
        )


def _load_earliest_etf_list_date() -> date:
    """Require reference data before launching an ETF daily full backfill."""
    with Session(get_engine()) as session:
        earliest_list_date = EtfCodeRepository(session).earliest_list_date(
            source=TUSHARE_SOURCE
        )
    if earliest_list_date is None:
        raise ValueError(
            "ETF basic data is required before starting a full ETF daily sync"
        )
    return earliest_list_date


def _commit_etf_daily_date(
    *,
    bars: list[EtfDailyBarInput],
    expected_checkpoint: DataSyncCheckpointState | None,
    synced_through_date: date,
    sync_key: str,
    full_cycle_target_date: date | None = None,
    calendar_id: str | None = None,
) -> tuple[EtfDailyBarUpsertResult, DataSyncCheckpointState]:
    """Commit one complete ETF session and its checkpoint atomically."""
    with Session(get_engine()) as session:
        try:
            accepted_at = datetime.now(UTC)
            write_result = EtfDailyBarRepository(session).upsert_bars(
                bars, source=TUSHARE_SOURCE, accepted_at=accepted_at
            )
            checkpoint = DataSyncCheckpointRepository(session).advance(
                sync_key=sync_key,
                scope_key=(ETF_DAILY_SCOPE_KEY if calendar_id is None else f"calendar_id={normalize_calendar_id(calendar_id)}"),
                cursor={
                    "synced_through_date": synced_through_date.isoformat(),
                    **(
                        {"target_through_date": full_cycle_target_date.isoformat()}
                        if full_cycle_target_date is not None
                        else {}
                    ),
                },
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


def _initialize_full_cycle(
    *,
    expected_checkpoint: DataSyncCheckpointState | None,
    initial_start_date: date,
    target_through_date: date,
) -> DataSyncCheckpointState:
    """Persist a new full-cycle target before its first source request starts.

    A dedicated initialization transaction makes an interrupted first request
    resumable against the same historical endpoint. The date before the initial
    boundary is only a cursor position; no market-data row is implied by it.
    """
    with Session(get_engine()) as session:
        try:
            checkpoint = DataSyncCheckpointRepository(session).advance(
                sync_key=ETF_DAILY_FULL_SYNC_KEY,
                scope_key=ETF_DAILY_SCOPE_KEY,
                cursor={
                    "synced_through_date": (
                        initial_start_date - timedelta(days=1)
                    ).isoformat(),
                    "target_through_date": target_through_date.isoformat(),
                },
                expected_version=(
                    expected_checkpoint.version
                    if expected_checkpoint is not None
                    else None
                ),
            )
            session.commit()
            return checkpoint
        except Exception:
            session.rollback()
            raise


def _normalize_etf_daily_row(row: object) -> EtfDailyBarInput:
    if not isinstance(row, dict):
        raise ValueError("Tushare ETF daily response row is not an object")
    open_price = _parse_non_negative_decimal(row.get("open"), "open")
    high = _parse_non_negative_decimal(row.get("high"), "high")
    low = _parse_non_negative_decimal(row.get("low"), "low")
    close = _parse_non_negative_decimal(row.get("close"), "close")
    if high < low:
        raise ValueError("Tushare ETF daily record has high below low")
    return EtfDailyBarInput(
        ts_code=_parse_required_text(row.get("ts_code"), "ts_code"),
        trade_date=_parse_date(row.get("trade_date")),
        open=open_price,
        high=high,
        low=low,
        close=close,
        vol=_parse_non_negative_decimal(row.get("vol"), "vol"),
        amount=_parse_non_negative_decimal(row.get("amount"), "amount"),
    )


def _parse_required_text(value: object, field_name: str) -> str:
    normalized = str(value).strip() if value is not None else ""
    if normalized.lower() in {"", "nan", "nat", "none"}:
        raise ValueError(f"Tushare ETF daily record has no {field_name}")
    return normalized


def _parse_date(value: object) -> date:
    try:
        return date.fromisoformat(_parse_required_text(value, "trade_date"))
    except ValueError as exc:
        raise ValueError("Tushare ETF daily record has an invalid trade_date") from exc


def _parse_non_negative_decimal(value: object, field_name: str) -> Decimal:
    try:
        parsed = Decimal(_parse_required_text(value, field_name))
    except InvalidOperation as exc:
        raise ValueError(
            f"Tushare ETF daily record has an invalid {field_name}"
        ) from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(
            f"Tushare ETF daily record has an invalid {field_name}"
        )
    return parsed


def _reject_duplicate_bar_keys(bars: list[EtfDailyBarInput]) -> None:
    keys = [(bar.ts_code, bar.trade_date) for bar in bars]
    if len(keys) != len(set(keys)):
        raise ValueError("Tushare ETF daily response contains duplicate ETF bars")


def _checkpoint_synced_through_date(checkpoint: DataSyncCheckpointState) -> date:
    value = checkpoint.cursor.get("synced_through_date")
    if not isinstance(value, str):
        raise ValueError("ETF daily checkpoint has no synced_through_date")
    return date.fromisoformat(value)


def _full_checkpoint_target_date(checkpoint: DataSyncCheckpointState) -> date:
    value = checkpoint.cursor.get("target_through_date")
    if not isinstance(value, str):
        raise ValueError("ETF daily full checkpoint has no target_through_date")
    return date.fromisoformat(value)


def _full_cycle_is_complete(checkpoint: DataSyncCheckpointState) -> bool:
    return _checkpoint_synced_through_date(checkpoint) >= _full_checkpoint_target_date(
        checkpoint
    )
