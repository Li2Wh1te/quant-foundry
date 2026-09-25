"""The sole code-owned source/domain/representation registry for D02 and D04.

Baseline entry count is NOT a business-dataset count. Support evidence stays
native; import progress routes to real price/action inputs. No vendor client is
constructed here. No SourceRef, Execution, Definition or old publishing import.
"""
from dataclasses import dataclass, replace
from functools import cached_property
from typing import get_args
from copy import deepcopy
from pydantic import create_model

from . import record_schemas as shapes
from .record_adapters import SOURCE_DATASETS
from .layout import Layout, partition_for, tick_partition
from .facts import CorporateAction, TradingStatus, Tick, IntradayBar

# Projection rules name business dates explicitly. They are not inferred from
# ingestion IDs, task times, source array position or arbitrary column names.
SERIES = {
    'stock_daily': ('points', shapes.ReportedPricePoint, ('trade_date',)),
    'etf_daily': ('points', shapes.ReportedPricePoint, ('trade_date',)),
    'index_daily': ('points', shapes.ReportedPricePoint, ('trade_date',)),
    'fund_nav': ('points', shapes.NavPoint, ('nav_date',)),
    'rank_trend': ('points', shapes.RankTrendPoint, ('trading_date',)),
    'fund_manager_performance': ('points', shapes.ManagerPerformancePoint, ('trading_date',)),
    'fund_performance_history': ('points', shapes.PerformancePoint, ('trading_date',)),
    'fund_news': ('articles', shapes.FundNewsArticle, ('provider_article_id',)),
}
REPORTS = {
    'fund_stock_history': ('reports', shapes.HistoricalPortfolioReport, ('report_key',)),
    'fund_bond_history': ('reports', shapes.HistoricalPortfolioReport, ('report_key',)),
    'stock_indicators': ('reports', shapes.StockIndicatorReport, ('reported_period',)),
    'fund_financial_indicators': ('reports', shapes.FundFinancialIndicatorReport, ('reported_end_date','reported_start_date')),
    'fund_income': ('reports', shapes.FundIncomeReport, ('reported_end_date','reported_start_date')),
    'fund_balance': ('reports', shapes.FundBalanceReport, ('reported_end_date','reported_start_date')),
    'stock_income': ('reports', shapes.StockIncomeReport, ('reported_period_end','reported_period')),
    'stock_balance': ('reports', shapes.StockBalanceReport, ('reported_period_end','reported_period')),
    'stock_cash_flow': ('reports', shapes.StockCashFlowReport, ('reported_period_end','reported_period')),
    'limit_ladder': ('days', shapes.LadderDay, ('trading_date',)),
}
GROUPS = {
    'fund_holdings': ('members', shapes.CurrentPortfolioMember, ('reported_end_date',)),
    'fund_allocation': ('members', shapes.FundAllocationMember, ('reported_report_date',)),
    'fund_industry': ('members', shapes.FundIndustryMember, ('reported_report_period',)),
    'fund_holders': ('members', shapes.FundHolderMember, ('reported_report_date',)),
    'fund_top_holders': ('members', shapes.FundTopHolderMember, ('reported_report_date',)),
    'fund_dividends': ('events', shapes.FundDividendEvent, ('reported_ex_dividend_date',)),
    'stock_actions': ('events', shapes.CorporateActionEvent, ('reported_ex_date',)),
}
SNAPSHOT_DATE = {
    'hot_history': 'ranking_date', 'dragon_tiger': 'trading_date',
    'auction_benchmark': 'trading_date', 'limit_up': 'trading_date',
    'limit_down': 'trading_date', 'limit_break': 'trading_date',
}
TABLE_KEYS = {'etf_directory': ('source_code',), 'exchange_calendar': ('calendar_date',),
              'etf_daily': ('trade_date',), 'etf_adjustment_factors': ('trade_date',),
              'corporate_action_facts': ('logical_fact_key',),
              'trading_status_facts': ('trade_date','dimension')}


