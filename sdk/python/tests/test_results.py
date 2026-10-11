"""Installed-wheel D11 results through real D09/D10 execution, no analyzer oracle."""
import json
from decimal import Decimal
from types import SimpleNamespace
import unittest
from quantfoundry import BacktestClient, ContractError, RunResults
from quantfoundry.api import order, LimitOrder, log, record
from quantfoundry._worker import _run_module
from support_sdk import spec

class ResultsTests(unittest.TestCase):
    def run_strategy(self,module,source):
        result = _run_module(module,source)
        self.addCleanup(result.close)
        return result
    def test_real_account_hand_oracle_and_exact_page_values(self):
        source=spec(days=1,securities=('A.SH',),frequency='1m')
        for row in source['source']['events'][1:]:
            row['event'].update(open='12',high='13',low='11',close='12')
        def before(c): order('A.SH',100,style=LimitOrder('10'))
        def bars(c,d):
            if c.portfolio.positions['A.SH'].quantity == 100:
                order('A.SH',-40,style=LimitOrder('12'))
        # Submit the sale only once; pending target/trade history is Rust-owned.
        count=0
        def once(c,d):
            nonlocal count
            count+=1
            if count==1: bars(c,d)
        result=self.run_strategy(SimpleNamespace(before_trading_start=before,handle_data=once),source)
        summary=result.summary();self.assertEqual(summary['outcome']['status'],'succeeded')
        p=summary['results']['performance']
        self.assertEqual(p['terminal_equity'],Decimal('10198'))
        self.assertAlmostEqual(p['cumulative_return']['value'],.0198,places=14)
        self.assertEqual(p['trades']['fill_fees'],Decimal('2'))
        self.assertEqual(p['trades']['total_notional'],Decimal('1480'))
        self.assertEqual(p['trades']['account_totals']['realized_pnl'],Decimal('78.6'))
        page=result.page(kind='trades',limit=1)
        self.assertEqual(page.items[0]['price'],Decimal('10'));self.assertFalse(page.partial)
        following=result.page(kind='trades',limit=1,cursor=page.next_cursor)
        self.assertEqual(following.items[0]['price'],Decimal('12'));self.assertIsNone(following.next_cursor)
        self.assertIsInstance(following.items[0]['execution_time_ns'],int)
        self.assertLess(page.items[0]['result_sequence'],following.items[0]['result_sequence'])
        with self.assertRaises(TypeError): page.items[0]['price']=Decimal('11')
        position=result.page(kind='positions',limit=1).items[0]
        self.assertEqual(position['cost_basis_total'],Decimal('600.6'))
        curve=result.page(kind='equity',limit=10)
        self.assertEqual([p['equity'] for p in curve.items],[Decimal('10000'),Decimal('10198')])
    def test_default_daily_statistics_fine_curve_comparison_and_empty_source(self):
        output=[]
        for sampling in ('session_close','bar_close'):
            source=spec(days=2,frequency='1m');source['config']['result_sampling']=sampling
            result=self.run_strategy(SimpleNamespace(),source)
            summary=result.summary();self.assertEqual(summary['outcome']['status'],'succeeded')
            output.append(summary['results']['performance'])
            self.assertEqual(len(result.page(kind='equity',limit=100).items),3 if sampling=='session_close' else 11)
        self.assertEqual(output[0]['sharpe'],output[1]['sharpe'])
        self.assertEqual(output[0]['volatility']['value'],0)
        source=spec(days=1);source['source']['events']=[]
        result=self.run_strategy(SimpleNamespace(),source)
        self.assertEqual(result.summary()['results']['performance']['cumulative_return']['value'],0)
    def test_real_missing_mark_day_is_retained_and_not_bridged(self):
        source=spec(days=3,securities=('A.SH',))
        source['source']['events']=[e for e in source['source']['events'] if e['event']['session']!='S2']
        def before(c):
            if not c.portfolio.positions: order('A.SH',1,style=LimitOrder('10'))
        result=self.run_strategy(SimpleNamespace(before_trading_start=before),source)
        self.assertEqual(result.summary()['outcome']['status'],'succeeded')
        p=result.summary()['results']['performance']
        self.assertEqual(p['missing_equity_sessions'],1)
        self.assertEqual(p['cumulative_return']['reason'],'MISSING_EQUITY')
        points=result.page(kind='equity',limit=10).items
        self.assertIsNone(points[2]['equity']);self.assertEqual(points[3]['session_return']['reason'],'MISSING_EQUITY_SEGMENT')
    def test_aligned_benchmark_and_missing_origin_are_explicit(self):
        for origin in (True,False):
            source=spec(days=3,securities=('A.SH','BENCH.SH'));source['config']['benchmark']='BENCH.SH'
            if origin: source['benchmark_origin']=dict(time_ns=source['calendar'][0]['before_open_ns'],value='100',unavailable_reason=None)
            for e in source['source']['events']:
                if e['event']['security']=='BENCH.SH':
                    value=100+10*(int(e['event']['session'][1:])-1)
                    e['event'].update(open=str(value),high=str(value+1),low=str(value-1),close=str(value))
            result=self.run_strategy(SimpleNamespace(),source);p=result.summary()['results']['performance']
            if origin:
                self.assertAlmostEqual(p['benchmark_return']['value'],.2)
                self.assertAlmostEqual(p['cumulative_excess_return']['value'],-.2)
            else: self.assertEqual(p['benchmark_return']['reason'],'MISSING_BENCHMARK')
    def test_empty_logs_are_count_bounded_and_visible(self):
        result=self.run_strategy(SimpleNamespace(handle_data=lambda c,d:[log.info('') for _ in range(600)]),spec(days=5,frequency='1m'))
        summary=result.summary();self.assertEqual(summary['outcome']['status'],'succeeded')
        self.assertEqual(summary['results']['counts']['logs'],10000)
        self.assertTrue(summary['logs_truncated'])
    def test_kind_and_cursor_limits_closed_reads_and_nonfinite_responses(self):
        result=self.run_strategy(SimpleNamespace(),spec(days=1))
        for kind,limit,cursor in [('all',1,None),('logs',True,None),('logs',10001,None),('logs',1,''),('logs',1,'x'*4097)]:
            with self.assertRaises(ContractError):result.page(kind=kind,limit=limit,cursor=cursor)
        result.close()
        with self.assertRaises(ContractError):result.summary()
        fake=RunResults(SimpleNamespace(summary_json=lambda:'{"value":1e999}',close=lambda:None))
        with self.assertRaises(ContractError):fake.summary()
        client=BacktestClient(base_url='http://localhost',token='fixture')
        with self.assertRaises(ContractError):client.result('fixture',limit=True)
        with self.assertRaises(ContractError):client.batch_status('fixture')
    def test_user_record_names_do_not_reinterpret_values_as_account_fields(self):
        def bars(c,d): record(time_ns=0.25, price=Decimal('0.333333333333'), cash='label')
        result=self.run_strategy(SimpleNamespace(handle_data=bars),spec(days=1))
        values=result.page(kind='records',limit=1).items[0]['values']
        self.assertEqual(values['time_ns']['value'],0.25)
        self.assertEqual(values['price']['value'],Decimal('0.333333333333'))
        self.assertEqual(values['cash']['value'],'label')

if __name__=='__main__':unittest.main()
