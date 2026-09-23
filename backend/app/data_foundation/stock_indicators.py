"""Preserve provider indicator identifiers and grouped report windows exactly."""
from app.data_foundation.market_activity import invalid, number, text
from app.data_foundation.distributions import reported_day


def indicators_body(source, raw):
    rows = raw.get('item')
    if not isinstance(rows, list):
        invalid('股票指标缺少完整报告列表。')
    reports = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or row.get('thscode') != source.subject:
            invalid('股票指标报告主体与固定来源不一致。')
        label = text(row.get('report'), required=True)
        groups = row.get('abilities')
        if not isinstance(groups, list):
            invalid('股票指标缺少明确能力分组列表。')
        abilities = []
        for group in groups:
            if not isinstance(group, dict):
                invalid('股票指标能力分组无效。')
            ability = text(group.get('ability'), required=True)
            values = group.get('indicators')
            if not isinstance(values, list):
                invalid('股票指标分组缺少明确指标列表。')
            indicators = []
            for value in values:
                if not isinstance(value, dict) or 'value' not in value:
                    invalid('股票指标缺少显式数值字段，未将缺项视为空值。')
                indicators.append(dict(provider_indicator=text(value.get('index_id'), required=True),
                                       reported_value=number(value['value'], signed=True)))
            keys = [value['provider_indicator'] for value in indicators]
            if len(set(keys)) != len(keys):
                invalid('股票指标同组包含重复代码，整组保留待修复。')
            abilities.append(dict(provider_ability=ability, indicators=indicators))
        names = [group['provider_ability'] for group in abilities]
        if len(set(names)) != len(names):
            invalid('股票指标报告包含重复能力分组。')
        reports.append(dict(source_order=index, reported_period=label, abilities=abilities))
    labels = [row['reported_period'] for row in reports]
    if len(set(labels)) != len(labels):
        invalid('股票指标窗口包含重复报告期间。')
    revision = raw.get('historical_revision_evidence')
    if revision is not None and type(revision) is not bool:
        invalid('股票指标历史修订证据标志无效。')
    body = dict(source_code=text(source.subject, required=True), asset_type='a-share', reports=reports,
                requested_start=reported_day(raw.get('requested_start')), requested_end=reported_day(raw.get('requested_end')),
                reported_historical_revision_evidence=revision, coverage_basis='source_observation_window',
                calculation_formula=None, comparable_units=None, resolved_indicator_taxonomy=None,
                effective_period_boundaries=None, as_filed_history=None)
    quality = dict(calculation_formula='PROVIDER_FORMULA_UNVERIFIED', comparable_units='PROVIDER_UNITS_UNVERIFIED',
                   resolved_indicator_taxonomy='PROVIDER_IDENTIFIERS_ONLY', effective_period_boundaries='PROVIDER_PERIOD_LABELS_ONLY',
                   as_filed_history='HISTORICAL_VINTAGES_UNVERIFIED')
    return body, quality
