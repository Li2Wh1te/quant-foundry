"""Typed, atomic distribution windows retain reported events without netting.

A provider row is an occurrence inside one immutable observation, not a proven
corporate-action identity. Preserve order and duplicates, including zero and
negative adjustments. Publication dates remain reported dates, never public
point-in-time availability or evidence that a payment was executed.
"""
from app.data_foundation.market_activity import invalid, number, text

FUND_DATES = ('ex_dividend_date', 'in_dividend_date', 'payment_date', 'profit_base_date',
              'publish_date', 'registration_date', 'reinvestment_date')
FUND_NUMBERS = ('per_ten_cash_after_tax', 'per_ten_cash_before_tax')
STOCK_NUMBERS = ('dividend_per_share', 'per_share_bonus', 'allotment_ratio', 'allotment_price')


def reported_day(value):
    from app.data_foundation.record_adapters import source_date
    if value is None:
        return None
    try:
        return source_date(value)
    except (ValueError, OverflowError, OSError):
        invalid('分配事件报告日期无效，整组保留待修复。')


def distribution_body(source, raw):
    rows = raw.get('item')
    if not isinstance(rows, list):
        invalid('分配事件缺少明确完整列表，未将缺项视为空。')
    code = text(source.subject, required=True)
    if raw.get('thscode') not in (None, code):
        invalid('分配事件容器主体与固定来源不一致。')
    fund = source.dataset == 'fund_dividends'
    events = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            invalid('分配事件成员不是字段对象。')
        event = dict(source_order=index)
        if fund:
            fields = (*[name+'_ms' for name in FUND_DATES], *FUND_NUMBERS, 'progress')
            for name in FUND_DATES:
                event['reported_'+name] = reported_day(row.get(name+'_ms'))
            for name in FUND_NUMBERS:
                event['reported_'+name] = number(row.get(name), signed=True)
            event['reported_progress_code'] = text(row.get('progress'))
        else:
            fields = ('ex_date_ms', *STOCK_NUMBERS, 'ticker', 'thscode', 'currency')
            if row.get('thscode') not in (None, code):
                invalid('除权除息成员主体与固定来源不一致。')
            if raw.get('ticker') is not None and row.get('ticker') not in (None, raw['ticker']):
                invalid('除权除息成员证券简称代码与容器不一致。')
            event['reported_ex_date'] = reported_day(row.get('ex_date_ms'))
            for name in STOCK_NUMBERS:
                event['reported_'+name] = number(row.get(name), signed=True)
            for name in ('ticker', 'currency'):
                event['reported_'+name] = text(row.get(name))
        if set(row) - set(fields):
            invalid('分配事件包含未声明字段，整组保留待处理。')
        event['reported_fields'] = [name for name in fields if name in row]
        events.append(event)
    body = dict(source_code=code, asset_type='fund' if fund else 'a-share', events=events,
                coverage_basis='source_observation_window', payment_execution=None,
                normalized_cashflow=None, event_identity=None, complete_history=None)
    quality = dict(payment_execution='REPORTED_EVENT_NOT_EXECUTION',
                   normalized_cashflow='PROVIDER_UNITS_AND_TAX_BASIS_UNVERIFIED',
                   event_identity='SOURCE_OCCURRENCES_NOT_RESOLVED', complete_history='SOURCE_WINDOW_ONLY')
    if fund:
        count = raw.get('dividend_count')
        if count is not None and (type(count) is not int or count < 0):
            invalid('分红来源报告计数无效。')
        stamp = raw.get('timestamp')
        if stamp is not None and (type(stamp) is not int or stamp < 0):
            invalid('分红来源报告时间戳无效。')
        body.update(reported_dividend_count=count, reported_dividend_total=number(raw.get('dividend_total'), signed=True),
                    reported_timestamp_ms=stamp, currency=None, tax_basis=None)
        quality.update(currency='CURRENCY_UNVERIFIED', tax_basis='TAX_BASIS_UNVERIFIED')
    else:
        body.update(reported_ticker=text(raw.get('ticker')), reported_adjustment=text(raw.get('adjust')),
                    requested_start=reported_day(raw.get('requested_start')), requested_end=reported_day(raw.get('requested_end')),
                    reported_coverage=text(raw.get('coverage')))
    return body, quality
