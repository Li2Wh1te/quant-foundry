"""Explicit financial-statement fields preserve reported periods and revisions.

These are complete source observation windows, not reconstructed as-filed
statements. Never subtract successive quarters, reconcile accounting equations,
normalize currencies, or interpret a provider report date as public availability.
"""
from app.data_foundation.market_activity import invalid, number, text
from app.data_foundation.distributions import reported_day
from app.data_foundation.fund_ownership import counter

DOMAINS = {
    'fund_financial_indicators': 'fund.financial_indicator_window',
    'fund_income': 'fund.income_window', 'fund_balance': 'fund.balance_window',
    'stock_income': 'market.income_window', 'stock_balance': 'market.balance_window',
    'stock_cash_flow': 'market.cash_flow_window',
}
NUMBERS = {
    'fund_financial_indicators': ('asset_nav', 'average_nav_profit_margin', 'average_share_current_profit',
        'current_income', 'current_profit', 'distribution_profit', 'distribution_share_profit', 'nav_rate',
        'share_nav', 'sum_nav_rate', 'sum_share_nav'),
    'fund_income': ('bond_investment_income', 'custodian_fee', 'dividend_income', 'exchange_income',
        'fair_value_income', 'fee', 'fund_investment_income', 'income', 'interest_income', 'investment_income',
        'manager_reward', 'net_profit', 'other_income', 'stock_investment_income', 'tax_surcharge',
        'total_fee', 'total_income', 'total_profit', 'transaction_cost'),
    'fund_balance': ('bank_deposit', 'bond_investment', 'fund_investment', 'liability_and_owner_equity',
        'other_assets', 'other_liability', 'owner_total_equity', 'stock_investment', 'total_assets',
        'total_liability', 'transactional_financial_assets', 'undistributed_profit'),
    'stock_income': ('basic_eps', 'income_tax_expense', 'interest_expenses', 'manage_fee', 'net_profit',
        'operating_costs', 'operating_expenses', 'operating_income', 'operating_profit', 'parent_holder_net_profit',
        'profit_total', 'research_and_development_expenses', 'sales_fee'),
    'stock_balance': ('accounts_receivable', 'assets_total', 'cash', 'holder_equity_total',
        'non_current_nets_total', 'total_current_assets', 'total_debt'),
    'stock_cash_flow': ('act_cash_flow_net', 'cash_equivalents_net_addition', 'financing_cash_flow_net',
        'invest_cash_flow_net', 'pay_dividends_profits_interest_cash', 'pay_fixed_assets_etc_cash'),
}


def financial_body(source, raw):
    rows = raw.get('item')
    if not isinstance(rows, list):
        invalid('财务观察缺少明确完整报表列表。')
    fund = source.dataset.startswith('fund_')
    dates = ('start_date', 'end_date', 'publish_date') if fund else ('period_end', 'report_date')
    texts = () if fund else ('currency', 'fiscal_period', 'period', 'ticker')
    fields = (*NUMBERS[source.dataset], *[name+'_ms' for name in dates], *texts,
              *(() if fund else ('fiscal_year', 'thscode')))
    reports = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) - set(fields):
            invalid('财务报表包含未声明字段或无效成员，整组保留待处理。')
        if row.get('thscode') not in (None, source.subject):
            invalid('财务报表主体与固定来源不一致。')
        report = dict(source_order=index, reported_fields=[name for name in fields if name in row])
        for name in NUMBERS[source.dataset]:
            report['reported_'+name] = number(row.get(name), signed=True)
        for name in dates:
            report['reported_'+name] = reported_day(row.get(name+'_ms'))
        for name in texts:
            report['reported_'+name] = text(row.get(name))
        if not fund:
            report['reported_fiscal_year'] = counter(row.get('fiscal_year'))
            if raw.get('period') is not None and row.get('period') not in (None, raw['period']):
                invalid('财务报表来源期间与采集容器不一致。')
        reports.append(report)
    revision = raw.get('historical_revision_evidence')
    if revision is not None and type(revision) is not bool:
        invalid('财务历史修订证据标志无效。')
    body = dict(source_code=text(source.subject, required=True), asset_type='fund' if fund else 'a-share',
                reports=reports, reported_timestamp_ms=counter(raw.get('timestamp')),
                reported_period=text(raw.get('period')), requested_start=reported_day(raw.get('requested_start')),
                requested_end=reported_day(raw.get('requested_end')), reported_historical_revision_evidence=revision,
                coverage_basis='source_observation_window', comparable_units=None,
                accounting_basis=None, normalized_currency=None, single_period_values=None,
                as_filed_history=None, complete_history=None)
    quality = dict(comparable_units='PROVIDER_UNITS_UNVERIFIED', accounting_basis='ACCOUNTING_BASIS_UNVERIFIED',
                   normalized_currency='CURRENCY_NOT_NORMALIZED', single_period_values='NO_CUMULATIVE_PERIOD_INFERENCE',
                   as_filed_history='HISTORICAL_VINTAGES_UNVERIFIED', complete_history='SOURCE_WINDOW_ONLY')
    return body, quality
