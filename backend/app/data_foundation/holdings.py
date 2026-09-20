"""Evidence-bound, whole-object normalization for local holdings reports.

This module has no database or provider client. Callers must supply a fixed
source object and identity bindings whose evidence has already been reviewed.
A catalogue suggestion must never be passed as a resolved binding. Invalid
members quarantine their entire report; optional field failures preserve the
core ratio. Original values remain in the separately retained SourceRef.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from typing import Callable
from uuid import UUID
from zoneinfo import ZoneInfo

from app.data_foundation.canonical import FoundationError, digest

DATASET = 'fund.holdings_report'
SERIES = 'cn-fund-stock-holdings-provider-reported'
VERSION = '1.0'
REPORT_TYPES = {'quarter': '季度', 'annual': '年度', 'semiannual': '半年度'}
SHANGHAI = ZoneInfo('Asia/Shanghai')


@dataclass(frozen=True)
class ResolvedIdentity:
    """A reviewed binding covers a half-open business interval.

    ``known_at`` remains a current observation timestamp even when the business
    interval is historical. It is not evidence of historical public availability.
    """
    instrument_id: UUID
    binding_id: UUID
    valid_from: date
    valid_to: date
    known_at: datetime

    def covers(self, start: date, end: date) -> bool:
        return (self.valid_from <= start <= end < self.valid_to
                and self.known_at.tzinfo is not None)


IdentityResolver = Callable[[str, str, date, date], ResolvedIdentity | None]


def business_date(value: object) -> date:
    # Reject floats/bools and sub-day timestamps rather than silently shifting a
    # report into another business day. Integer milliseconds convert exactly.
    if not isinstance(value, int) or isinstance(value, bool):
        raise FoundationError('REPORT_PERIOD_INVALID', '报告期必须是整数毫秒时间。')
    try:
        dt = (datetime(1970, 1, 1, tzinfo=timezone.utc)
              + timedelta(milliseconds=value)).astimezone(SHANGHAI)
    except (OverflowError, ValueError):
        raise FoundationError('REPORT_PERIOD_INVALID', '报告期超出支持日期范围。') from None
    if (dt.hour, dt.minute, dt.second, dt.microsecond) != (0, 0, 0, 0):
        raise FoundationError('REPORT_PERIOD_INVALID', '报告期不是上海时区的日期边界。')
    return dt.date()


def ratio(value: object, *, core: bool) -> Decimal:
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int, Decimal)):
        raise FoundationError('REPORT_VALUE_INVALID', '持仓比例不是精确数值。')
    try:
        raw = Decimal(value)
    except InvalidOperation:
        raise FoundationError('REPORT_VALUE_INVALID', '持仓比例不是有效数值。') from None
    if not raw.is_finite() or len(raw.as_tuple().digits) > 100 or abs(raw.as_tuple().exponent) > 100:
        raise FoundationError('REPORT_VALUE_INVALID', '持仓比例不是有限且可存储的数值。')
    # Supply sufficient precision before division; never round under the global
    # Decimal context (which a caller may have changed).
    with localcontext() as context:
        context.prec = 120
        result = raw / Decimal(100)
        scaled = result * Decimal(10 ** 10)
        if scaled != scaled.to_integral_value() or abs(result) >= Decimal(10 ** 10):
            raise FoundationError('REPORT_VALUE_PRECISION', '持仓比例超出契约精度，未进行舍入。')
    if core and not Decimal(0) <= result <= Decimal(1):
        raise FoundationError('REPORT_VALUE_INVALID', '持仓比例必须在0至1之间。')
    return result


def report_key(report: dict) -> tuple[str, date, date, str]:
    start = business_date(report.get('start_date_ms'))
    end = business_date(report.get('end_date_ms'))
    kind = report.get('report_type')
    if start > end or not isinstance(kind, str) or kind not in REPORT_TYPES:
        raise FoundationError('REPORT_PERIOD_INVALID', '报告起止日期或报告类型不受支持。')
    return f'{end}:{kind}', start, end, kind


def target_key(fund_id: UUID, start: date, end: date, kind: str,
               scope: str = 'provider_reported', series: str = SERIES) -> str:
    # A hash fits the shared 128-character unit key without dropping dimensions.
    return digest('holdings-business-key-v1', [fund_id, start, end, kind, scope, series])


def normalize_report(*, subject: str, selected_key: str, container: dict,
                     resolve_identity: IdentityResolver, receipt_requests: list | None = None) -> dict:
    """Evaluate exactly one selected object, without repairing source content.

    ``container`` is the materialized immutable collection, not the mutable
    collection state. Successful object wrappers and matching directory entries
    are required. A failed request for this report invalidates its receipt even
    if the source retained a last-good wrapper; failures for other reports do not.
    """
    reasons: list[str] = []
    result = {'source_subject': subject, 'source_report_key': selected_key,
              'readiness': 'quarantined', 'reasons': reasons, 'header': None,
              'members': [], 'transport_complete': None, 'portfolio_complete': None,
              'time_evidence': 'observed_only', 'public_at': None}
    def reject(reason: str) -> dict:
        reasons.append(reason)
        return result
    if not isinstance(container, dict) or not isinstance(container.get('item'), list):
        return reject('REPORT_CONTAINER_INVALID')
    wrappers = [r for r in container['item'] if isinstance(r, dict) and r.get('report_key') == selected_key]
    if len(wrappers) != 1:
        return reject('REPORT_MISSING' if not wrappers else 'REPORT_DUPLICATE')
    wrapper = wrappers[0]
    try:
        descriptor = wrapper.get('report')
        if not isinstance(descriptor, dict):
            return reject('REPORT_PERIOD_INVALID')
        key, start, end, kind = report_key(descriptor)
        if key != selected_key:
            return reject('REPORT_KEY_MISMATCH')
    except FoundationError as exc:
        return reject(exc.code)
    directory = container.get('report_directory')
    if not isinstance(directory, dict) or not isinstance(directory.get('item'), list):
        return reject('REPORT_DIRECTORY_MISSING')
    matches = []
    for entry in directory['item']:
        if not isinstance(entry, dict):
            return reject('REPORT_DIRECTORY_INVALID')
        # Invalid unrelated directory rows cannot be silently excluded from the
        # completeness evidence for the collection.
        try:
            entry_key, entry_start, entry_end, entry_kind = report_key(entry)
        except FoundationError:
            return reject('REPORT_DIRECTORY_INVALID')
        if entry_key == key:
            matches.append((entry_start, entry_end, entry_kind))
    if matches != [(start, end, kind)]:
        return reject('REPORT_DIRECTORY_MISMATCH')
    if not isinstance(receipt_requests, list) or not any(
        isinstance(r, dict) and r.get('interface') == 'fund.portfolio.stock-history'
        and isinstance(r.get('request_id'), str) and r['request_id']
        and r.get('parameters') == {'thscode': subject, 'end_date': str(end), 'report_type': kind}
        for r in receipt_requests):
        return reject('REPORT_RECEIPT_UNKNOWN')
    failures = container.get('failed_requests', [])
    if not isinstance(failures, list):
        return reject('REPORT_RECEIPT_UNKNOWN')
    for failure in failures:
        params = failure.get('parameters') if isinstance(failure, dict) else None
        if not isinstance(params, dict) or not {'end_date', 'report_type'} <= params.keys():
            return reject('REPORT_RECEIPT_UNKNOWN')
        if (params['end_date'], params['report_type']) == (str(end), kind):
            result['transport_complete'] = False
            return reject('REPORT_REQUEST_FAILED')
    data = wrapper.get('data')
    if not isinstance(data, dict) or not isinstance(data.get('item'), list):
        return reject('REPORT_RECEIPT_UNKNOWN')
    # No endpoint pagination is currently part of this source contract. If a
    # future response introduces explicit pagination, do not silently admit it.
    if data.get('has_more') or data.get('next_cursor') or data.get('next_page'):
        result['transport_complete'] = False
        return reject('REPORT_PAGES_MISSING')
    result['transport_complete'] = True
    fund = resolve_identity(subject, 'fund_share', start, end)
    if fund is None or not fund.covers(start, end):
        reasons.append('FUND_IDENTITY_UNRESOLVED')
    result['header'] = {'fund_share_id': fund.instrument_id if fund else None,
                        'binding_id': fund.binding_id if fund else None,
                        'period_start': start, 'period_end': end, 'report_type': kind,
                        'scope_kind': 'provider_reported', 'series': SERIES,
                        'member_count': len(data['item']), 'public_at': None}
    for ordinal, raw in enumerate(data['item']):
        if not isinstance(raw, dict):
            reasons.append('REPORT_MEMBER_INVALID')
            continue
        code = raw.get('thscode')
        if (raw.get('asset_type') != 'stock' or not isinstance(code, str)
                or not code.strip() or len(code) > 64):
            reasons.append('REPORT_MEMBER_INVALID')
            continue
        try:
            if business_date(raw.get('end_date_ms')) != end or raw.get('report_type') != REPORT_TYPES[kind]:
                reasons.append('REPORT_MEMBER_PERIOD_MISMATCH')
                continue
            weight = ratio(raw.get('hold_ratio'), core=True)
        except FoundationError as exc:
            reasons.append(exc.code)
            continue
        identity = resolve_identity(code, 'stock', end, end)
        if identity is None or not identity.covers(end, end):
            reasons.append('MEMBER_IDENTITY_UNRESOLVED')
        quality = {'market_value': 'UNIT_UNVERIFIED'}
        change = None
        if raw.get('period_increase_pct') is None:
            quality['period_change_ratio'] = 'SOURCE_NULL'
        else:
            try:
                change = ratio(raw['period_increase_pct'], core=False)
            except FoundationError as exc:
                quality['period_change_ratio'] = exc.code
        rank = raw.get('rank')
        if rank is not None and (isinstance(rank, bool) or not isinstance(rank, int) or not 0 < rank <= 2147483647):
            quality['rank'] = 'REPORT_RANK_INVALID'
            rank = None
        if rank is None and 'rank' not in quality:
            quality['rank'] = 'SOURCE_NULL'
        result['members'].append({'member_ordinal': ordinal,
            'member_instrument_id': identity.instrument_id if identity else None,
            'binding_id': identity.binding_id if identity else None,
            'source_member_code': code, 'source_namespace': 'provider_report_member',
            'hold_ratio': weight, 'market_value': None, 'period_change_ratio': change,
            'rank': rank, 'field_quality': quality})
    result['reasons'] = sorted(set(reasons))
    result['readiness'] = 'ready' if not reasons else 'quarantined'
    result['values_hash'] = digest('holdings-normalized-v1', result)
    return result