@dataclass(frozen=True)
class Entry:
    id: str
    source: str
    native: str
    domain: str
    disposition: str
    target: str | None = None

    @property
    def business(self):
        return self.disposition == 'business'

    @cached_property
    def projection(self):
        if not self.business:
            return None
        if self.source == 'synthetic':
            return ('events', Tick if self.native=='tick' else IntradayBar, ())
        if self.source == 'tushare':
            model = {'corporate_action_facts':CorporateAction, 'trading_status_facts':TradingStatus}.get(self.native)
            return ('table', model or shapes.schema_for(self.domain).body, TABLE_KEYS[self.native])
        if self.native in SERIES: return ('series', *SERIES[self.native])
        if self.native in REPORTS: return ('reports', *REPORTS[self.native])
        if self.native in GROUPS:
            path,member,keys=GROUPS[self.native]
            root=shapes.schema_for(self.domain).body
            header={n:(f.annotation,deepcopy(f)) for n,f in root.model_fields.items() if n!=path}
            model=create_model(self.native.title().replace('_','')+'Period',
                               **header,members=(list[member], ...))
            return ('group',path,model,keys)
        if self.native == 'fund_manager_experience':
            return ('intervals','assignments',shapes.ManagerAssignment,('fund_code','start_text'))
        model=shapes.schema_for(self.domain).body
        if self.native in ('tickers','calendar','fund_company','fund_manager','fund_profile'):
            return ('reference',model,('calendar_date',) if self.native=='calendar' else ())
        return ('snapshot',model,(SNAPSHOT_DATE[self.native],) if self.native in SNAPSHOT_DATE else ())

    @cached_property
    def layout(self):
        p=self.projection
        if p is None: return None
        model=p[-2]
        return Layout(model)

    @cached_property
    def spec(self):
        if not self.business: return None
        # These representations intentionally DO NOT silently blend raw ETF
        # prices with unanchored forward prices or exchange calendars with lists.
        suffix = 'reported' if self.source=='tonghuashun' else 'local'
        name=self.domain.removesuffix('_window').removesuffix('_snapshot')+'.'+suffix
        if self.source=='synthetic': name='market.'+self.native+'.synthetic'
        if self.native=='corporate_action_facts': name='market.corporate_action.local'
        if self.native=='trading_status_facts': name='market.trading_status.local'
        if self.native=='etf_daily' and self.source=='tonghuashun': name='market.etf_daily.forward_unanchored'
        if self.native=='tickers': name='instrument.reference.provider_reported'
        semantics={'entry_id':self.id,'source':self.source,'native_dataset':self.native,'domain':self.domain,
                   'row_layout':'typed-object-nodes-v1','issue_scope':'object-key-v1','tombstones':'explicit-current-state','history':'business_dates_only',
                   'order':'comparable_source_per_object','merge':'whole_object' if self.projection[0] not in ('series','table','reference','intervals','events') else 'sparse_keys',
                   'unknown_units':'restricted_not_inferred','unbounded_decimal':'exact_text_no_sql_arithmetic',
                   'cross_source_fallback':'disabled'}
        spec=self.layout.spec(name, 'lfd02-v1', semantics,
                              report=self.projection[0] in ('reports','group','snapshot'))
        return replace(spec,partitioning='tick-session-sequence100k-v1',partitioner=tick_partition) if self.source=='synthetic' and self.native=='tick' else spec

    def describe_capability(self):
        return {'entry_id':self.id,'source':self.source,'native_dataset':self.native,'domain':self.domain,
                'classification':self.disposition,'dataset':self.spec.name if self.business else None,
                'reader':'local_sources.NativeSources.iter_entry / RescueSources.iter_entry','adapter':'adapters.normalize.normalize',
                'target_entry':self.target,'update_unit':self.projection[0] if self.business else self.disposition,
                'business_key':list(self.spec.key) if self.business else [],
                'schema_id':self.spec.schema_id if self.business else None,
                'rule':self.spec.rule if self.business else None,
                'limitations':list(shapes.schema_for(self.domain).limitations) if self.domain in shapes.SCHEMAS else
                    ['native_support_not_a_business_copy'] if not self.business else ['source_observation_not_public_availability'],
                'field_contracts':[{'column':f.name,'type':str(f.type),
                    'meaning':(f.metadata or {}).get(b'path',b'current_identity_or_confirmation').decode(),
                    'logical_type':(f.metadata or {}).get(b'logical_type',b'typed_scalar').decode(),
                    'arithmetic':(f.metadata or {}).get(b'arithmetic',b'type_only_units_require_domain_contract').decode()}
                    for f in self.spec.schema] if self.business else [],
                'supplier_network_required':False,'historical_release_supported':False}


