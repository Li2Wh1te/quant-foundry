"""ETF adjustment-factor retrieval and whole-market synchronization."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session
import structlog

from app.core.config import get_settings
from app.data_ingestion.clients.tushare import TushareClient
from app.data_ingestion.constants import (
    ETF_ADJUSTMENT_FULL_SYNC_KEY,
    ETF_ADJUSTMENT_INCREMENTAL_SYNC_KEY,
    TUSHARE_SOURCE,
)
from app.data_ingestion.repositories.etf import EtfCodeRepository
from app.data_ingestion.models.etf import EtfCode
from app.instruments.models import InstrumentIdentityFactRecord
from app.data_ingestion.repositories.etf_adjustment import (
    EtfAdjustmentFactorRepository,
)
from app.data_ingestion.repositories.sync_checkpoint import DataSyncCheckpointRepository
from app.data_ingestion.request_pacing import tushare_request_pacer
from app.data_ingestion.schemas.etf_adjustment import (
    EtfAdjustmentFactorInput,
    EtfAdjustmentFactorUpsertResult,
    EtfAdjustmentSyncResult,
)
from app.data_ingestion.schemas.trading_calendar import DataSyncCheckpointState
from app.backtesting.calendar_axis import normalize_calendar_id
from app.backtesting.data.calendar_repository import CalendarFactRepository
from app.backtesting.data.errors import InstrumentCalendarUnresolvedError
from app.db.session import get_engine

if TYPE_CHECKING:
    from pandas import DataFrame


ETF_ADJUSTMENT_SCOPE_KEY = "market=CN"
# No market-wide exchange is a valid strict calendar source.  The old
# exchange-level task remains only as a migration facade and fails closed
# unless callers supply the identity-derived calendar_id set.
ETF_ADJUSTMENT_CALENDAR_EXCHANGE: str | None = None
ETF_ADJUSTMENT_PAGE_SIZE = 2_000
DEFAULT_RECONCILIATION_LOOKBACK_DAYS = 60
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
logger = structlog.get_logger(__name__)


def fetch_etf_adjustment_factors(
    client: TushareClient,
    *,
    ts_code: str,
    start_date: str,
    end_date: str,
) -> "DataFrame":
    """Fetch raw ETF adjustment factors for one code and date range.

    This preserves Tushare's documented ``fund_adj`` example for targeted repair.
    Whole-market scheduled work uses ``fetch_etf_adjustment_factors_for_trade_date``
    so one paginated request covers every ETF for a completed trading session.
    """
    return client.pro.fund_adj(
        ts_code=ts_code,
        start_date=start_date,
        end_date=end_date,
    )


def fetch_etf_adjustment_factors_for_trade_date(
    client: TushareClient,
    *,
    trade_date: str,
    offset: int,
    limit: int = ETF_ADJUSTMENT_PAGE_SIZE,
) -> "DataFrame":
    """Fetch one paginated whole-market factor page for a trading date."""
    return client.pro.fund_adj(trade_date=trade_date, offset=offset, limit=limit)


def sync_etf_adjustment_factors(
    client: TushareClient,
    *,
    ts_code: str,
    start_date: str,
    end_date: str,
    request_interval_ms: int | None = None,
) -> EtfAdjustmentFactorUpsertResult:
    """Fetch one code range and atomically upsert its latest source values."""
    effective_interval_ms = _effective_request_interval_ms(request_interval_ms)
    logger.info(
        "etf_adjustment_sync_started",
        message=(
            f"开始采集 ETF 复权因子：{ts_code}，{start_date} 至 {end_date}，拉取 0 条，"
            "变更 0 条，未变更 0 条，失败 0 条，checkpoint 未推进。"
        ),
        title="开始 ETF 复权因子采集",
        data_type="etf_adjustment",
        calendar_id=None,
        ts_code=ts_code,
        start_date=start_date,
        end_date=end_date,
        fetched_count=0,
        changed_count=0,
        unchanged_count=0,
        failed_count=0,
        checkpoint_scope=f"ts_code={ts_code}",
        checkpoint_before=None,
        checkpoint_after=None,
        checkpoint_advanced=False,
        source=TUSHARE_SOURCE,
        source_revision=None,
        reconciliation_range=None,
    )
    try:
        tushare_request_pacer.wait_for_turn(effective_interval_ms)
        factors = normalize_etf_adjustment_factors(
            fetch_etf_adjustment_factors(
                client,
                ts_code=ts_code,
                start_date=start_date,
                end_date=end_date,
            ),
            expected_ts_code=ts_code,
        )
        if not factors:
            raise ValueError("Tushare returned no ETF adjustment factors")
        result = _commit_etf_adjustment_factors(factors)
    except Exception:
        logger.exception(
            "etf_adjustment_sync_failed",
            message=(
                f"采集 ETF 复权因子失败：{ts_code}，{start_date} 至 {end_date}，拉取 0 条，"
                "变更 0 条，未变更 0 条，失败 1 条，checkpoint 未推进。"
            ),
            title="ETF 复权因子采集失败",
            data_type="etf_adjustment",
            calendar_id=None,
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date,
            fetched_count=0,
            changed_count=0,
            unchanged_count=0,
            failed_count=1,
            checkpoint_scope=f"ts_code={ts_code}",
            checkpoint_before=None,
            checkpoint_after=None,
            checkpoint_advanced=False,
            source=TUSHARE_SOURCE,
            source_revision=None,
            reconciliation_range=None,
        )
        raise
    logger.info(
        "etf_adjustment_sync_succeeded",
        message=(
            f"完成采集 ETF 复权因子：{ts_code}，{start_date} 至 {end_date}，"
            f"拉取 {result.received} 条，入库变更 {result.changed} 条，"
            f"未变更 {result.unchanged} 条，失败 0 条，checkpoint 未推进。"
        ),
        title="ETF 复权因子采集完成",
        data_type="etf_adjustment",
        calendar_id=None,
        ts_code=ts_code,
        start_date=start_date,
        end_date=end_date,
        fetched_count=result.received,
        changed_count=result.changed,
        unchanged_count=result.unchanged,
        failed_count=0,
        checkpoint_scope=f"ts_code={ts_code}",
        checkpoint_before=None,
        checkpoint_after=None,
        checkpoint_advanced=False,
        source=TUSHARE_SOURCE,
        source_revision=None,
        reconciliation_range=None,
        received=result.received,
        changed=result.changed,
        unchanged=result.unchanged,
    )
    return result


def _load_identity_calendar_by_code(
    *,
    effective_date: date,
    data_cutoff: datetime,
) -> dict[str, str]:
    """Resolve each ETF source code from one visible identity fact.

    The effective-day and PIT predicates are applied before each logical
    identity chain is folded.  Missing, malformed, or ambiguous identity
    facts fail closed; no exchange name is allowed to choose a calendar.
    """

    if (
        not isinstance(effective_date, date)
        or isinstance(effective_date, datetime)
        or not isinstance(data_cutoff, datetime)
        or data_cutoff.tzinfo is None
        or data_cutoff.utcoffset() is None
    ):
        raise InstrumentCalendarUnresolvedError(
            "ETF calendar resolution requires a calendar date and aware data_cutoff"
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
            raise InstrumentCalendarUnresolvedError("ETF identity fact has no source code")
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

    selected: dict[str, list[tuple[object, str, str]]] = {}
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


def sync_etf_adjustment_by_calendar(
    client: TushareClient,
    *,
    data_cutoff: datetime,
    as_of_date: date,
    calendar_ids: Iterable[str] | None = None,
    calendar_for_code: Callable[[str], str | None] | None = None,
    request_interval_ms: int | None = None,
    sync_key: str | None = ETF_ADJUSTMENT_INCREMENTAL_SYNC_KEY,
    initial_start_date: date | None = None,
    lookback_trading_days: int | None = None,
) -> EtfAdjustmentSyncResult:
    """Synchronize adjustment factors using identity-derived calendars.

    This is the strict scheduled entry point.  It rejects missing identity
    facts instead of falling back to an exchange-wide SSE calendar.  A single
    date response is cached and partitioned by canonical calendar so multiple
    calendars do not cause duplicate source requests.
    """

    if (
        not isinstance(as_of_date, date)
        or isinstance(as_of_date, datetime)
        or not isinstance(data_cutoff, datetime)
        or data_cutoff.tzinfo is None
        or data_cutoff.utcoffset() is None
    ):
        raise InstrumentCalendarUnresolvedError(
            "strict ETF adjustment synchronization requires a calendar date and aware data_cutoff"
        )
    if calendar_for_code is not None and calendar_ids is None:
        raise InstrumentCalendarUnresolvedError(
            "test calendar_for_code injection requires explicit calendar_ids"
        )
    identity_code_map: dict[str, str] = {}
    if calendar_for_code is None:
        identity_code_map = _load_identity_calendar_by_code(
            effective_date=as_of_date,
            data_cutoff=data_cutoff,
        )
    resolver = calendar_for_code or identity_code_map.get
    requested_ids = calendar_ids if calendar_ids is not None else identity_code_map.values()
    try:
        resolved_calendar_ids = {
            normalize_calendar_id(value) for value in requested_ids
        }
    except Exception as exc:
        raise InstrumentCalendarUnresolvedError(
            "ETF identity resolution returned an invalid calendar_id"
        ) from exc
    if not resolved_calendar_ids:
        raise InstrumentCalendarUnresolvedError("ETF calendar set is empty")
    effective_interval = _effective_request_interval_ms(request_interval_ms)
    cache: dict[date, list[EtfAdjustmentFactorInput]] = {}
    identity_maps_by_date: dict[date, dict[str, str]] = {}
    total_received = total_changed = total_unchanged = total_days = 0
    first_date: date | None = None
    last_date: date | None = None
    operation = (
        "reconciliation"
        if sync_key is None
        else "full"
        if sync_key == ETF_ADJUSTMENT_FULL_SYNC_KEY
        else "incremental"
    )
    for calendar_id in sorted(resolved_calendar_ids):
        canonical = normalize_calendar_id(calendar_id)
        checkpoint = _load_checkpoint(sync_key, calendar_id=canonical) if sync_key is not None else None
        start_date = (
            _checkpoint_synced_through_date(checkpoint) + timedelta(days=1)
            if checkpoint is not None
            else (initial_start_date or as_of_date)
        )
        dates = _load_open_dates(
            calendar_id=canonical,
            start_date=start_date,
            end_date=as_of_date,
            data_cutoff=data_cutoff,
        )
        if lookback_trading_days is not None:
            dates = dates[-lookback_trading_days:]
        checkpoint_before = (
            _checkpoint_synced_through_date(checkpoint).isoformat()
            if checkpoint is not None
            else None
        )
        checkpoint_scope = f"calendar_id={canonical}"
        plan_start = dates[0] if dates else start_date
        plan_end = dates[-1] if dates else as_of_date
        reconciliation_range = (
            {
                "range_start": plan_start.isoformat(),
                "range_end": (plan_end + timedelta(days=1)).isoformat(),
            }
            if operation == "reconciliation"
            else None
        )
        logger.info(
            f"etf_adjustment_{operation}_planned",
            message=(
                f"ETF 复权因子{('修订校验' if operation == 'reconciliation' else '采集')}计划：{canonical}，"
                f"{plan_start.isoformat()} 至 {plan_end.isoformat()}，拉取 0 条，变更 0 条，未变更 0 条，"
                "失败 0 条，checkpoint 未推进。"
            ),
            title=("ETF 复权因子修订校验计划" if operation == "reconciliation" else "ETF 复权因子采集计划"),
            data_type="etf_adjustment",
            calendar_id=canonical,
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
            reconciliation_range=reconciliation_range,
            days_planned=len(dates),
        )
        for trading_date in dates:
            date_text = trading_date.isoformat()
            day_checkpoint_before = checkpoint_before
            day_reconciliation_range = (
                {
                    "range_start": date_text,
                    "range_end": (trading_date + timedelta(days=1)).isoformat(),
                }
                if operation == "reconciliation"
                else None
            )
            logger.info(
                f"etf_adjustment_{operation}_started",
                message=(
                    f"开始 ETF 复权因子{('修订校验' if operation == 'reconciliation' else '采集')}：{canonical}，"
                    f"{date_text} 至 {date_text}，拉取 0 条，变更 0 条，未变更 0 条，失败 0 条，checkpoint 未推进。"
                ),
                title=("开始 ETF 复权因子修订校验" if operation == "reconciliation" else "开始 ETF 复权因子采集"),
                data_type="etf_adjustment",
                calendar_id=canonical,
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
                reconciliation_range=day_reconciliation_range,
            )
            try:
                if trading_date not in cache:
                    cache[trading_date] = _fetch_market_factors_for_trade_date(
                        client,
                        trade_date=trading_date,
                        request_interval_ms=effective_interval,
                    )
                factors: list[EtfAdjustmentFactorInput] = []
                effective_day_map = None
                if calendar_for_code is None:
                    effective_day_map = identity_maps_by_date.get(trading_date)
                    if effective_day_map is None:
                        effective_day_map = _load_identity_calendar_by_code(
                            effective_date=trading_date,
                            data_cutoff=data_cutoff,
                        )
                        identity_maps_by_date[trading_date] = effective_day_map
                for factor in cache[trading_date]:
                    resolved = (
                        resolver(factor.ts_code)
                        if calendar_for_code is not None
                        else (effective_day_map or {}).get(factor.ts_code)
                    )
                    if resolved is None:
                        raise InstrumentCalendarUnresolvedError(
                            "ETF adjustment response contains a source code without an identity calendar",
                            details={"ts_code": factor.ts_code},
                        )
                    try:
                        resolved = normalize_calendar_id(resolved)
                    except Exception as exc:
                        raise InstrumentCalendarUnresolvedError(
                            "ETF source code resolver returned an invalid calendar_id",
                            details={"ts_code": factor.ts_code},
                        ) from exc
                    if resolved == canonical:
                        factors.append(factor)
                if not factors:
                    raise InstrumentCalendarUnresolvedError(
                        "ETF adjustment response has no rows for an identity-derived calendar",
                        details={"calendar_id": canonical, "trade_date": date_text},
                    )
                write_result, checkpoint = _commit_market_trade_date(
                    factors=factors,
                    expected_checkpoint=checkpoint,
                    synced_through_date=trading_date,
                    sync_key=sync_key,
                    calendar_id=canonical,
                )
            except Exception:
                logger.exception(
                    f"etf_adjustment_{operation}_failed",
                    message=(
                        f"ETF 复权因子{('修订校验' if operation == 'reconciliation' else '采集')}失败：{canonical}，"
                        f"{date_text} 至 {date_text}，拉取 0 条，变更 0 条，未变更 0 条，失败 1 条，checkpoint 未推进。"
                    ),
                    title=("ETF 复权因子修订校验失败" if operation == "reconciliation" else "ETF 复权因子采集失败"),
                    data_type="etf_adjustment",
                    calendar_id=canonical,
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
                    reconciliation_range=day_reconciliation_range,
                )
                raise
            total_received += write_result.received
            total_changed += write_result.changed
            total_unchanged += write_result.unchanged
            total_days += 1
            first_date = trading_date if first_date is None else min(first_date, trading_date)
            last_date = trading_date
            logger.info(
                (
                    "etf_adjustment_reconciliation_succeeded"
                    if operation == "reconciliation"
                    else "etf_adjustment_calendar_succeeded"
                ),
                message=(
                    f"完成 {canonical} ETF 复权因子{('修订校验' if operation == 'reconciliation' else '采集')}："
                    f"{trading_date.isoformat()} 至 {trading_date.isoformat()}，"
                    f"拉取 {write_result.received} 条，变更 {write_result.changed} 条，未变更 {write_result.unchanged} 条，"
                    f"失败 0 条，"
                    + (
                        f"checkpoint 已推进至 {trading_date.isoformat()}。"
                        if operation != "reconciliation" and checkpoint is not None
                        else "checkpoint 未推进。"
                    )
                ),
                title=("ETF 复权因子修订校验完成" if operation == "reconciliation" else "ETF 复权因子采集完成"),
                data_type="etf_adjustment",
                calendar_id=canonical,
                start_date=trading_date.isoformat(),
                end_date=trading_date.isoformat(),
                fetched_count=write_result.received,
                changed_count=write_result.changed,
                unchanged_count=write_result.unchanged,
                failed_count=0,
                checkpoint_scope=checkpoint_scope,
                checkpoint_before=day_checkpoint_before,
                checkpoint_after=(
                    _checkpoint_synced_through_date(checkpoint).isoformat()
                    if operation != "reconciliation" and checkpoint is not None
                    else day_checkpoint_before
                ),
                checkpoint_advanced=operation != "reconciliation" and checkpoint is not None,
                source=TUSHARE_SOURCE,
                source_revision=None,
                reconciliation_range=day_reconciliation_range,
            )
            checkpoint_before = _checkpoint_synced_through_date(checkpoint).isoformat() if checkpoint is not None else day_checkpoint_before
    return EtfAdjustmentSyncResult(
        days_completed=total_days,
        received=total_received,
        changed=total_changed,
        unchanged=total_unchanged,
        synced_through_date=last_date,
        start_date=first_date,
        end_date=last_date,
        calendar_ids=tuple(sorted(resolved_calendar_ids)),
    )


def sync_etf_adjustment_incremental(
    client: TushareClient,
    *,
    request_interval_ms: int | None = None,
    as_of_date: date | None = None,
    calendar_ids: Iterable[str] | None = None,
    calendar_for_code: Callable[[str], str | None] | None = None,
    data_cutoff: datetime | None = None,
) -> EtfAdjustmentSyncResult:
    """Synchronize factors after identity-derived calendar resolution.

    The previous no-argument market-wide entry point is intentionally blocked
    before any clock, checkpoint, or calendar read.  Strict callers must carry
    a PIT cutoff; identity facts then derive the calendar partition.
    """
    if data_cutoff is None:
        raise InstrumentCalendarUnresolvedError(
            "strict ETF adjustment synchronization requires data_cutoff"
        )
    return sync_etf_adjustment_by_calendar(
        client,
        data_cutoff=data_cutoff,
        as_of_date=as_of_date or _completed_through_date(None, data_cutoff=data_cutoff),
        calendar_ids=calendar_ids,
        calendar_for_code=calendar_for_code,
        request_interval_ms=request_interval_ms,
    )


def sync_etf_adjustment_full(
    client: TushareClient,
    *,
    request_interval_ms: int | None = None,
    as_of_date: date | None = None,
    data_cutoff: datetime | None = None,
    calendar_ids: Iterable[str] | None = None,
    calendar_for_code: Callable[[str], str | None] | None = None,
) -> EtfAdjustmentSyncResult:
    """Run a full historical cycle with independent identity/calendar cursors."""
    if data_cutoff is None:
        raise InstrumentCalendarUnresolvedError(
            "strict ETF adjustment synchronization requires data_cutoff"
        )
    completed_through_date = _completed_through_date(
        as_of_date,
        data_cutoff=data_cutoff,
    )
    return sync_etf_adjustment_by_calendar(
        client,
        data_cutoff=data_cutoff,
        as_of_date=completed_through_date,
        calendar_ids=calendar_ids,
        calendar_for_code=calendar_for_code,
        request_interval_ms=request_interval_ms,
        sync_key=ETF_ADJUSTMENT_FULL_SYNC_KEY,
        initial_start_date=_load_earliest_etf_list_date(),
    )


def sync_etf_adjustment_reconciliation(
    client: TushareClient,
    *,
    lookback_trading_days: int = DEFAULT_RECONCILIATION_LOOKBACK_DAYS,
    request_interval_ms: int | None = None,
    as_of_date: date | None = None,
    data_cutoff: datetime | None = None,
    calendar_ids: Iterable[str] | None = None,
    calendar_for_code: Callable[[str], str | None] | None = None,
) -> EtfAdjustmentSyncResult:
    """Re-fetch recent sessions for source corrections by identity calendar."""
    if lookback_trading_days <= 0:
        raise ValueError("lookback_trading_days must be positive")
    if data_cutoff is None:
        raise InstrumentCalendarUnresolvedError(
            "strict ETF adjustment synchronization requires data_cutoff"
        )
    target_through_date = _completed_through_date(
        as_of_date,
        data_cutoff=data_cutoff,
    )
    return sync_etf_adjustment_by_calendar(
        client,
        data_cutoff=data_cutoff,
        as_of_date=target_through_date,
        calendar_ids=calendar_ids,
        calendar_for_code=calendar_for_code,
        request_interval_ms=request_interval_ms,
        sync_key=None,
        initial_start_date=target_through_date - timedelta(days=lookback_trading_days * 3),
        lookback_trading_days=lookback_trading_days,
    )


def normalize_etf_adjustment_factors(
    dataframe: "DataFrame",
    *,
    expected_ts_code: str | None = None,
    expected_trade_date: date | None = None,
) -> list[EtfAdjustmentFactorInput]:
    """Validate and normalize one Tushare adjustment-factor response page."""
    factors = [_normalize_factor_row(row) for row in dataframe.to_dict(orient="records")]
    if expected_ts_code is not None and any(
        factor.ts_code != expected_ts_code for factor in factors
    ):
        raise ValueError("Tushare ETF adjustment response contains another ts_code")
    if expected_trade_date is not None and any(
        factor.trade_date != expected_trade_date for factor in factors
    ):
        raise ValueError("Tushare ETF adjustment response contains another trade_date")
    _reject_duplicate_factor_keys(factors)
    return factors


def _sync_market_dates(
    client: TushareClient,
    *,
    start_date: date,
    target_through_date: date,
    checkpoint: DataSyncCheckpointState | None,
    sync_key: str | None,
    event_prefix: str,
    task_label: str,
    request_interval_ms: int | None,
    full_cycle_target_date: date | None = None,
    trading_dates: list[date] | None = None,
) -> EtfAdjustmentSyncResult:
    """Fetch complete whole-market pages for each requested completed session."""
    effective_interval_ms = _effective_request_interval_ms(request_interval_ms)
    dates = trading_dates if trading_dates is not None else _load_open_dates(
        start_date=start_date,
        end_date=target_through_date,
    )
    logger.info(
        f"{event_prefix}_planned",
        message=(
            f"{task_label}采集计划：{start_date.isoformat()} 至 "
            f"{target_through_date.isoformat()}，共 {len(dates)} 个交易日，拉取 0 条，"
            "变更 0 条，未变更 0 条，失败 0 条，checkpoint 未推进。"
        ),
        title=f"{task_label}采集计划",
        data_type="etf_adjustment",
        calendar_id=None,
        start_date=start_date.isoformat(),
        end_date=target_through_date.isoformat(),
        fetched_count=0,
        changed_count=0,
        unchanged_count=0,
        failed_count=0,
        checkpoint_scope="market=CN",
        checkpoint_before=(
            _checkpoint_synced_through_date(checkpoint).isoformat()
            if checkpoint is not None
            else None
        ),
        checkpoint_after=(
            _checkpoint_synced_through_date(checkpoint).isoformat()
            if checkpoint is not None
            else None
        ),
        checkpoint_advanced=False,
        source=TUSHARE_SOURCE,
        source_revision=None,
        reconciliation_range=None,
        days_planned=len(dates),
        request_interval_ms=effective_interval_ms,
    )
    received = changed = unchanged = 0
    for trading_date in dates:
        date_text = trading_date.isoformat()
        day_checkpoint_before = (
            _checkpoint_synced_through_date(checkpoint).isoformat()
            if checkpoint is not None
            else None
        )
        logger.info(
            f"{event_prefix}_started",
            message=(
                f"开始采集 {task_label}：{date_text} 至 {date_text}，拉取 0 条，变更 0 条，"
                "未变更 0 条，失败 0 条，checkpoint 未推进。"
            ),
            title=f"开始{task_label}采集",
            data_type="etf_adjustment",
            calendar_id=None,
            start_date=date_text,
            end_date=date_text,
            fetched_count=0,
            changed_count=0,
            unchanged_count=0,
            failed_count=0,
            checkpoint_scope="market=CN",
            checkpoint_before=day_checkpoint_before,
            checkpoint_after=day_checkpoint_before,
            checkpoint_advanced=False,
            source=TUSHARE_SOURCE,
            source_revision=None,
            reconciliation_range=None,
        )
        try:
            factors = _fetch_market_factors_for_trade_date(
                client,
                trade_date=trading_date,
                request_interval_ms=effective_interval_ms,
            )
            if not factors:
                raise ValueError("Tushare returned no ETF adjustment factors")
            write_result, checkpoint = _commit_market_trade_date(
                factors=factors,
                expected_checkpoint=checkpoint,
                synced_through_date=trading_date,
                sync_key=sync_key,
                full_cycle_target_date=full_cycle_target_date,
            )
        except Exception:
            logger.exception(
                f"{event_prefix}_failed",
                message=(
                    f"采集 {task_label}失败：{date_text} 至 {date_text}，"
                    "拉取 0 条，变更 0 条，未变更 0 条，失败 1 条，checkpoint 未推进。"
                ),
                title=f"{task_label}采集失败",
                data_type="etf_adjustment",
                calendar_id=None,
                start_date=date_text,
                end_date=date_text,
                fetched_count=0,
                changed_count=0,
                unchanged_count=0,
                failed_count=1,
                checkpoint_scope="market=CN",
                checkpoint_before=day_checkpoint_before,
                checkpoint_after=day_checkpoint_before,
                checkpoint_advanced=False,
                source=TUSHARE_SOURCE,
                source_revision=None,
                reconciliation_range=None,
            )
            raise
        received += write_result.received
        changed += write_result.changed
        unchanged += write_result.unchanged
        cursor_message = (
            f"游标已推进至 {checkpoint.cursor['synced_through_date']}。"
            if checkpoint is not None
            else "未使用游标。"
        )
        logger.info(
            f"{event_prefix}_succeeded",
            message=(
                f"完成采集 {task_label}：{date_text} 至 {date_text}，"
                f"拉取 {write_result.received} 条，入库变更 {write_result.changed} 条，"
                f"未变更 {write_result.unchanged} 条，失败 0 条，{cursor_message}"
            ),
            title=f"{task_label}采集完成",
            data_type="etf_adjustment",
            calendar_id=None,
            start_date=date_text,
            end_date=date_text,
            received=write_result.received,
            changed=write_result.changed,
            unchanged=write_result.unchanged,
            synced_through_date=(
                checkpoint.cursor["synced_through_date"]
                if checkpoint is not None
                else None
            ),
            fetched_count=write_result.received,
            changed_count=write_result.changed,
            unchanged_count=write_result.unchanged,
            failed_count=0,
            checkpoint_scope="market=CN",
            checkpoint_before=day_checkpoint_before,
            checkpoint_after=(
                _checkpoint_synced_through_date(checkpoint).isoformat()
                if checkpoint is not None
                else None
            ),
            checkpoint_advanced=checkpoint is not None,
            source=TUSHARE_SOURCE,
            source_revision=None,
            reconciliation_range=None,
        )
    return EtfAdjustmentSyncResult(
        days_completed=len(dates),
        received=received,
        changed=changed,
        unchanged=unchanged,
        synced_through_date=(
            _checkpoint_synced_through_date(checkpoint)
            if checkpoint is not None
            else (dates[-1] if dates else None)
        ),
    )


def _fetch_market_factors_for_trade_date(
    client: TushareClient,
    *,
    trade_date: date,
    request_interval_ms: int,
) -> list[EtfAdjustmentFactorInput]:
    """Fetch every page for one date before exposing any partial result to writes."""
    offset = 0
    factors: list[EtfAdjustmentFactorInput] = []
    while True:
        tushare_request_pacer.wait_for_turn(request_interval_ms)
        page = normalize_etf_adjustment_factors(
            fetch_etf_adjustment_factors_for_trade_date(
                client,
                trade_date=trade_date.strftime("%Y%m%d"),
                offset=offset,
            ),
            expected_trade_date=trade_date,
        )
        factors.extend(page)
        if len(page) < ETF_ADJUSTMENT_PAGE_SIZE:
            break
        offset += ETF_ADJUSTMENT_PAGE_SIZE
    _reject_duplicate_factor_keys(factors)
    return factors


def _commit_etf_adjustment_factors(
    factors: list[EtfAdjustmentFactorInput],
) -> EtfAdjustmentFactorUpsertResult:
    """Commit all normalized targeted-repair factors in one transaction."""
    with Session(get_engine()) as session:
        try:
            result = EtfAdjustmentFactorRepository(session).upsert_factors(
                factors, source=TUSHARE_SOURCE
            )
            session.commit()
            return result
        except Exception:
            session.rollback()
            raise


def _commit_market_trade_date(
    *,
    factors: list[EtfAdjustmentFactorInput],
    expected_checkpoint: DataSyncCheckpointState | None,
    synced_through_date: date,
    sync_key: str | None,
    full_cycle_target_date: date | None = None,
    calendar_id: str | None = None,
) -> tuple[EtfAdjustmentFactorUpsertResult, DataSyncCheckpointState | None]:
    """Commit a complete date and, when required, its market cursor atomically."""
    with Session(get_engine()) as session:
        try:
            result = EtfAdjustmentFactorRepository(session).upsert_factors(
                factors, source=TUSHARE_SOURCE
            )
            checkpoint = (
                DataSyncCheckpointRepository(session).advance(
                    sync_key=sync_key,
                    scope_key=(
                        ETF_ADJUSTMENT_SCOPE_KEY
                        if calendar_id is None
                        else f"calendar_id={normalize_calendar_id(calendar_id)}"
                    ),
                    cursor={
                        "synced_through_date": synced_through_date.isoformat(),
                        **(
                            {
                                "target_through_date": full_cycle_target_date.isoformat()
                            }
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
                if sync_key is not None
                else None
            )
            session.commit()
            return result, checkpoint
        except Exception:
            session.rollback()
            raise


def _load_checkpoint(sync_key: str, *, calendar_id: str | None = None) -> DataSyncCheckpointState | None:
    with Session(get_engine()) as session:
        scope = ETF_ADJUSTMENT_SCOPE_KEY if calendar_id is None else f"calendar_id={normalize_calendar_id(calendar_id)}"
        return DataSyncCheckpointRepository(session).get(sync_key, scope)


def _load_open_dates(
    *,
    start_date: date,
    end_date: date,
    calendar_id: str | None = None,
    data_cutoff: datetime | None = None,
) -> list[date]:
    """Load open dates from the identity-bound named-calendar fact chain."""

    if calendar_id is None or data_cutoff is None:
        raise InstrumentCalendarUnresolvedError(
            "ETF adjustment synchronization requires an identity-derived calendar_id and data_cutoff"
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
    with Session(get_engine()) as session:
        earliest_list_date = EtfCodeRepository(session).earliest_list_date(
            source=TUSHARE_SOURCE
        )
    if earliest_list_date is None:
        raise ValueError(
            "ETF basic data is required before starting a full adjustment sync"
        )
    return earliest_list_date


def _initialize_full_cycle(
    *,
    expected_checkpoint: DataSyncCheckpointState | None,
    initial_start_date: date,
    target_through_date: date,
) -> DataSyncCheckpointState:
    """Freeze one full-run target before its first source request starts."""
    with Session(get_engine()) as session:
        try:
            checkpoint = DataSyncCheckpointRepository(session).advance(
                sync_key=ETF_ADJUSTMENT_FULL_SYNC_KEY,
                scope_key=ETF_ADJUSTMENT_SCOPE_KEY,
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


def _completed_through_date(
    as_of_date: date | None,
    *,
    data_cutoff: datetime | None = None,
) -> date:
    """Freeze the last complete local date without using a second PIT clock.

    Strict calendar callers derive the completion boundary from their explicit
    aware cutoff; the wall clock is retained only for the legacy diagnostic
    facade, which is blocked before this helper is reached.
    """

    if as_of_date is not None:
        if not isinstance(as_of_date, date) or isinstance(as_of_date, datetime):
            raise ValueError("as_of_date must be a calendar date")
        return as_of_date
    if data_cutoff is not None:
        if data_cutoff.tzinfo is None or data_cutoff.utcoffset() is None:
            raise InstrumentCalendarUnresolvedError(
                "ETF completion cutoff requires an aware data_cutoff"
            )
        return data_cutoff.astimezone(SHANGHAI_TIMEZONE).date() - timedelta(days=1)
    return datetime.now(SHANGHAI_TIMEZONE).date() - timedelta(days=1)


def _effective_request_interval_ms(request_interval_ms: int | None) -> int:
    return max(get_settings().ingestion_request_interval_ms, request_interval_ms or 0)


def _normalize_factor_row(row: object) -> EtfAdjustmentFactorInput:
    if not isinstance(row, dict):
        raise ValueError("Tushare ETF adjustment response row is not an object")
    return EtfAdjustmentFactorInput(
        ts_code=_required_text(row.get("ts_code"), "ts_code"),
        trade_date=_parse_date(row.get("trade_date")),
        adj_factor=_parse_positive_decimal(row.get("adj_factor"), "adj_factor"),
    )


def _required_text(value: object, field_name: str) -> str:
    normalized = str(value).strip() if value is not None else ""
    if normalized.lower() in {"", "nan", "nat", "none"}:
        raise ValueError(f"Tushare ETF adjustment record has no {field_name}")
    return normalized


def _parse_date(value: object) -> date:
    try:
        return date.fromisoformat(_required_text(value, "trade_date"))
    except ValueError as exc:
        raise ValueError(
            "Tushare ETF adjustment record has an invalid trade_date"
        ) from exc


def _parse_positive_decimal(value: object, field_name: str) -> Decimal:
    try:
        parsed = Decimal(_required_text(value, field_name))
    except InvalidOperation as exc:
        raise ValueError(
            f"Tushare ETF adjustment record has an invalid {field_name}"
        ) from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(
            f"Tushare ETF adjustment record has an invalid {field_name}"
        )
    return parsed


def _reject_duplicate_factor_keys(
    factors: list[EtfAdjustmentFactorInput],
) -> None:
    keys = [(factor.ts_code, factor.trade_date) for factor in factors]
    if len(keys) != len(set(keys)):
        raise ValueError(
            "Tushare ETF adjustment response contains duplicate ts_code and trade_date"
        )


def _checkpoint_synced_through_date(checkpoint: DataSyncCheckpointState) -> date:
    value = checkpoint.cursor.get("synced_through_date")
    if not isinstance(value, str):
        raise ValueError("ETF adjustment checkpoint has no synced_through_date")
    return date.fromisoformat(value)


def _full_checkpoint_target_date(checkpoint: DataSyncCheckpointState) -> date:
    value = checkpoint.cursor.get("target_through_date")
    if not isinstance(value, str):
        raise ValueError("ETF adjustment full checkpoint has no target_through_date")
    return date.fromisoformat(value)


def _full_cycle_is_complete(checkpoint: DataSyncCheckpointState) -> bool:
    return _checkpoint_synced_through_date(checkpoint) >= _full_checkpoint_target_date(
        checkpoint
    )
