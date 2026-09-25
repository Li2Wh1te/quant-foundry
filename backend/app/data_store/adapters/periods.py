"""Exact provider report-period parser migrated without publication dependencies."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from .canonical import NativeInputError
REPORT_TYPES = {'quarter': '季度', 'annual': '年度', 'semiannual': '半年度'}
SHANGHAI = ZoneInfo('Asia/Shanghai')
def business_date(value: object) -> date:
    if not isinstance(value, int) or isinstance(value, bool):
        raise NativeInputError('REPORT_PERIOD_INVALID', '报告期必须是整数毫秒时间。')
    try:
        dt = (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=value)).astimezone(SHANGHAI)
    except (OverflowError, ValueError):
        raise NativeInputError('REPORT_PERIOD_INVALID', '报告期超出支持日期范围。') from None
    if (dt.hour, dt.minute, dt.second, dt.microsecond) != (0, 0, 0, 0):
        raise NativeInputError('REPORT_PERIOD_INVALID', '报告期不是上海时区的日期边界。')
    return dt.date()

def report_key(report: dict) -> tuple[str, date, date, str]:
    start = business_date(report.get('start_date_ms'))
    end = business_date(report.get('end_date_ms'))
    kind = report.get('report_type')
    if start > end or not isinstance(kind, str) or kind not in REPORT_TYPES:
        raise NativeInputError('REPORT_PERIOD_INVALID', '报告起止日期或报告类型不受支持。')
    return (f'{end}:{kind}', start, end, kind)
