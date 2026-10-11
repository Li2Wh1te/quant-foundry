"""Installed wheel, real Rust matching/account, bounded Arrow and Python callbacks.

All data, calendar and fees are named synthetic fixtures, not production data.
"""
from copy import deepcopy
from decimal import Decimal
import json
from types import SimpleNamespace
import unittest
from quantfoundry import ContractError
from quantfoundry.api import (
    order, order_target, cancel_order, get_order, get_open_orders, get_trades,
    get_price, get_current_data, record, LimitOrder,
)
from quantfoundry._worker import _run_module
from support_sdk import spec, ipc_spec, MODEL


def quote(event, *, bid='9.99', ask='10.01', bid_quantity=100, ask_quantity=100):
    result = deepcopy(event)
    result['kind'] = 'quote_tick'
    row = result['event']
    row.pop('price'); row.pop('quantity')
    row.update(bid=bid, ask=ask, bid_quantity=bid_quantity, ask_quantity=ask_quantity)
    return result


def report(result):
    return json.loads(result.summary_json())


def known_prior_print(source):
    # D09 does not invent a last trade from a quote. D06's market reservation
    # uses this explicitly supplied, already known synthetic prior raw print.
    prior = str(int(source['calendar'][0]['before_open_ns']) - 1)
    source['initial_marks']['A.SH'] = dict(price='10',price_time_ns=prior,known_ns=prior,
        session='PRIOR',basis='raw',source='synthetic known prior print',
        origin=dict(kind='synthetic',reference=MODEL))


def trades(result):
    return json.loads(result.page_json('trades', None, 1000))['items']


