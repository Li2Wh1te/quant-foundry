"""Typed source-local portfolio observations with explicit unresolved identities.

Reuse the established holdings report-period parser. These observation windows
are distinct from fund.holdings_report: they do not install reviewed historical
security bindings or claim a complete portfolio. Repeated bond codes remain
separate source occurrences, not consolidated positions. The existing reviewed
holdings contract and its identity/receipt gates remain authoritative for that
stronger semantic series.
"""
from app.data_foundation.market_activity import invalid, number, text
from app.data_foundation.distributions import reported_day
from app.data_foundation.fund_ownership import counter
from app.data_foundation.holdings import report_key, REPORT_TYPES

DOMAINS = {'fund_holdings': 'fund.reported_holdings_window',
           'fund_stock_history': 'fund.reported_stock_holdings_window',
           'fund_bond_history': 'fund.reported_bond_holdings_window'}
CURRENT_NUMBERS = ('hold_ratio', 'period_increase_rate_pct', 'position_capital', 'position_count',
                   'security_market_value_rate_pct')
CURRENT_DATES = ('start_date', 'end_date', 'modify_time', 'publish_date')
HISTORY_NUMBERS = ('hold_ratio', 'market_value', 'period_increase_pct')


def descriptor(raw):
    if not isinstance(raw, dict):
        invalid('持仓报告期间不是明确对象。')
    key, start, end, kind = report_key(raw)
    label = text(raw.get('report_type_name'))
    if label is not None and label != REPORT_TYPES[kind]:
        invalid('持仓报告期间标签与来源类型不一致。')
    return dict(report_key=key, period_start=start, period_end=end, provider_report_type=kind,
                reported_type_name=label)


def directory(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get('item'), list):
        invalid('持仓历史缺少明确报告目录。')
    items = [descriptor(item) for item in raw['item']]
    keys = [item['report_key'] for item in items]
    if len(keys) != len(set(keys)):
        invalid('持仓历史报告目录包含重复期间。')
    return items


def member(raw, index, *, historical, kind=None, end=None):
    if not isinstance(raw, dict):
        invalid('持仓成员不是字段对象。')
    numbers = HISTORY_NUMBERS if historical else CURRENT_NUMBERS
    dates = ('end_date',) if historical else CURRENT_DATES
    texts = ('asset_type', 'name', 'report_type', 'ticker') if historical else ('asset_type', 'stock_name', 'ticker')
    rank = 'rank' if historical else 'investment_rank'
    fields = ('thscode', *numbers, *[name+'_ms' for name in dates], *texts, rank)
    if set(raw) - set(fields):
        invalid('持仓成员包含未声明字段，整组保留待处理。')
    result = dict(source_order=index, source_member_code=text(raw.get('thscode'), required=historical),
                  reported_fields=[name for name in fields if name in raw])
    for name in numbers:
        result['reported_'+name] = number(raw.get(name), signed=True)
    for name in dates:
        result['reported_'+name] = reported_day(raw.get(name+'_ms'))
    for name in texts:
        result['reported_'+name] = text(raw.get(name))
    result['reported_'+rank] = counter(raw.get(rank))
    if historical:
        if result['reported_end_date'] != end or result['reported_report_type'] != REPORT_TYPES[kind]:
            invalid('持仓成员期间与所属报告不一致。')
    return result


def portfolio_body(source, raw):
    rows = raw.get('item')
    if not isinstance(rows, list):
        invalid('持仓观察缺少明确完整列表。')
    if raw.get('thscode') not in (None, source.subject):
        invalid('持仓容器主体与固定来源不一致。')
    body = dict(source_code=text(source.subject, required=True), asset_type='fund',
                coverage_basis='source_observation_window', resolved_instrument_identities=None,
                portfolio_complete=None, transport_complete=None, comparable_units=None,
                normalized_weights=None, as_filed_history=None)
    quality = dict(resolved_instrument_identities='HISTORICAL_IDENTITY_BINDINGS_UNRESOLVED',
                   portfolio_complete='PROVIDER_REPORTED_SCOPE_ONLY', transport_complete='TRANSPORT_RECEIPTS_NOT_RECONCILED',
                   comparable_units='PROVIDER_UNITS_UNVERIFIED', normalized_weights='REPORTED_RATIOS_NOT_RENORMALIZED',
                   as_filed_history='HISTORICAL_VINTAGES_UNVERIFIED')
    if source.dataset == 'fund_holdings':
        # Fund-of-funds members can lack provider security codes. Retain the
        # reported name and occurrence without fabricating a binding or code.
        body['members'] = [member(row, index, historical=False) for index, row in enumerate(rows)]
        for name in ('concentration_ratio', 'stock_ratio_pct', 'total_stock_ratio_pct', 'total_bond_ratio_pct', 'total_fund_ratio_pct'):
            body['reported_'+name] = number(raw.get(name), signed=True)
        body['reported_main_industry'] = text(raw.get('main_industry'))
        body['reported_timestamp_ms'] = counter(raw.get('timestamp'))
        return body, quality
    declared = directory(raw.get('report_directory'))
    current = directory(raw['current_provider_directory']) if 'current_provider_directory' in raw else None
    expected = {item['report_key']: item for item in declared}
    reports = []
    for index, wrapper in enumerate(rows):
        if not isinstance(wrapper, dict):
            invalid('持仓历史报告容器无效。')
        period = descriptor(wrapper.get('report'))
        key = wrapper.get('report_key')
        if key != period['report_key'] or expected.get(key) != period:
            invalid('持仓历史报告与固定目录不一致。')
        data = wrapper.get('data')
        if not isinstance(data, dict) or not isinstance(data.get('item'), list):
            invalid('持仓历史报告缺少明确成员列表。')
        if data.get('has_more') or data.get('next_cursor') or data.get('next_page'):
            invalid('持仓历史报告仍有未读取分页，不能作为完整观察发布。')
        members = [member(item, ordinal, historical=True, kind=period['provider_report_type'],
                          end=period['period_end']) for ordinal, item in enumerate(data['item'])]
        asset = 'stock' if source.dataset == 'fund_stock_history' else 'bond'
        if any(item['reported_asset_type'] != asset for item in members):
            invalid('持仓历史成员资产类别与报告来源不一致。')
        reports.append(dict(source_order=index, **period, reported_timestamp_ms=counter(data.get('timestamp')), members=members))
    keys = [item['report_key'] for item in reports]
    if len(set(keys)) != len(keys) or set(keys) != set(expected):
        invalid('持仓历史报告缺失或重复，未把目录缺项视为空报告。')
    failures = raw.get('failed_requests', [])
    if not isinstance(failures, list) or failures:
        invalid('持仓历史含未完成来源请求，旧成员不能冒充完整新观察。')
    revision = raw.get('historical_revision_evidence')
    if revision is not None and type(revision) is not bool:
        invalid('持仓历史修订证据标志无效。')
    body.update(reports=reports, report_directory=declared, current_provider_directory=current,
                reported_historical_revision_evidence=revision)
    return body, quality
