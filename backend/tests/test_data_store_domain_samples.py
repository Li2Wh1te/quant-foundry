"""LF-D02 domain fixture acceptance; small synthetic source-shaped samples only.

This deliberately excludes B05 ten-million-tick capacity. Every baseline business
entry gets one explicit source-shaped fixture that must normalize and traverse the
real D01/D02 current-store pipeline. Non-business entries are checked as routing /
support / empty-state dispositions and are never normalized into fake datasets.
"""
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from tests.test_data_store_kernel import store  # isolated PostgreSQL + real Parquet/DuckDB fixture
from app.data_store.adapters.contracts import LocalInput, digest
from app.data_store.adapters.normalize import normalize
from app.data_store.adapters.registry import BY_ID, ENTRIES
from app.data_store.pipeline import run_entry
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
    if e.native.startswith('fund_'):
        row = {'start_date_ms': DAY_MS, 'end_date_ms': NEXT_DAY_MS, 'publish_date_ms': NEXT_DAY_MS}
    else:
        row = {'period_end_ms': NEXT_DAY_MS, 'report_date_ms': NEXT_DAY_MS,
               'currency': 'CNY', 'fiscal_period': 'Q1', 'period': 'Q1',
               'fiscal_year': 2026, 'thscode': subject}
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
            'currency': 'CNY', 'quality': 'complete'})
    if entry_id == 'E64':
        return ts(entry_id, 'status', {'source': 'tushare', 'ts_code': '000001.SZ', 'trade_date': '2026-01-02',
            'dimension': 'suspend', 'status': 'normal', 'quality_status': 'complete'})
    if entry_id == 'E67':
        return ts(entry_id, 'directory', {'ts_code': '510300.SH', 'name': 'ETF', 'exchange': 'SSE', 'list_date': '20260102'})
    if entry_id == 'E68':
        return ts(entry_id, 'calendar', {'exchange': 'SSE', 'calendar_date': '2026-01-02', 'is_open': True})
    if entry_id == 'E69':
        return ts(entry_id, 'factor', {'ts_code': '510300.SH', 'trade_date': '2026-01-02', 'adj_factor': Decimal('1.001')})
    if entry_id == 'E70':
        return ts(entry_id, 'daily', {'source': 'tushare', 'ts_code': '510300.SH', 'trade_date': '2026-01-02',
            'open': Decimal('4.1'), 'high': Decimal('4.2'), 'low': Decimal('4.0'), 'close': Decimal('4.15'),
            'vol': Decimal('1000'), 'amount': Decimal('4100')})
    raise KeyError(entry_id)


BUSINESS_IDS = tuple(e.id for e in ENTRIES if e.business)
NONBUSINESS_IDS = tuple(e.id for e in ENTRIES if not e.business)


@pytest.mark.parametrize('entry_id', BUSINESS_IDS)
def test_every_business_entry_has_a_source_shaped_sample_that_normalizes(entry_id):
    entry = BY_ID[entry_id]
    raw = sample(entry_id)
    units = list(normalize(entry, raw))
    assert units, entry_id
    assert all(unit.failure is None for unit in units), (entry_id, [(u.object_key, u.failure) for u in units])
    assert all(unit.rows for unit in units), entry_id
    assert all(unit.subject for unit in units)
    assert all(unit.representation == raw.representation_key for unit in units)


class OnePass:
    def __init__(self, raw):
        self.raw = raw
        self.summary = {}
    def iter_entry(self, entry):
        self.summary = {'complete': False}
        yield self.raw
        self.summary = {'complete': True, 'source_rows': 1, 'state': 'present'}


@pytest.fixture
def ready(store):
    with store.catalog.engine.begin() as c:
        entry_status.create(c)
    return store


def test_all_sixty_business_entries_traverse_real_current_store_pipeline(ready):
    completed = []
    for entry_id in BUSINESS_IDS:
        entry = BY_ID[entry_id]
        result = run_entry(ready, entry, OnePass(sample(entry_id)))
        assert result['complete'] is True, (entry_id, result)
        assert result['qualified'] is True, (entry_id, result)
        assert result['normalized_units'] >= 1, (entry_id, result)
        completed.append(entry_id)
    assert tuple(completed) == BUSINESS_IDS
    assert len(completed) == 60


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
            assert entry.target in ('E67','E61','E64')
        else:
            assert entry_id == 'E71' and entry.disposition == 'remove_operational_pseudodataset'