class TickMatchingTests(unittest.TestCase):
    def run_module(self, module, source, **kwargs):
        result = _run_module(module, source, **kwargs)
        self.addCleanup(result.close)
        return result

    def succeeded(self, result):
        summary = report(result)
        self.assertEqual(summary['outcome']['status'], 'succeeded', summary)
        self.assertEqual(summary['cleanup_errors'], [])
        return summary

    def test_quote_query_commands_fills_account_same_ns_and_nonrecursive_notifications(self):
        source = spec(days=1, securities=('A.SH',), frequency='tick', ticks=5)
        source['config']['execution_model'] = 'quote_tick_v1'
        known_prior_print(source)
        raw = source['source']['events']
        raw[0] = quote(raw[0])
        raw[1] = quote(raw[1])
        raw[2]['event'].update(price='10.01', quantity=1000)  # research only
        raw[3] = quote(raw[3], bid='9.90', bid_quantity=30)
        raw[4] = quote(raw[4], bid='9.90', bid_quantity=30, ask_quantity=130)
        seen, fills, orders = [], [], []
        depth, notified = 0, False

        def handle_tick(c, t):
            seen.append((c.now_ns, t.sequence, t.kind))
            self.assertEqual(c.now_ns % 1000, 123)
            self.assertEqual(get_current_data()['A.SH'].sequence, t.sequence)
            frame = get_price('A.SH', count=5, fields=['price','bid','ask','quantity','bid_quantity','ask_quantity','sequence'])
            self.assertEqual(frame['sequence'].tolist(), list(range(1, t.sequence + 1)))
            self.assertTrue(all(int(n) == c.now_ns for n in frame['time_ns']))
            for column in ('price', 'bid', 'ask'):
                self.assertTrue(all(isinstance(v, Decimal) for v in frame[column] if v is not None))
            with self.assertRaises(ContractError) as future:
                get_price('A.SH', count=1, end=c.now_ns + 1, fields=['price'])
            self.assertEqual(future.exception.code, 'LOOKAHEAD_FORBIDDEN')
            if t.sequence == 1:
                self.assertEqual(get_trades(limit=1).items, ())
                for _ in range(2):
                    accepted = order('A.SH', 60)
                    self.assertTrue(accepted.accepted); orders.append(accepted.order_id)
                same = order_target('A.SH', 120)
                self.assertTrue(same.unchanged)  # ordinary pending orders counted
                rejected = order_target('A.SH', 100000)
                self.assertFalse(rejected.accepted)
                self.assertEqual(len(get_open_orders()), 2)
            elif t.sequence == 2:
                self.assertEqual(c.portfolio.positions['A.SH'].quantity, 100)
                self.assertEqual(get_order(orders[1]).filled_quantity, 40)
                self.assertTrue(cancel_order(orders[1]).accepted)
                self.assertTrue(order('A.SH', -30).accepted)
                self.assertEqual(get_trades(limit=1).items[0].quantity, 60)
            elif t.sequence == 3:
                self.assertEqual(c.portfolio.positions['A.SH'].quantity, 100)
            elif t.sequence == 4:
                self.assertEqual(c.portfolio.positions['A.SH'].quantity, 70)
                self.assertTrue(order('A.SH', 30).accepted)
            else:
                self.assertEqual(c.portfolio.positions['A.SH'].quantity, 100)
                self.assertEqual(c.portfolio.cash, Decimal('8991.70'))
                self.assertEqual(get_open_orders(), {})
                record(cash=c.portfolio.cash)

        def on_order(c, o):
            nonlocal depth, notified
            depth += 1
            self.assertEqual(depth, 1)
            if not notified:
                notified = True
                cancelled = order('A.SH', 1, style=LimitOrder('9'))
                self.assertTrue(cancelled.accepted)
                self.assertTrue(cancel_order(cancelled.order_id).accepted)
                self.assertEqual(get_order(cancelled.order_id).status, 'cancelled')
            depth -= 1

        def on_trade(c, t):
            self.assertEqual(depth, 0)
            self.assertEqual(t.execution_time_ns, c.now_ns)
            self.assertIsInstance(t.price, Decimal)
            self.assertIsInstance(t.fee, Decimal)
            fills.append((t.side,t.quantity,t.price,t.fee))

        # One-row and prefetched whole-fixture Arrow batches must be identical.
        for batch_rows in (1, 5):
            seen.clear(); fills.clear(); orders.clear(); notified = False
            source['chunk_events'] = batch_rows
            with ipc_spec(source, batch_rows=batch_rows) as (wired, gateway, routes):
                result = self.run_module(SimpleNamespace(handle_tick=handle_tick,on_order=on_order,on_trade=on_trade), wired, market_routes=routes)
                summary = self.succeeded(result)
                self.assertEqual(summary['progress']['completed_events'], 5)
                self.assertLessEqual(summary['merge']['peak_buffered_events'], batch_rows)
                self.assertLessEqual(gateway.max_batch, batch_rows)
                self.assertEqual(summary['portfolio']['positions']['A.SH']['quantity'], 100)
                self.assertEqual(Decimal(summary['portfolio']['cash']), Decimal('8991.70'))
                self.assertEqual(len(trades(result)), 4)
            self.assertEqual([seq for _,seq,_ in seen], [1,2,3,4,5])
            self.assertEqual(fills, [
                ('buy',60,Decimal('10.01'),Decimal('1')),
                ('buy',40,Decimal('10.01'),Decimal('1')),
                ('sell',30,Decimal('9.90'),Decimal('1')),
                ('buy',30,Decimal('10.01'),Decimal('1')),
            ])

    def test_merged_ticks_preserve_callbacks_single_basis_and_repeated_print_values(self):
        for model, expected in [('trade_tick_v1', [60,60]), ('quote_tick_v1', [100])]:
            source = spec(days=1, securities=('A.SH',), frequency='tick', ticks=5)
            source['config']['execution_model'] = model
            known_prior_print(source)
            for i, event in enumerate(source['source']['events']):
                if i % 2 == 0:
                    source['source']['events'][i] = quote(event, ask='10')
                else:
                    event['event'].update(price='10',quantity=60)
            seen = []
            def handle_tick(c, t):
                seen.append(t.sequence)
                if t.sequence == 1:
                    self.assertTrue(order('A.SH', 300).accepted)
            with ipc_spec(source, batch_rows=2) as (wired, gateway, routes):
                result = self.run_module(SimpleNamespace(handle_tick=handle_tick), wired, market_routes=routes)
                self.succeeded(result)
                self.assertEqual([t['quantity'] for t in trades(result)], expected)
            self.assertEqual(seen, [1,2,3,4,5])

    def test_unknown_side_and_zero_size_never_borrow_prefetched_quote(self):
        source = spec(days=1,securities=('A.SH',),frequency='tick',ticks=6)
        source['config']['execution_model'] = 'quote_tick_v1'
        known_prior_print(source)
        for i, size in enumerate((None,None,0,100,None,100)):
            source['source']['events'][i] = quote(source['source']['events'][i],ask='10',ask_quantity=size)
        quantities = []
        def handle_tick(c,t):
            if t.sequence == 1: self.assertTrue(order('A.SH',200).accepted)
            quantities.append(c.portfolio.positions.get('A.SH').quantity if 'A.SH' in c.portfolio.positions else 0)
        with ipc_spec(source,batch_rows=6) as (wired,gateway,routes):
            result=self.run_module(SimpleNamespace(handle_tick=handle_tick),wired,market_routes=routes)
            self.succeeded(result)
            self.assertEqual(quantities,[0,0,0,100,100,100])
            self.assertEqual([t['quantity'] for t in trades(result)],[100])

    def test_tick_bounded_results_and_batches_with_every_python_callback(self):
        source=spec(days=1,securities=('A.SH',),frequency='tick',ticks=2048)
        source['config']['execution_model']='quote_tick_v1';source['chunk_events']=17
        for i,event in enumerate(source['source']['events']):
            source['source']['events'][i]=quote(event)
        count=0
        def handle_tick(c,t):
            nonlocal count
            count+=1
            if count==2048: record(callbacks=count)
        with ipc_spec(source,batch_rows=17) as (wired,gateway,routes):
            result=self.run_module(SimpleNamespace(handle_tick=handle_tick),wired,market_routes=routes)
            summary=self.succeeded(result)
            self.assertEqual(count,2048)
            self.assertEqual(summary['progress']['completed_events'],2048)
            self.assertLessEqual(summary['merge']['peak_buffered_events'],17)
            self.assertEqual(trades(result),[])
            self.assertLess(summary['results']['result_bytes'],100_000)
