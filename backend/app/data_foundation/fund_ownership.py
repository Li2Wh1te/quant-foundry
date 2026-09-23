"""Source-reported fund composition and ownership, without inferred identities.

Rows lacking a report date remain distinct ordered occurrences. A holder code
can describe a category shared by different names; neither code nor name is a
resolved person/entity identity. Ratios are retained, never renormalized.
"""
from app.data_foundation.market_activity import invalid, number, text
from app.data_foundation.distributions import reported_day

DOMAINS = {
    'fund_allocation': 'fund.allocation_window',
    'fund_industry': 'fund.industry_window',
    'fund_holders': 'fund.holder_composition_window',
    'fund_top_holders': 'fund.top_holder_window',
}
NUMBERS = {
    'fund_allocation': ('bond_ratio_pct', 'deposit_ratio_pct', 'other_ratio_pct', 'stock_ratio_pct'),
    'fund_industry': ('ratio_pct',),
    'fund_holders': ('avg_holder_share', 'ins_position', 'mgmt_staff_hold_rate', 'psnl_rate'),
    'fund_top_holders': ('hold_rate_pct', 'hold_share'),
}
TEXTS = {
    'fund_allocation': (), 'fund_industry': ('industry_name', 'report_period'),
    'fund_holders': ('merge_scope',),
    'fund_top_holders': ('holder_code', 'holder_id', 'holder_name', 'holder_type'),
}
DATES = {
    'fund_allocation': ('report_date',), 'fund_industry': (),
    'fund_holders': ('report_date',), 'fund_top_holders': ('publish_date', 'report_date'),
}
COUNTERS = {'fund_allocation': (), 'fund_industry': (), 'fund_holders': ('holder_amount',), 'fund_top_holders': ('rank',)}


def counter(value):
    if value is not None and (type(value) is not int or value < 0):
        invalid('持有人或配置报告计数无效，未取整或补零。')
    return value


def ownership_body(source, raw):
    native = source.dataset
    rows = raw.get('item')
    if not isinstance(rows, list):
        invalid('基金配置或持有人观察缺少明确完整列表。')
    if raw.get('thscode') not in (None, source.subject):
        invalid('基金配置或持有人主体与固定来源不一致。')
    members = []
    fields = (*NUMBERS[native], *TEXTS[native], *[d+'_ms' for d in DATES[native]], *COUNTERS[native])
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) - set(fields):
            invalid('基金配置或持有人观察成员无效。')
        member = dict(source_order=index, reported_fields=[name for name in fields if name in row])
        for name in NUMBERS[native]:
            member['reported_'+name] = number(row.get(name), signed=True)
        for name in TEXTS[native]:
            member['reported_'+name] = text(row.get(name))
        for name in DATES[native]:
            member['reported_'+name] = reported_day(row.get(name+'_ms'))
        for name in COUNTERS[native]:
            member['reported_'+name] = counter(row.get(name))
        members.append(member)
    stamp = counter(raw.get('timestamp'))
    body = dict(source_code=text(source.subject, required=True), asset_type='fund', members=members,
                reported_timestamp_ms=stamp, reported_limit=counter(raw.get('limit')),
                coverage_basis='source_observation_window', comparable_units=None,
                resolved_holder_identity=None, normalized_allocation=None,
                industry_taxonomy=None, complete_history=None)
    quality = dict(comparable_units='PROVIDER_UNITS_UNVERIFIED',
                   resolved_holder_identity='HOLDER_CODES_AND_NAMES_NOT_RESOLVED',
                   normalized_allocation='REPORTED_RATIOS_NOT_RENORMALIZED',
                   industry_taxonomy='PROVIDER_LABELS_ONLY', complete_history='SOURCE_WINDOW_ONLY')
    return body, quality