# Ordered E01–E71 are copied from the task baseline, not discovered from DB data.
ENTRIES = (
    Entry('E01', 'tonghuashun', 'stock_daily_dump', 'operations.import_progress', 'ingestion_channel', 'E50'),
    Entry('E02', 'tonghuashun', 'stock_recent_dump', 'operations.import_progress', 'ingestion_channel', 'E50'),
    Entry('E03', 'tonghuashun', 'stock_actions_dump', 'operations.import_progress', 'ingestion_channel', 'E22'),
    Entry('E04', 'tonghuashun', 'fund_news', 'fund.news_window', 'business', None),
    Entry('E05', 'tonghuashun', 'anomaly_stock', 'market.stock_anomaly_window', 'business', None),
    Entry('E06', 'tonghuashun', 'rank_trend', 'market.rank_trend_window', 'business', None),
    Entry('E07', 'tonghuashun', 'fund_holdings', 'fund.reported_holdings_window', 'business', None),
    Entry('E08', 'tonghuashun', 'fund_stock_history', 'fund.reported_stock_holdings_window', 'business', None),
    Entry('E09', 'tonghuashun', 'fund_bond_history', 'fund.reported_bond_holdings_window', 'business', None),
    Entry('E10', 'tonghuashun', 'stock_indicators', 'market.financial_indicator_window', 'business', None),
    Entry('E11', 'tonghuashun', 'fund_financial_indicators', 'fund.financial_indicator_window', 'business', None),
    Entry('E12', 'tonghuashun', 'fund_income', 'fund.income_window', 'business', None),
    Entry('E13', 'tonghuashun', 'fund_balance', 'fund.balance_window', 'business', None),
    Entry('E14', 'tonghuashun', 'stock_income', 'market.income_window', 'business', None),
    Entry('E15', 'tonghuashun', 'stock_balance', 'market.balance_window', 'business', None),
    Entry('E16', 'tonghuashun', 'stock_cash_flow', 'market.cash_flow_window', 'business', None),
    Entry('E17', 'tonghuashun', 'fund_allocation', 'fund.allocation_window', 'business', None),
    Entry('E18', 'tonghuashun', 'fund_industry', 'fund.industry_window', 'business', None),
    Entry('E19', 'tonghuashun', 'fund_holders', 'fund.holder_composition_window', 'business', None),
    Entry('E20', 'tonghuashun', 'fund_top_holders', 'fund.top_holder_window', 'business', None),
    Entry('E21', 'tonghuashun', 'fund_dividends', 'fund.dividend_window', 'business', None),
    Entry('E22', 'tonghuashun', 'stock_actions', 'market.corporate_action_window', 'business', None),
    Entry('E23', 'tonghuashun', 'fund_manager_performance', 'fund.manager_performance_window', 'business', None),
    Entry('E24', 'tonghuashun', 'fund_manager_style', 'fund.manager_style_snapshot', 'business', None),
    Entry('E25', 'tonghuashun', 'fund_returns', 'fund.return_snapshot', 'business', None),
    Entry('E26', 'tonghuashun', 'fund_drawdowns', 'fund.drawdown_snapshot', 'business', None),
    Entry('E27', 'tonghuashun', 'fund_performance_history', 'fund.performance_window', 'business', None),
    Entry('E28', 'tonghuashun', 'dragon_tiger', 'market.dragon_tiger_snapshot', 'business', None),
    Entry('E29', 'tonghuashun', 'stock_auction', 'market.auction_snapshot', 'business', None),
    Entry('E30', 'tonghuashun', 'auction_benchmark', 'market.auction_benchmark_snapshot', 'business', None),
    Entry('E31', 'tonghuashun', 'limit_up', 'market.limit_up_snapshot', 'business', None),
    Entry('E32', 'tonghuashun', 'limit_down', 'market.limit_down_snapshot', 'business', None),
    Entry('E33', 'tonghuashun', 'limit_break', 'market.limit_break_snapshot', 'business', None),
    Entry('E34', 'tonghuashun', 'anomaly_list', 'market.anomaly_snapshot', 'business', None),
    Entry('E35', 'tonghuashun', 'limit_ladder', 'market.limit_ladder_window', 'business', None),
    Entry('E36', 'tonghuashun', 'stock_quote', 'market.stock_quote_snapshot', 'business', None),
    Entry('E37', 'tonghuashun', 'etf_quote', 'market.etf_quote_snapshot', 'business', None),
    Entry('E38', 'tonghuashun', 'index_quote', 'market.index_quote_snapshot', 'business', None),
    Entry('E39', 'tonghuashun', 'stock_valuation', 'market.stock_valuation_snapshot', 'business', None),
    Entry('E40', 'tonghuashun', 'tickers', 'instrument.reference', 'business', None),
    Entry('E41', 'tonghuashun', 'fund_company', 'fund.company', 'business', None),
    Entry('E42', 'tonghuashun', 'fund_manager', 'fund.manager', 'business', None),
    Entry('E43', 'tonghuashun', 'fund_profile', 'fund.profile', 'business', None),
    Entry('E44', 'tonghuashun', 'fund_nav', 'fund.nav_snapshot', 'business', None),
    Entry('E45', 'tonghuashun', 'hot_list', 'market.popularity_snapshot', 'business', None),
    Entry('E46', 'tonghuashun', 'skyrocket', 'market.rising_popularity_snapshot', 'business', None),
    Entry('E47', 'tonghuashun', 'hot_history', 'market.popularity_history_snapshot', 'business', None),
    Entry('E48', 'tonghuashun', 'fund_quota_summary', 'fund.quota_summary_snapshot', 'business', None),
    Entry('E49', 'tonghuashun', 'fund_quota_list', 'fund.quota_list_snapshot', 'business', None),
    Entry('E50', 'tonghuashun', 'stock_daily', 'market.stock_daily_window', 'business', None),
    Entry('E51', 'tonghuashun', 'etf_daily', 'market.etf_daily_window', 'business', None),
    Entry('E52', 'tonghuashun', 'index_daily', 'market.index_daily_window', 'business', None),
    Entry('E53', 'tonghuashun', 'fund_offerings', 'fund.offering_snapshot', 'business', None),
    Entry('E54', 'tonghuashun', 'fund_manager_experience', 'fund.manager_experience', 'business', None),
    Entry('E55', 'tonghuashun', 'calendar', 'market.calendar', 'business', None),
    Entry('E56', 'tonghuashun', 'index_catalog', 'index.category_snapshot', 'business', None),
    Entry('E57', 'tonghuashun', 'index_constituents', 'index.constituent_snapshot', 'business', None),
    Entry('E58', 'tushare', 'etf_code_mapping_audits', 'operations.etf_mapping_audit', 'source_support', 'E67'),
    Entry('E59', 'tushare', 'etf_daily_revision_audits', 'operations.etf_daily_revision', 'source_support', 'E70'),
    Entry('E60', 'tushare', 'corporate_action_source_facts', 'operations.corporate_action_source_fact', 'source_support', 'E61'),
    Entry('E61', 'tushare', 'corporate_action_facts', 'operations.corporate_action_fact', 'business', None),
    Entry('E62', 'tushare', 'corporate_action_coverage_facts', 'operations.corporate_action_coverage', 'source_support', 'E61'),
    Entry('E63', 'tushare', 'trading_status_source_facts', 'operations.trading_status_source_fact', 'source_support', 'E64'),
    Entry('E64', 'tushare', 'trading_status_facts', 'operations.trading_status_fact', 'business', None),
    Entry('E65', 'tushare', 'trading_status_coverage_facts', 'operations.trading_status_coverage', 'source_support', 'E64'),
    Entry('E66', 'tushare', 'trading_status_revision_audits', 'operations.trading_status_revision', 'source_support', 'E64'),
    Entry('E67', 'tushare', 'etf_directory', 'instrument.reference', 'business', None),
    Entry('E68', 'tushare', 'exchange_calendar', 'market.calendar', 'business', None),
    Entry('E69', 'tushare', 'etf_adjustment_factors', 'market.adjustment_factor', 'business', None),
    Entry('E70', 'tushare', 'etf_daily', 'market.fund_daily', 'business', None),
    Entry('E71', 'foundation', 'empty_local_scope', 'operations.empty_local_scope', 'remove_operational_pseudodataset', None),
)
BY_ID = {e.id:e for e in ENTRIES}
BY_NATIVE = {(e.source,e.native):e for e in ENTRIES}
SYNTHETIC = (Entry('B05','synthetic','tick','market.tick','business'), Entry('B05-M','synthetic','minute','market.minute','business'))
