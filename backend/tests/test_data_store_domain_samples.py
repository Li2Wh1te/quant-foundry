"""LF-D02 domain fixture acceptance; small synthetic source-shaped samples only.

This deliberately excludes B05 ten-million-tick capacity. Every baseline business
entry gets one explicit source-shaped fixture that must normalize and traverse the
real D01/D02 current-store pipeline. Non-business entries are checked as routing /
support / empty-state dispositions and are never normalized into fake datasets.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import BigInteger, Boolean, Column, MetaData, Numeric, Table, Text, text

from tests.test_data_store_kernel import database, limits, store  # isolated PostgreSQL and real Parquet/DuckDB fixtures
from app.data_store.adapters.contracts import LocalInput, digest, native_json
from app.data_store.adapters.normalize import normalize
from app.data_store.adapters.registry import BY_ID, ENTRIES
from app.data_store.domain_reader import read_object
from app.data_store.errors import DataStoreError
from app.data_store.local_sources import NativeSources, TABLES
from app.data_store.pipeline import PipelineOptions, run_entry
from app.data_store.tables import entry_status

NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)
DAY_MS = 1767283200000  # 2026-01-02 00:00 Asia/Shanghai
NEXT_DAY_MS = 1767369600000


def ths(entry_id, subject, content, *, variant='default'):
    entry = BY_ID[entry_id]
    return LocalInput('tonghuashun', entry.native, subject, variant, NOW, content, digest([entry_id, subject, content]))


def ts(entry_id, subject, row):
    entry = BY_ID[entry_id]
    return LocalInput('tushare', entry.native, subject, 'default', NOW, row, digest([entry_id, row]),
                      order_kind='current_table_snapshot', representation='local_table_baseline')


def report_period(kind='quarter'):
    return {'start_date_ms': DAY_MS, 'end_date_ms': NEXT_DAY_MS, 'report_type': kind,
            'report_type_name': {'quarter': '季度', 'annual': '年度', 'semiannual': '半年度'}[kind]}


def _financial(entry_id, subject):
    e = BY_ID[entry_id]
    amounts = {
        'fund_financial_indicators': ('asset_nav', Decimal('123456789.012345')),
        'fund_income': ('net_profit', Decimal('-123.456789')),
        'fund_balance': ('total_assets', Decimal('987654.321')),
        'stock_income': ('basic_eps', Decimal('1.234567')),
        'stock_balance': ('assets_total', Decimal('765432.10')),
        'stock_cash_flow': ('act_cash_flow_net', Decimal('-12345.67')),
    }
    if e.native.startswith('fund_'):
        row = {'start_date_ms': DAY_MS, 'end_date_ms': NEXT_DAY_MS, 'publish_date_ms': NEXT_DAY_MS}
    else:
        row = {'period_end_ms': NEXT_DAY_MS, 'report_date_ms': NEXT_DAY_MS,
               'currency': 'CNY', 'fiscal_period': 'Q1', 'period': 'Q1',
               'fiscal_year': 2026, 'thscode': subject}
    field, value = amounts[e.native]
    row[field] = value
    return ths(entry_id, subject, {'item': [row], 'period': 'Q1'})


def _market_list(entry_id, subject):
    e = BY_ID[entry_id]
    if e.native == 'stock_auction':
        return ths(entry_id, subject, {'item': [{'thscode': subject, 'ticker': '000001', 'name': '示例',
            'auction_price': Decimal('10.01')}], 'collection_scope': {'thscodes': subject},
            'auction_phase': 'closed', 'data_status': 'final', 'timestamp': DAY_MS})
    if e.native == 'anomaly_list':
        return ths(entry_id, 'market', {'item': [{'thscode': '000001.SZ', 'stock_name': '示例',
            'tag_name': '异动', 'keyword_list': ['样本'], 'analysis_content': '测试'}]})
    if e.native == 'limit_ladder':
        groups = ('two_board','three_board','four_board','five_board','six_board','seven_over')
        return ths(entry_id, 'market', {'coverage': 'provider_rolling_window',
            'window': {'board_caps': {g: 10 for g in groups}, 'date_list': [DAY_MS], 'length': 1},
            'item': [{'date': DAY_MS, 'boards': {g: [] for g in groups}}]})
    row = {'thscode': '000001.SZ', 'ticker': '000001', 'name': '示例'}
    if e.native == 'auction_benchmark':
        row.update(auction_pct=Decimal('1.2'), tags=['样本'])
    elif e.native == 'limit_up':
        row.update(last_price=Decimal('10'), max_seal_money=Decimal('100'), price_change_ratio_pct=Decimal('10'),
                   seal_money=Decimal('80'), continue_day_cnt=1, continue_day_text='首板', is_new=True, is_st=False,
                   limit_up_reason='测试', limit_up_time='10:00')
    elif e.native == 'limit_down':
        row.update(last_price=Decimal('9'), price_change_ratio_pct=Decimal('-10'), turnover_ratio_pct=Decimal('2'),
                   first_limit_time='10:00', last_limit_time='14:00')
    elif e.native == 'limit_break':
        row.update(last_price=Decimal('10'), price_change_ratio_pct=Decimal('4'), turnover=Decimal('1000'),
                   turnover_ratio_pct=Decimal('3'), open_times=2)
    return ths(entry_id, subject, {'item': [row], 'date': DAY_MS, 'collection_scope': {'date': DAY_MS}})


def sample(entry_id):
    # Narrative / series
    if entry_id == 'E04':
        return ths(entry_id, 'FUND.SH', {'requested_start': DAY_MS, 'requested_end': DAY_MS, 'coverage': 'requested_range',
            'scope_truncated': False, 'item': [{'id': 'n1', 'title': '标题', 'publish_time_ms': DAY_MS, 'top': False}]})
    if entry_id == 'E05':
        return ths(entry_id, '000001.SZ', {'item': [{'thscode': '000001.SZ', 'stock_name': '示例',
            'tag_name': '异动', 'keyword_list': ['样本'], 'analysis_content': '内容'}]})
    if entry_id == 'E06':
        return ths(entry_id, '000001.SZ', {'requested_start': DAY_MS, 'requested_end': DAY_MS,
            'item': [{'thscode': '000001.SZ', 'date_ms': DAY_MS, 'date': '20260102', 'ticker': '000001', 'rank': 1}]})

    # Portfolio / report families
    if entry_id == 'E07':
        return ths(entry_id, 'FUND.SH', {'item': [{'thscode': '000001.SZ', 'stock_name': '持仓',
            'end_date_ms': NEXT_DAY_MS, 'hold_ratio': Decimal('1.2'), 'investment_rank': 1}]})
    if entry_id in ('E08', 'E09'):
        period = report_period('quarter'); key = '2026-01-03:quarter'; asset = 'stock' if entry_id == 'E08' else 'bond'
        wrapper = {'report_key': key, 'report': period,
                   'data': {'item': [{'thscode': '000001.SZ', 'asset_type': asset, 'name': '成员',
                                      'report_type': '季度', 'end_date_ms': NEXT_DAY_MS, 'hold_ratio': Decimal('1')} ]}}
        return ths(entry_id, 'FUND.SH', {'report_directory': {'item': [period]}, 'item': [wrapper],
                                         'failed_requests': [], 'historical_revision_evidence': True})
    if entry_id == 'E10':
        return ths(entry_id, '000001.SZ', {'item': [{'thscode': '000001.SZ', 'report': '2026Q1',
            'abilities': [{'ability': 'profit', 'indicators': [{'index_id': 'roe', 'value': Decimal('10.5')}]}]}]})
    if entry_id in ('E11','E12','E13','E14','E15','E16'):
        return _financial(entry_id, 'FUND.SH' if entry_id in ('E11','E12','E13') else '000001.SZ')

    # Ownership / distributions
    if entry_id == 'E17':
        return ths(entry_id, 'FUND.SH', {'item': [{'report_date_ms': DAY_MS, 'stock_ratio_pct': Decimal('80')} ]})
    if entry_id == 'E18':
        return ths(entry_id, 'FUND.SH', {'item': [{'report_period': '2026Q1', 'industry_name': '科技', 'ratio_pct': Decimal('20')}]})
    if entry_id == 'E19':
        return ths(entry_id, 'FUND.SH', {'item': [{'report_date_ms': DAY_MS, 'merge_scope': 'all', 'holder_amount': 10}]})
    if entry_id == 'E20':
        return ths(entry_id, 'FUND.SH', {'item': [{'report_date_ms': DAY_MS, 'holder_name': '持有人', 'rank': 1,
                                                   'hold_share': Decimal('100')}]})
    if entry_id == 'E21':
        return ths(entry_id, 'FUND.SH', {'item': [{'ex_dividend_date_ms': DAY_MS, 'progress': 'done',
                                                   'per_ten_cash_before_tax': Decimal('1.2')}], 'dividend_count': 1})
    if entry_id == 'E22':
        return ths(entry_id, '000001.SZ', {'ticker': '000001', 'item': [{'thscode': '000001.SZ',
            'ticker': '000001', 'ex_date_ms': DAY_MS, 'dividend_per_share': Decimal('0.2')}]})

    # Manager/fund performance
    if entry_id == 'E23':
        return ths(entry_id, 'MGR1.month', {'collection_scope': {'manager_id': 'MGR1', 'range': 'month'},
            'item': [{'date_ms': DAY_MS, 'manager_return_pct': Decimal('1.1')}]})
    if entry_id == 'E24':
        return ths(entry_id, 'MGR1', {'collection_scope': {'manager_id': 'MGR1'}, 'item': [{
            'industry_preferences': [{'report_tag': '2026Q1', 'percent': [1,2,3,4,5,6,7], 'total_fund_scale': Decimal('10')}],
            'investment_idea': '价值'}]})
    if entry_id == 'E25':
        return ths(entry_id, 'FUND.SH', {'collection_scope': {'thscode': 'FUND.SH'},
            'item': [{'thscode': 'FUND.SH', 'return_week': Decimal('1'), 'rank_week': 1, 'rank_total_week': 10}]})
    if entry_id == 'E26':
        return ths(entry_id, 'FUND.SH', {'collection_scope': {'thscode': 'FUND.SH'},
            'item': [{'thscode': 'FUND.SH', 'week': Decimal('-2')}]})
    if entry_id == 'E27':
        return ths(entry_id, 'FUND.SH', {'collection_scope': {'thscode': 'FUND.SH'}, 'coverage': 'observed_rows_only',
            'item': [{'date_ms': DAY_MS, 'rsi_pct': Decimal('50')}]})

    # Market activity / snapshots
    if entry_id == 'E28':
        return ths(entry_id, '2026-01-02.all', {'collection_scope': {'board_type': 'all', 'date': '2026-01-02'},
            'provider_metadata': {'board_type': 'all', 'trade_date': '2026-01-02', 'count': 1, 'stock_count': 1},
            'item': [{'board_type': 'all', 'stock_items': [{'thscode': '000001.SZ', 'range_days': 1,
                                                           'concept_list': []}], 'hot_money_items': []}]})
    if entry_id in ('E29','E30','E31','E32','E33','E34','E35'):
        subject = '000001.SZ' if entry_id == 'E29' else 'market' if entry_id in ('E34','E35') else '2026-01-02'
        return _market_list(entry_id, subject)
    if entry_id in ('E36','E37','E38','E39'):
        code = '000001.SZ'; item = {'thscode': code, 'ticker': '000001', 'name': '示例'}
        if entry_id == 'E39': item['pe_ttm'] = Decimal('12.3')
        else: item.update(open_price=Decimal('10'), high_price=Decimal('11'), low_price=Decimal('9'), last_price=Decimal('10.5'))
        return ths(entry_id, code, {'item': [item], 'timestamp': DAY_MS, 'collection_scope': {'thscode': code}})

    # Reference/current records
    if entry_id == 'E40':
        return ths(entry_id, 'a-share', {'item': [{'thscode': '000001.SZ', 'asset_type': 'a-share', 'name': '示例'}]})
    if entry_id == 'E41':
        return ths(entry_id, 'COMP1', {'item': [{'company_id': 'COMP1', 'company_name': '公司', 'fund_count': 1}]})
    if entry_id == 'E42':
        return ths(entry_id, 'MGR1', {'item': [{'manager_id': 'MGR1', 'manager_name': '经理'}]})
    if entry_id == 'E43':
        return ths(entry_id, 'FUND.SH', {'item': [{'thscode': 'FUND.SH', 'fund_name': '基金'}]})
    if entry_id == 'E44':
        return ths(entry_id, 'FUND.SH', {'coverage': 'provider_rolling_window',
            'item': [{'thscode': 'FUND.SH', 'nav_date': '20260102', 'unit_nav': Decimal('1.2345')}]})
    if entry_id in ('E45','E46'):
        period = 'day' if entry_id == 'E45' else 'hour'
        return ths(entry_id, period, {'collection_scope': {'period': period},
            'item': [{'thscode': '000001.SZ', 'rank': 1, 'name': '示例', 'heat': Decimal('9.9')}]})
    if entry_id == 'E47':
        return ths(entry_id, '2026-01-02', {'date': '2026-01-02', 'collection_scope': {'date': '2026-01-02'},
            'item': [{'thscode': '000001.SZ', 'rank': 1, 'name': '示例'}]})
    if entry_id == 'E48':
        return ths(entry_id, 'QDII', {'category_scope': 'explicit_categories_only', 'collection_scope': {'tab': '["QDII"]'},
            'item': [{'name': 'QDII', 'buy': '可购', 'total': '100', 'total_limit': '200', 'unlimited': '否'}]})
    if entry_id == 'E49':
        return ths(entry_id, 'QDII', {'category_scope': 'explicit_categories_only', 'collection_scope': {'tab': '["QDII"]'},
            'item': [{'name': 'QDII', 'sub_tab': [{'name': '普通', 'fund_list': [{'thscode': 'FUND.SH',
                'fund_name': '基金', 'quota': '100', 'year': '2026', 'classify': ['A']}]}]}]})
    if entry_id in ('E50','E51','E52'):
        code = {'E50':'000001.SZ','E51':'510300.SH','E52':'000300.SH'}[entry_id]
        adjust = {'E50':'none','E51':'forward','E52':'not_applicable'}[entry_id]
        row = {'thscode': code, 'date_ms': DAY_MS, 'interval': '1d', 'adjust': adjust,
               'open_price': Decimal('10'), 'high_price': Decimal('11'), 'low_price': Decimal('9'),
               'close_price': Decimal('10.5'), 'volume': 100, 'turnover': Decimal('1000')}
        return ths(entry_id, code, {'thscode': code, 'coverage': 'observed_rows_only', 'adjust': adjust, 'item': [row]})
    if entry_id == 'E53':
        return ths(entry_id, 'active', {'collection_scope': {'subscribe': 'active'},
            'item': [{'thscode': 'FUND.SH', 'ticker': '基金', 'subscription_start_ms': DAY_MS,
                      'subscription_end_ms': NEXT_DAY_MS}]})
    if entry_id == 'E54':
        return ths(entry_id, 'MGR1', {'item': [{'investment_history': {'FUND.SH': {'code': 'FUND.SH', 'name': '基金',
                    'start': '2025-01-01', 'end': '2026-01-01'}}}]})
    if entry_id == 'E55':
        return ths(entry_id, 'provider-calendar', {'item': [{'date_ms': DAY_MS}]})
    if entry_id == 'E56':
        return ths(entry_id, 'cn_concept', {'item': [{'thscode': '000001.SH', 'name': '概念'}]})
    if entry_id == 'E57':
        return ths(entry_id, '000300.SH', {'item': [{'thscode': '000001.SZ', 'name': '成分'}]})

    # Tushare local tables
    if entry_id == 'E61':
        return ts(entry_id, 'actions', {'source': 'tushare', 'logical_fact_key': 'ca:1', 'instrument_id': 'inst-1',
            'action_type': 'cash_dividend', 'ex_date': '2026-01-02', 'cash_amount_per_unit': Decimal('0.2'),
            'currency': 'CNY', 'quality': 'complete', 'fact_version': 1})
    if entry_id == 'E64':
        return ts(entry_id, 'status', {'source': 'tushare', 'ts_code': '000001.SZ', 'trade_date': '2026-01-02',
            'dimension': 'suspend', 'status': 'normal', 'quality_status': 'complete'})
    if entry_id == 'E67':
        return ts(entry_id, 'directory', {'source': 'tushare', 'ts_code': '510300.SH', 'csname': 'ETF',
                                          'exchange': 'SSE', 'list_date': '20260102'})
    if entry_id == 'E68':
        return ts(entry_id, 'calendar', {'exchange': 'SSE', 'calendar_date': '2026-01-02', 'is_open': True})
    if entry_id == 'E69':
        return ts(entry_id, 'factor', {'source': 'tushare', 'ts_code': '510300.SH',
                                       'trade_date': '2026-01-02', 'adj_factor': Decimal('1.001')})
    if entry_id == 'E70':
        return ts(entry_id, 'daily', {'source': 'tushare', 'ts_code': '510300.SH', 'trade_date': '2026-01-02',
            'open': Decimal('4.1'), 'high': Decimal('4.2'), 'low': Decimal('4.0'), 'close': Decimal('4.15'),
            'vol': Decimal('1000'), 'amount': Decimal('4100')})
    raise KeyError(entry_id)


BUSINESS_IDS = tuple(e.id for e in ENTRIES if e.business)
NONBUSINESS_IDS = tuple(e.id for e in ENTRIES if not e.business)

# Each expectation is written from the source contract rather than computed by
# the adapter under test: (native subject, business key, decoded field path, value).
# A successful normalization with missing/misrouted optional values must fail here.
GOLDEN = {
    'E04': ('FUND.SH', 'n1', ('reported_title',), '标题'),
    'E05': ('000001.SZ', 'current', ('narratives', 0, 'reported_analysis'), '内容'),
    'E06': ('000001.SZ', '2026-01-02', ('reported_rank',), 1),
    'E07': ('FUND.SH', '2026-01-03', ('members', 0, 'reported_hold_ratio'), '1.2'),
    'E08': ('FUND.SH', '2026-01-03:quarter', ('members', 0, 'reported_asset_type'), 'stock'),
    'E09': ('FUND.SH', '2026-01-03:quarter', ('members', 0, 'reported_asset_type'), 'bond'),
    'E10': ('000001.SZ', '2026Q1', ('abilities', 0, 'indicators', 0, 'reported_value'), '10.5'),
    'E11': ('FUND.SH', '2026-01-03:2026-01-02', ('reported_asset_nav',), '123456789.012345'),
    'E12': ('FUND.SH', '2026-01-03:2026-01-02', ('reported_net_profit',), '-123.456789'),
    'E13': ('FUND.SH', '2026-01-03:2026-01-02', ('reported_total_assets',), '987654.321'),
    'E14': ('000001.SZ', '2026-01-03:Q1', ('reported_basic_eps',), '1.234567'),
    'E15': ('000001.SZ', '2026-01-03:Q1', ('reported_assets_total',), '765432.1'),
    'E16': ('000001.SZ', '2026-01-03:Q1', ('reported_act_cash_flow_net',), '-12345.67'),
    'E17': ('FUND.SH', '2026-01-02', ('members', 0, 'reported_stock_ratio_pct'), '80'),
    'E18': ('FUND.SH', '2026Q1', ('members', 0, 'reported_industry_name'), '科技'),
    'E19': ('FUND.SH', '2026-01-02', ('members', 0, 'reported_holder_amount'), 10),
    'E20': ('FUND.SH', '2026-01-02', ('members', 0, 'reported_hold_share'), '100'),
    'E21': ('FUND.SH', '2026-01-02', ('members', 0, 'reported_per_ten_cash_before_tax'), '1.2'),
    'E22': ('000001.SZ', '2026-01-02', ('members', 0, 'reported_dividend_per_share'), '0.2'),
    'E23': ('MGR1.month', '2026-01-02', ('reported_manager_return_pct',), '1.1'),
    'E24': ('MGR1', 'current', ('reported_investment_idea',), '价值'),
    'E25': ('FUND.SH', 'current', ('periods', 0, 'reported_return'), '1'),
    'E26': ('FUND.SH', 'current', ('periods', 0, 'reported_drawdown'), '-2'),
    'E27': ('FUND.SH', '2026-01-02', ('reported_rsi_pct',), '50'),
    'E28': ('dragon_tiger:2026-01-02.all', '2026-01-02', ('stocks', 0, 'source_code'), '000001.SZ'),
    'E29': ('000001.SZ', 'current', ('reported_auction_price',), '10.01'),
    'E30': ('auction_benchmark:2026-01-02', '2026-01-02', ('members', 0, 'reported_auction_pct'), '1.2'),
    'E31': ('limit_up:2026-01-02', '2026-01-02', ('members', 0, 'reported_seal_money'), '80'),
    'E32': ('limit_down:2026-01-02', '2026-01-02', ('members', 0, 'reported_last_price'), '9'),
    'E33': ('limit_break:2026-01-02', '2026-01-02', ('members', 0, 'reported_open_times'), 2),
    'E34': ('anomaly_list:market', 'current', ('members', 0, 'reported_tag'), '异动'),
    'E35': ('limit_ladder:market', '2026-01-02', ('reported_groups', 'seven_over'), []),
    'E36': ('000001.SZ', 'current', ('reported_last_price',), '10.5'),
    'E37': ('000001.SZ', 'current', ('reported_last_price',), '10.5'),
    'E38': ('000001.SZ', 'current', ('reported_last_price',), '10.5'),
    'E39': ('000001.SZ', 'current', ('reported_pe_ttm',), '12.3'),
    'E40': ('000001.SZ', 'current', ('name',), '示例'),
    'E41': ('COMP1', 'current', ('name',), '公司'),
    'E42': ('MGR1', 'current', ('name',), '经理'),
    'E43': ('FUND.SH', 'current', ('name',), '基金'),
    'E44': ('FUND.SH', '2026-01-02', ('reported_unit_nav',), '1.2345'),
    'E45': ('hot_list:day', 'current', ('members', 0, 'reported_heat'), '9.9'),
    'E46': ('skyrocket:hour', 'current', ('members', 0, 'reported_heat'), '9.9'),
    'E47': ('hot_history:2026-01-02', '2026-01-02', ('members', 0, 'reported_rank'), 1),
    'E48': ('QDII', 'current', ('reported_groups', 0, 'reported_total_text'), '100'),
    'E49': ('QDII', 'current', ('reported_groups', 0, 'subcategories', 0, 'funds', 0, 'reported_quota_text'), '100'),
    'E50': ('000001.SZ', '2026-01-02', ('reported_close',), '10.5'),
    'E51': ('510300.SH', '2026-01-02', ('reported_close',), '10.5'),
    'E52': ('000300.SH', '2026-01-02', ('reported_close',), '10.5'),
    'E53': ('active', 'current', ('members', 0, 'source_code'), 'FUND.SH'),
    'E54': ('MGR1', 'FUND.SH:2025-01-01', ('end_text',), '2026-01-01'),
    'E55': ('provider-calendar', '2026-01-02', ('calendar_date',), '2026-01-02'),
    'E56': ('cn_concept', 'current', ('members', 0, 'name'), '概念'),
    'E57': ('000300.SH', 'current', ('members', 0, 'name'), '成分'),
    'E61': ('inst-1', 'event:8dfca90436aa774d965e2443b5dd56b5f0de93d8678c4eb1b828cb86c38b8f63',
            ('cash_amount_per_unit',), '0.2'),
    'E64': ('000001.SZ', '2026-01-02:suspend', ('status',), 'normal'),
    'E67': ('510300.SH', '510300.SH', ('name',), 'ETF'),
    'E68': ('SSE', '2026-01-02', ('is_open',), True),
    'E69': ('510300.SH', '2026-01-02', ('factor',), '1.001'),
    'E70': ('510300.SH', '2026-01-02', ('turnover_thousand_yuan',), '4100'),
}

RESTRICTION_GOLDEN = {
    'E08': ('portfolio_complete', 'PROVIDER_REPORTED_SCOPE_ONLY'),
    'E11': ('normalized_currency', 'CURRENCY_NOT_NORMALIZED'),
    'E50': ('adjustment_anchor', 'ADJUSTMENT_ANCHOR_UNVERIFIED'),
    'E51': ('adjustment_anchor', 'ADJUSTMENT_ANCHOR_UNVERIFIED'),
    'E55': ('is_open', 'EXCHANGE_SESSION_UNVERIFIED'),
    'E69': ('anchor_date', 'ADJUSTMENT_ANCHOR_UNVERIFIED'),
    'E70': ('previous_close', 'MISSING'),
}


def field_at(value, path):
    for part in path:
        value = value[part]
    return value


def current_object(store, entry_id):
    subject, object_key, _, _ = GOLDEN[entry_id]
    return read_object(store, entry_id, sample(entry_id).representation_key, subject, object_key)


@pytest.mark.parametrize('entry_id', BUSINESS_IDS)
def test_every_business_entry_has_a_source_shaped_sample_that_normalizes(entry_id):
    assert set(GOLDEN) == set(BUSINESS_IDS)
    entry = BY_ID[entry_id]
    raw = sample(entry_id)
    units = list(normalize(entry, raw))
    assert units, entry_id
    assert all(unit.failure is None for unit in units), (entry_id, [(u.object_key, u.failure) for u in units])
    assert all(unit.rows for unit in units), entry_id
    assert all(unit.subject for unit in units)
    assert all(unit.representation == raw.representation_key for unit in units)
    subject, object_key, _, _ = GOLDEN[entry_id]
    assert [(unit.subject, unit.object_key) for unit in units] == [(subject, object_key)]


def _native_column(value):
    if type(value) is bool:
        return Boolean()
    if type(value) is int:
        return BigInteger()
    if isinstance(value, Decimal):
        return Numeric(38, 18)
    return Text()


def seed_native_sources(engine):
    """Persist contract-shaped inputs behind the production local readers.

    The test schema contains only synthetic observations and minimal typed local
    tables. No supplier client or production table is accessed. Actual source
    reconstruction, PostgreSQL reads, Parquet writes and DuckDB reads still run.
    """
    with engine.begin() as connection:
        connection.exec_driver_sql('''CREATE TABLE tonghuashun_observations (
            id uuid PRIMARY KEY, dataset text NOT NULL, subject text NOT NULL,
            variant text NOT NULL, observed_at timestamptz NOT NULL,
            content_hash text NOT NULL, data_json text NOT NULL,
            request_json text NOT NULL, base_observation_id uuid)''')
        connection.exec_driver_sql('''CREATE TABLE tonghuashun_collection_states (
            dataset text, status text, subject text, variant text,
            attempted_at timestamptz, error_kind text)''')
        connection.exec_driver_sql('CREATE TABLE tonghuashun_dump_imports (dataset text, status text)')
        for entry in ENTRIES:
            if entry.source != 'tonghuashun' or not entry.business:
                continue
            raw = sample(entry.id)
            connection.execute(text('''INSERT INTO tonghuashun_observations
                (id,dataset,subject,variant,observed_at,content_hash,data_json,request_json)
                VALUES (:id,:dataset,:subject,:variant,:observed_at,:content_hash,:data_json,:request_json)'''),
                {'id': uuid4(), 'dataset': entry.native, 'subject': raw.subject,
                 'variant': raw.variant, 'observed_at': raw.observed_at,
                 'content_hash': digest(raw.content), 'data_json': native_json(raw.content),
                 'request_json': '[]'})
        for entry_id in ('E01', 'E02', 'E03'):
            connection.execute(text('INSERT INTO tonghuashun_dump_imports VALUES (:dataset, :status)'),
                               {'dataset': BY_ID[entry_id].native, 'status': 'completed'})

    metadata = MetaData()
    rows = {}
    for entry in ENTRIES:
        if entry.source != 'tushare':
            continue
        table_name, source_column = TABLES[entry.native]
        row = dict(sample(entry.id).content) if entry.business else {}
        if source_column:
            row[source_column] = 'tushare'
        if not row:
            raise AssertionError(f'No native fields for {entry.id}')
        table = Table(table_name, metadata,
                      *(Column(name, _native_column(value)) for name, value in row.items()))
        rows[table_name] = row
    metadata.create_all(engine)
    with engine.begin() as connection:
        for table in metadata.sorted_tables:
            connection.execute(table.insert().values(**rows[table.name]))


def add_observation(engine, entry_id, content, observed_at, *, base_id=None, stored_data=None, requests=()):
    """Append one immutable, checksum-valid native observation to the test schema."""
    raw = sample(entry_id)
    identity = uuid4()
    with engine.begin() as connection:
        connection.execute(text('''INSERT INTO tonghuashun_observations
            (id,dataset,subject,variant,observed_at,content_hash,data_json,request_json,base_observation_id)
            VALUES (:id,:dataset,:subject,:variant,:observed_at,:content_hash,:data_json,:request_json,:base_id)'''),
            {'id': identity, 'dataset': raw.dataset, 'subject': raw.subject, 'variant': raw.variant,
             'observed_at': observed_at, 'content_hash': digest(content),
             'data_json': native_json(content if stored_data is None else stored_data),
             'request_json': native_json(requests), 'base_id': base_id})
    return identity


@pytest.fixture
def ready(store):
    with store.catalog.engine.begin() as c:
        entry_status.create(c)
    seed_native_sources(store.catalog.engine)
    return store


def test_all_sixty_business_entries_traverse_real_current_store_pipeline(ready):
    native = NativeSources(ready.catalog.engine)
    completed = []
    for entry_id in BUSINESS_IDS:
        entry = BY_ID[entry_id]
        result = run_entry(ready, entry, native)
        assert result['complete'] is True, (entry_id, result)
        assert result['qualified'] is True, (entry_id, result)
        assert result['normalized_units'] >= 1, (entry_id, result)
        assert result['source_rows'] >= 1 and native.summary['complete'], entry_id

        subject, object_key, path, expected = GOLDEN[entry_id]
        raw = sample(entry_id)
        stored = read_object(ready, entry_id, raw.representation_key, subject, object_key)
        assert stored['found'] and stored['generation'] >= 1, entry_id
        assert field_at(stored['data'], path) == expected, (entry_id, path, stored['data'])
        assert stored['capability']['dataset'] == entry.spec.name, entry_id
        if entry_id in RESTRICTION_GOLDEN:
            field, reason = RESTRICTION_GOLDEN[entry_id]
            assert stored['field_quality'][field] == reason, entry_id
        partition = entry.spec.partitioner((raw.representation_key, subject, object_key, 'root'))
        assert ready.catalog.files(entry.spec.name, partition), entry_id
        completed.append(entry_id)
    assert tuple(completed) == BUSINESS_IDS
    assert len(completed) == 60


def test_all_eleven_nonbusiness_entries_route_without_publishing_fake_datasets(ready):
    native = NativeSources(ready.catalog.engine)
    for entry_id in NONBUSINESS_IDS:
        entry = BY_ID[entry_id]
        summary = run_entry(ready, entry, native)
        assert summary['complete'] and summary['state'] == entry.disposition, entry_id
        if entry.disposition == 'ingestion_channel':
            assert summary['target_entry'] == entry.target, entry_id
        else:
            assert 'target_entry' not in summary, entry_id
        assert summary['source_rows'] == (0 if entry_id == 'E71' else 1), entry_id
    assert len(NONBUSINESS_IDS) == 11


def test_real_observation_delta_newer_value_wins_and_late_old_input_does_not_regress(ready):
    entry = BY_ID['E50']
    native = NativeSources(ready.catalog.engine)
    run_entry(ready, entry, native)
    assert current_object(ready, 'E50')['data']['reported_close'] == '10.5'

    with ready.catalog.engine.connect() as connection:
        base_id = connection.execute(text("SELECT id FROM tonghuashun_observations WHERE dataset='stock_daily'"))\
                            .scalar_one()
    original = sample('E50').content
    corrected = deepcopy(original)
    corrected['item'][0]['close_price'] = Decimal('12.345678')
    corrected['item'][0]['high_price'] = Decimal('13')
    delta = {'key_field': 'date_ms', 'removed': [], 'upserts': corrected['item'],
             'metadata': {key: value for key, value in corrected.items() if key != 'item'}}
    receipt = {'parameters': {'start': DAY_MS, 'end': DAY_MS},
               'key_receipt': 'actual_returned_keys_v1', 'returned_keys': {'date_ms': [DAY_MS]}}
    add_observation(ready.catalog.engine, 'E50', corrected, NOW + timedelta(seconds=2),
                    base_id=base_id, stored_data=delta, requests=(receipt,))
    result = run_entry(ready, entry, native)
    assert result['qualified'], result
    assert current_object(ready, 'E50')['data']['reported_close'] == '12.345678'

    older = deepcopy(original)
    older['item'][0]['close_price'] = Decimal('8.25')
    older['item'][0]['low_price'] = Decimal('8')
    add_observation(ready.catalog.engine, 'E50', older, NOW - timedelta(seconds=1))
    result = run_entry(ready, entry, native)
    assert result['qualified'] and current_object(ready, 'E50')['data']['reported_close'] == '12.345678'
    repeat = run_entry(ready, entry, native)
    assert repeat['metrics']['files_written'] == 0


def test_fund_report_bad_member_blocks_whole_report_until_complete_repair(ready):
    entry = BY_ID['E08']
    native = NativeSources(ready.catalog.engine)
    run_entry(ready, entry, native)
    assert current_object(ready, 'E08')['data']['members'][0]['source_member_code'] == '000001.SZ'

    malformed = deepcopy(sample('E08').content)
    malformed['item'][0]['data']['item'].append({'thscode': None, 'asset_type': 'stock', 'name': '坏成员',
                                                  'report_type': '季度', 'end_date_ms': NEXT_DAY_MS,
                                                  'hold_ratio': Decimal('2')})
    add_observation(ready.catalog.engine, 'E08', malformed, NOW + timedelta(seconds=1))
    result = run_entry(ready, entry, native)
    assert result['complete'] and not result['qualified']
    with pytest.raises(DataStoreError) as caught:
        current_object(ready, 'E08')
    assert caught.value.code == 'DATA_RESTRICTED'

    repaired = deepcopy(sample('E08').content)
    repaired['item'][0]['data']['item'][0]['name'] = '修复成员'
    add_observation(ready.catalog.engine, 'E08', repaired, NOW + timedelta(seconds=2))
    representation = sample('E08').representation_key
    partition = entry.spec.partitioner((representation, 'FUND.SH', '2026-01-03:quarter', 'root'))
    result = run_entry(ready, entry, native, options=PipelineOptions(mode='retry', partitions=(partition,)))
    assert result['qualified']
    members = current_object(ready, 'E08')['data']['members']
    assert len(members) == 1 and members[0]['reported_name'] == '修复成员'


def test_failed_native_refresh_stays_restricted_until_new_valid_observation(ready):
    entry = BY_ID['E41']
    native = NativeSources(ready.catalog.engine)
    run_entry(ready, entry, native)
    with ready.catalog.engine.begin() as connection:
        connection.execute(text('''INSERT INTO tonghuashun_collection_states
            (dataset,status,subject,variant,attempted_at,error_kind)
            VALUES ('fund_company','failed','COMP1','default',:when,'synthetic-test')'''),
            {'when': NOW + timedelta(seconds=1)})
    result = run_entry(ready, entry, native)
    assert result['complete'] and not result['qualified'] and native.summary['source_failures'] == 1
    with pytest.raises(DataStoreError) as caught:
        current_object(ready, 'E41')
    assert caught.value.code == 'DATA_RESTRICTED'

    fixed = deepcopy(sample('E41').content)
    fixed['item'][0]['company_name'] = '修复公司'
    add_observation(ready.catalog.engine, 'E41', fixed, NOW + timedelta(seconds=2))
    with ready.catalog.engine.begin() as connection:
        connection.execute(text("DELETE FROM tonghuashun_collection_states WHERE dataset='fund_company'"))
    representation = sample('E41').representation_key
    partition = entry.spec.partitioner((representation, 'COMP1', 'current', 'root'))
    result = run_entry(ready, entry, native, options=PipelineOptions(mode='retry', partitions=(partition,)))
    assert result['qualified'], result
    assert current_object(ready, 'E41')['data']['name'] == '修复公司'


def test_explicit_empty_index_set_replaces_members_without_inventing_missing_data(ready):
    entry = BY_ID['E57']
    native = NativeSources(ready.catalog.engine)
    run_entry(ready, entry, native)
    assert len(current_object(ready, 'E57')['data']['members']) == 1
    add_observation(ready.catalog.engine, 'E57', {'item': []}, NOW + timedelta(seconds=1))
    result = run_entry(ready, entry, native)
    assert result['qualified'] and current_object(ready, 'E57')['data']['members'] == []

    # A missing collection member list has different meaning from a proven
    # empty list, so it must restrict the current object until a full retry.
    add_observation(ready.catalog.engine, 'E57', {}, NOW + timedelta(seconds=2))
    result = run_entry(ready, entry, native)
    assert result['complete'] and not result['qualified']
    with pytest.raises(DataStoreError) as caught:
        current_object(ready, 'E57')
    assert caught.value.code == 'DATA_RESTRICTED'
    add_observation(ready.catalog.engine, 'E57', {'item': []}, NOW + timedelta(seconds=3))
    assert run_entry(ready, entry, native)['qualified']
    assert current_object(ready, 'E57')['data']['members'] == []


def test_tushare_local_table_correction_is_read_and_published_without_network(ready, monkeypatch):
    import requests

    def reject_supplier(*args, **kwargs):
        raise AssertionError('The local-table acceptance must not request supplier data')

    monkeypatch.setattr(requests.Session, 'request', reject_supplier)
    entry = BY_ID['E70']
    native = NativeSources(ready.catalog.engine)
    run_entry(ready, entry, native)
    assert current_object(ready, 'E70')['data']['close'] == '4.15'
    with ready.catalog.engine.begin() as connection:
        connection.execute(text("""UPDATE etf_daily_bars SET close=:close, high=:high
                              WHERE ts_code='510300.SH'"""),
                           {'close': Decimal('4.25'), 'high': Decimal('4.30')})
    result = run_entry(ready, entry, native)
    assert result['qualified'], result
    assert current_object(ready, 'E70')['data']['close'] == '4.25'

    with ready.catalog.engine.begin() as connection:
        connection.execute(text("UPDATE etf_daily_bars SET close=:value WHERE ts_code='510300.SH'"),
                           {'value': Decimal('4.50')})
    result = run_entry(ready, entry, native)
    assert result['complete'] and not result['qualified']
    with pytest.raises(DataStoreError) as caught:
        current_object(ready, 'E70')
    assert caught.value.code == 'DATA_RESTRICTED'
    with ready.catalog.engine.begin() as connection:
        connection.execute(text("UPDATE etf_daily_bars SET high=:value WHERE ts_code='510300.SH'"),
                           {'value': Decimal('4.60')})
    assert run_entry(ready, entry, native)['qualified']
    assert current_object(ready, 'E70')['data']['close'] == '4.5'


def test_nonbusiness_entries_stay_support_routing_or_empty_state_not_fake_business_data():
    assert set(NONBUSINESS_IDS) == {'E01','E02','E03','E58','E59','E60','E62','E63','E65','E66','E71'}
    for entry_id in NONBUSINESS_IDS:
        entry = BY_ID[entry_id]
        assert not entry.business and entry.spec is None
        capability = entry.describe_capability()
        assert capability['dataset'] is None
        assert capability['supplier_network_required'] is False
        if entry.disposition == 'ingestion_channel':
            assert entry.target in ('E50','E22')
        elif entry.disposition == 'source_support':
            assert entry.target in ('E67','E70','E61','E64')
        else:
            assert entry_id == 'E71' and entry.disposition == 'remove_operational_pseudodataset'
