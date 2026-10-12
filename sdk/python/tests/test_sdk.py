"""Installed wheel -> Rust Engine -> Python -> Rust orders/account regressions."""
import ast
from dataclasses import FrozenInstanceError
from decimal import Decimal, localcontext
import inspect
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from quantfoundry import _native, ContractError
from quantfoundry.api import *
from quantfoundry.context import _binding
from quantfoundry.data import _ACTIVE as READ_ACTIVE
from quantfoundry._worker import _run_module
from support_sdk import spec, ipc_spec, MIDNIGHT, MINUTE

ROOT = Path(__file__).resolve().parents[3]

def summary(result): return json.loads(result.summary_json())
def page(result, kind, limit=1000, after=None): return json.loads(result.page_json(kind, after, limit))

class SDKTests(unittest.TestCase):
    def run_strategy(self, module, source=None, **kwargs):
        result = _run_module(module, source or spec(), **kwargs)
        self.addCleanup(result.close)
        return result
    def assertSucceeded(self, result):
        report = summary(result)
        self.assertEqual(report['outcome']['status'],'succeeded',report)
        self.assertEqual(report['cleanup_errors'],[])
        return report

    def test_query_metadata_preserves_json_and_decimal_through_pandas_copies(self):
        from copy import deepcopy
        seen=[]
        def bars(c,data):
            frame=get_price(['A.SH','B.SH'],count=2,fields=['close'])
            meta=frame.attrs['qf']
            before=json.dumps(meta,sort_keys=True)
            copied=frame.copy(deep=True).sort_values(['security','time_ns'])
            grouped=copied.groupby('security',sort=True)['close'].agg(['size','first','last'])
            self.assertEqual(json.dumps(copied.attrs['qf'],sort_keys=True),before)
            self.assertEqual(meta['actual_scope']['securities'],['A.SH','B.SH'])
            self.assertEqual(meta['requested_scope']['count_per_security'],2)
            self.assertEqual(meta['short_window'],{'A.SH':1,'B.SH':1} if not seen else {})
            self.assertTrue(all(isinstance(value,Decimal) for value in copied['close']))
            self.assertEqual(set(grouped['last']),{Decimal('10')})
            self.assertIs(deepcopy(meta),meta)
            for mutate in (lambda:meta.update(extra='future'),
                           lambda:meta['actual_scope'].__setitem__('end_ns','future'),
                           lambda:meta['actual_scope']['securities'].append('future'),
                           lambda:meta.__init__({'future':True}),
                           lambda:meta['limitations'].clear()):
                with self.assertRaises(TypeError):mutate()
            self.assertEqual(json.dumps(meta,sort_keys=True),before)
            copied.attrs['qf']={'user_annotation':'local only'}
            self.assertEqual(json.dumps(frame.attrs['qf'],sort_keys=True),before)
            seen.append(frame)
        with ipc_spec(spec(days=2)) as (wired,gateway,routes):
            self.assertSucceeded(self.run_strategy(SimpleNamespace(handle_data=bars),wired,market_routes=routes))
        self.assertEqual(len(seen),2)
        self.assertNotEqual(seen[0].attrs['qf']['actual_scope']['end_ns'],seen[1].attrs['qf']['actual_scope']['end_ns'])

    def test_host_window_is_separate_from_arrow_packets_and_stays_within_run_grant(self):
        from quantfoundry._worker import _Host
        packet = spec(days=1)
        packet.update(data_budget_bytes=1024*1024*1024, data_max_rows=110000)
        host = _Host(SimpleNamespace(), packet, {}, {})
        self.assertEqual(host.packet_bytes, 64*1024*1024)
        self.assertEqual(host.session.max_rows, 110000)
        self.assertGreater(host.session.max_bytes, 512*1024*1024)
        self.assertLessEqual(host.session.max_bytes, _native.MAX_VIEW_BYTES)
        self.assertLessEqual(host.session.required_data_budget_bytes + 3*host.packet_bytes,
                             packet['data_budget_bytes'])
        # An invalid small grant must fail admission before strategy execution.
        packet['data_budget_bytes'] = 1024*1024
        with self.assertRaises(ContractError) as invalid:
            _Host(SimpleNamespace(), packet, {}, {})
        self.assertEqual(invalid.exception.code, 'RESOURCE_LIMIT')

    def test_rolling_window_matches_full_queries_and_cannot_be_poisoned_by_user_edits(self):
        source=spec(days=7)
        # Distinct prices, a missing B bar, and legitimate empty warmup make
        # retaining stale rows or count-slicing the wrong symbol observable.
        for event in source['source']['events']:
            day=int(event['event']['session'][1:])
            event['event']['close']=str(10+day+(1 if event['event']['security']=='B.SH' else 0))
            event['event']['high']=str(int(event['event']['close'])+1)
        source['source']['events']=[e for e in source['source']['events']
            if not (e['event']['session']=='S3' and e['event']['security']=='B.SH')]
        outputs=[]; requests=[]; counts=[]
        for rolling in (False,True):
            frames=[]
            def bars(c,data):
                session=READ_ACTIVE.get().session
                if not rolling:
                    session.window,session.window_size=None,0
                frame=get_price(['B.SH','A.SH'],count=3,fields=['close'])
                frames.append((frame.to_dict('records'),json.dumps(frame.attrs['qf'],sort_keys=True)))
                if len(frames)==1:
                    # Copies of materialized, exact prices are editable. The
                    # next query must still read the engine's original values.
                    frame.loc[:,'close']=Decimal('999')
                    frame.loc[:,'time_ns']=0
                    frame.attrs['qf']={'user':'edited'}
                    before=gateway.calls['open_stream']
                    reread=get_price(['B.SH','A.SH'],count=3,fields=['close'])
                    self.assertEqual(reread.to_dict('records'),frames[-1][0])
                    self.assertEqual(json.dumps(reread.attrs['qf'],sort_keys=True),frames[-1][1])
                    self.assertEqual(gateway.calls['open_stream'],before)
                if len(frames)==4:
                    # Another request cannot cause the shared cache grant to
                    # count the retained rolling frame twice or grow unbounded.
                    get_price('A.SH',count=1)
                self.assertLessEqual(session.cache_size+session.window_size,session.cache_bytes)
                if len(frames)<7:
                    self.assertTrue(order_target('A.SH',2).accepted)
            with ipc_spec(source) as (wired,gateway,routes):
                result=self.run_strategy(SimpleNamespace(handle_data=bars),wired,market_routes=routes)
                report=self.assertSucceeded(result)
                outputs.append((frames,page(result,'trades')['items'],report['results']['performance']))
                requests.append(gateway.requests)
                counts.append(gateway.calls['check'])
        self.assertEqual(outputs[0],outputs[1])
        self.assertTrue(any(q['count_per_security'] is None and q['start_ns'] is not None
            and int(q['end_ns'])>int(q['start_ns']) for q in requests[1]))
        self.assertTrue(any(q['count_per_security']==3 for q in requests[0]))
        self.assertGreaterEqual(counts[1],counts[0])

    def test_rolling_delta_over_budget_closes_stream_and_retries_original_count(self):
        source=spec(days=1,frequency='1m',securities=('A.SH',))
        # Many distinct completed bars become visible between two actual
        # callbacks. Count=1 is valid even though the optional delta is larger
        # than its credit; the fallback must not make the run fail.
        from copy import deepcopy
        first=deepcopy(source['source']['events'][0])
        last=deepcopy(source['source']['events'][1])
        first_ns=int(first['event']['interval_end_ns'])
        last_ns=int(last['event']['interval_end_ns'])
        hidden=[]
        for index in range(100):
            row=deepcopy(first)
            at=first_ns+1+index
            row['event'].update(interval_start_ns=str(at-1),interval_end_ns=str(at),close='12')
            row['event']['identity'].update(sequence=str(1000+index),stable_input_sequence=str(1000+index))
            hidden.append(row)
        seen=[]
        def bars(c,data):
            session=READ_ACTIVE.get().session
            # A smaller existing cache credit is enough for one retained row.
            session.cache_bytes=40000
            frame=get_price('A.SH',count=1)
            seen.append(frame.to_dict('records'))
        source['source']['events']=[first,last]
        with ipc_spec(source,batch_rows=1) as (wired,gateway,routes):
            # Engine stream stays two bars; history adds intervening records
            # only after its initial source open has selected the two events.
            original=bars
            def callback(c,data):
                if not seen:gateway.events.extend(hidden)
                original(c,data)
            report=self.assertSucceeded(self.run_strategy(SimpleNamespace(handle_data=callback),wired,market_routes=routes))
            self.assertEqual(report['progress']['completed_events'],2)
            self.assertGreaterEqual(gateway.calls['close_stream'],1)
            self.assertEqual(seen[-1],[dict(security='A.SH',time_ns=last_ns,close=Decimal('10'))])
            self.assertTrue(any(q['count_per_security']==1 and q['end_ns']==str(last_ns-1) for q in gateway.requests))

    def test_optional_delta_rejects_bad_header_before_budget_fallback(self):
        from quantfoundry.data import _DataSession
        for bad in (dict(rows=True,schema_id='qf.market.v1'),
                    dict(rows=10001,schema_id='qf.market.v1'),
                    dict(rows=1,schema_id='unknown')):
            with self.subTest(bad=bad):
                calls=[]
                def call(op,body):
                    calls.append(op)
                    return SimpleNamespace(body={'stream_id':'test'} if op=='open_stream' else bad,
                        status='batch',payload=b'x'*2048)
                session=_DataSession(SimpleNamespace(call=call,channel=SimpleNamespace(closed=False,arrow_budget=4096)),
                    universe=('A.SH',),frequency='1d',market_routes={},max_rows=10,
                    max_bytes=4096,cache_bytes=1024)
                view=_native.ReadView(json.dumps(dict(now_ns='123',market_through=None)),10,4096)
                with self.assertRaises(ContractError) as caught:
                    session.stream('test',dict(fields=['close']),view,bounded_delta=True)
                self.assertEqual(caught.exception.code,'INVALID_CONTRACT')
                self.assertEqual(calls,['open_stream','next','close_stream'])
                view.expire()

    def test_trusted_progress_is_live_bounded_and_finishes_with_actual_counters(self):
        progress=[]; callbacks=[]
        started=time.monotonic()
        def bars(c, data):
            callbacks.append(c.now_ns)
            threading.Event().wait(.55)
        result=self.run_strategy(SimpleNamespace(handle_data=bars),spec(days=4),
            _progress=lambda value:progress.append(json.loads(value)))
        elapsed=time.monotonic()-started
        report=self.assertSucceeded(result)
        self.assertEqual(len(callbacks),4)
        self.assertEqual(report['progress']['completed_events'],8)
        self.assertEqual(progress[-1],report['progress'])
        self.assertTrue(any(0<p['completed_events']<8 for p in progress),progress)
        self.assertLessEqual(len(progress),int(elapsed)+3)
        self.assertEqual(report['timing']['trusted_progress_messages'],len(progress))
        self.assertTrue(all(isinstance(p['now_ns'],str) for p in progress))
        self.assertEqual([p['completed_events'] for p in progress],
                         sorted(p['completed_events'] for p in progress))

    def test_trusted_progress_channel_failure_cannot_publish_success(self):
        def broken(value):
            if json.loads(value)['completed_events']:
                raise RuntimeError('private progress channel lost')
        result=self.run_strategy(SimpleNamespace(handle_data=lambda c,d:None),
                                 spec(days=1),_progress=broken)
        report=summary(result)
        self.assertEqual(report['outcome']['status'],'failed',report)
        self.assertEqual(report['outcome']['error']['code'],'STRATEGY_ERROR')
        self.assertEqual(report['outcome']['error']['scope']['function'],'broken')
        self.assertTrue(report['outcome']['partial'])
        self.assertTrue(report['results']['partial'])
        self.assertEqual(report['results']['outcome']['status'],'failed')

    def test_streamed_session_facts_cover_more_than_one_control_page(self):
        source=spec(days=2,securities=tuple(f'S{i:04}.SH' for i in range(257)))
        seen=[]
        with ipc_spec(source,batch_rows=257) as (wired,gateway,routes):
            wired.update(facts=[],stream_session_facts=True)
            result=self.run_strategy(SimpleNamespace(handle_data=lambda c,d:seen.append(len(d.securities))),wired,market_routes=routes)
            report=self.assertSucceeded(result)
            self.assertEqual(report['progress']['completed_events'],514)
            self.assertEqual(gateway.calls['session_facts'],4)
            self.assertEqual(seen,[257,257])

    def test_last_after_close_day_order_uses_admitted_next_session(self):
        submitted=[]
        source=spec(days=1,securities=('A.SH',))
        accepted=spec(days=2,securities=('A.SH',))['calendar']
        source['calendar']=accepted[:1]
        source['following_session']=accepted[1]
        source['next_session']='S2'
        def after_close(c):
            result=order_target('A.SH',10)
            self.assertTrue(result.accepted,result)
            submitted.append(get_order(result.order_id))
        result=self.run_strategy(SimpleNamespace(after_trading_end=after_close),source)
        report=self.assertSucceeded(result)
        self.assertEqual(submitted[0].effective_session,'S2')
        self.assertEqual(submitted[0].filled_quantity,0)
        self.assertEqual(report['progress']['completed_events'],1)
        self.assertEqual(page(result,'trades')['items'],[])

    def test_bad_streamed_session_facts_never_publish_success(self):
        from copy import deepcopy
        for fault in ('duplicate','missing','future_date','universe','offset'):
            with self.subTest(fault=fault):
                source=spec(days=2,securities=('A.SH','B.SH'))
                def malformed(page):
                    page=deepcopy(page)
                    if fault=='duplicate':page['facts'][1]=deepcopy(page['facts'][0])
                    elif fault=='missing':page['facts'].pop()
                    elif fault=='future_date':page['facts'][0]['day']['date']='2099-01-01'
                    elif fault=='universe':page['facts'][0]['instrument']['security']='UNKNOWN.SH'
                    else:page['next_offset']=99
                    return page
                with ipc_spec(source,facts_transform=malformed) as (wired,gateway,routes):
                    wired.update(facts=[],stream_session_facts=True)
                    result=self.run_strategy(SimpleNamespace(handle_data=lambda c,d:None),wired,market_routes=routes)
                    report=summary(result)
                    self.assertEqual(report['outcome']['status'],'failed',report)
                    self.assertEqual(report['progress']['completed_events'],0)

    def test_callbacks_query_submit_cancel_real_account_and_nonrecursive_notifications(self):
        calls=[]; depth=0; leases=[]; snapshots=[]
        def initialize(c):
            self.assertEqual(c.portfolio.cash,Decimal('10000'))
            c.state.initialized=True
            with self.assertRaises(TypeError): c.params['window']=4
            with self.assertRaises(TypeError): c.params['nested']['weights'][0]=9
            run_daily(lambda c:calls.append(('daily',c.now_ns)))
        def bars(c,data):
            self.assertTrue(c.state.initialized)
            calls.append(('bar',c.now_ns))
            self.assertEqual(data.securities,('A.SH','B.SH'))
            self.assertEqual(get_current_data()['A.SH'].last_price,Decimal('10'))
            frame=get_price('A.SH',count=2)
            self.assertTrue(all(isinstance(p,Decimal) for p in frame['close']))
            self.assertLessEqual(frame['time_ns'].max(),c.now_ns)
            leases.append(READ_ACTIVE.get().view)
            snapshots.append(frame)
            if c.now_ns == first_close:
                submitted=order_target('A.SH',10)
                self.assertTrue(submitted.accepted)
                self.assertEqual(c.portfolio.frozen_cash,Decimal('101'))
                self.assertEqual(get_order(submitted.order_id).quantity,10)
                same=order_target('A.SH',10)
                self.assertTrue(same.unchanged)
                refused=order_target('A.SH',100000)
                self.assertFalse(refused.accepted)
                self.assertEqual(c.portfolio.frozen_cash,Decimal('101'))
                extra=order('B.SH',2)
                self.assertTrue(extra.accepted)
                self.assertTrue(cancel_order(extra.order_id).accepted)
                self.assertEqual(c.portfolio.frozen_cash,Decimal('101'))
                self.assertEqual(len(get_open_orders()),1)
                with self.assertRaises((FrozenInstanceError,AttributeError)): c.portfolio.cash=Decimal(0)
                with self.assertRaises(TypeError): c.portfolio.positions['A.SH']=None
        def on_order(c,order):
            nonlocal depth
            depth+=1; self.assertEqual(depth,1)
            calls.append(('order',order.status))
            if order.security=='A.SH' and order.status=='open':
                cancel_order(order.order_id)
                order_target('A.SH',10)
            depth-=1
        def on_trade(c,trade):
            calls.append(('trade',trade.quantity))
            self.assertEqual(c.portfolio.positions['A.SH'].quantity,10)
            with self.assertRaises((FrozenInstanceError,AttributeError)): c.portfolio.positions['A.SH'].quantity=99
            self.assertEqual(get_trades(limit=1).items[0].trade_id,trade.trade_id)
        source=spec(); first_close=int(source['calendar'][0]['session']['close_ns'])
        # The on_order replacement should happen only once, not create a loop.
        replaced=False
        original=on_order
        def notify(c,o):
            nonlocal replaced
            if o.security=='A.SH' and o.status=='open' and not replaced:
                replaced=True; original(c,o)
            else: calls.append(('order',o.status))
        with ipc_spec(source,batch_rows=2) as (wired,gateway,routes):
            result=self.run_strategy(SimpleNamespace(initialize=initialize,handle_data=bars,on_order=notify,on_trade=on_trade),wired,market_routes=routes)
            report=self.assertSucceeded(result)
            self.assertEqual(report['portfolio']['cash'],'9899.00')
            self.assertEqual(page(result,'positions',limit=1)['items'][0]['quantity'],10)
            self.assertEqual(len([c for c in calls if c[0]=='bar']),3)
            self.assertEqual(len([c for c in calls if c[0]=='daily']),3)
            self.assertEqual(len(page(result,'trades')['items']),1)
            self.assertLessEqual(gateway.max_batch,2)
        for lease in leases:
            with self.assertRaises(ContractError): lease.resources_json()
        self.assertEqual(snapshots[0]['close'].tolist(),[Decimal('10')])

    def test_real_tick_count_same_nanosecond_cursor_no_prefetch_leak_and_subscriptions(self):
        source=spec(days=1,securities=('A.SH',),frequency='tick',ticks=7)
        seen=[]; frames=[]
        def tick(c,t):
            seen.append((c.now_ns,t.sequence))
            self.assertEqual(c.now_ns % 1000,123)
            current=get_current_data()['A.SH']
            self.assertEqual(current.sequence,t.sequence)
            prices=get_price('A.SH',count=1,fields=['price','sequence'])
            self.assertEqual(prices['sequence'].tolist(),[t.sequence])
            self.assertEqual(prices['price'].tolist(),[t.last_price])
            frames.append(READ_ACTIVE.get().view)
            self.assertEqual(c.current_dt.tzinfo.key,'Asia/Shanghai')
        with ipc_spec(source,batch_rows=7) as (wired,gateway,routes):
            result=self.run_strategy(SimpleNamespace(handle_tick=tick),wired,market_routes=routes)
            report=self.assertSucceeded(result)
            self.assertEqual(report['progress']['completed_events'],7)
        self.assertEqual([s for _,s in seen],list(range(1,8)))
        for view in frames:
            with self.assertRaises(ContractError): view.current_json('["A.SH"]')
        seen.clear()
        result=self.run_strategy(SimpleNamespace(initialize=lambda c:subscribe(['A.SH'],events='bar'),handle_tick=tick),source)
        self.assertSucceeded(result); self.assertEqual(seen,[])
        source=spec(days=1,securities=('A.SH','B.SH'),frequency='tick',ticks=3)
        subscribed=[]
        result=self.run_strategy(SimpleNamespace(initialize=lambda c:(subscribe(['A.SH'],events='tick'),subscribe(['A.SH'],events='tick')),
            handle_tick=lambda c,t:subscribed.append(t.security)),source)
        self.assertSucceeded(result); self.assertEqual(subscribed,['A.SH']*3)

    def test_tick_commands_cancel_before_matching_and_live_orders_use_d08(self):
        source=spec(days=1,securities=('A.SH',),frequency='tick',ticks=3)
        orders=[]
        def tick(c,t):
            result=order('A.SH',1)
            self.assertTrue(result.accepted); self.assertEqual(get_order(result.order_id).quantity,1)
            self.assertTrue(cancel_order(result.order_id).accepted); orders.append(result.order_id)
        self.assertSucceeded(self.run_strategy(SimpleNamespace(handle_tick=tick),source))
        self.assertEqual(len(orders),3)
        result=self.run_strategy(SimpleNamespace(handle_tick=lambda c,t:order('A.SH',1)),source)
        self.assertSucceeded(result)
        self.assertEqual([t['quantity'] for t in page(result,'trades')['items']],[1,1])

    def test_bar_barrier_and_all_order_routes_decimal_inputs(self):
        source=spec(days=1,frequency='1m'); close=int(source['source']['events'][0]['event']['interval_end_ns'])
        captured=[]
        def bars(c,d):
            if c.now_ns==close:
                for submit in [lambda:order_value('A.SH','20'),lambda:order_target_value('B.SH',Decimal('20')),lambda:order_target_percent('B.SH','0.01')]:
                    result=submit(); self.assertTrue(result.accepted,result); captured.append(result)
                for invalid in (True,0.1,Decimal('NaN')):
                    with self.assertRaises((ContractError,TypeError)): order_value('A.SH',invalid)
                self.assertEqual(get_trades(limit=1).items,())
        result=self.run_strategy(SimpleNamespace(handle_data=bars),source)
        report=self.assertSucceeded(result)
        trades=page(result,'trades')['items']
        self.assertTrue(trades)
        self.assertTrue(all(int(t['execution_time_ns'])>close for t in trades))
        self.assertGreaterEqual(int(page(result,'positions',limit=1)['items'][0]['quantity']),2)

    def test_exception_source_location_expiration_and_cancel_release(self):
        saved=[]
        def broken(c,d):
            saved.extend([c,_binding()[1],READ_ACTIVE.get().view])
            raise ValueError('do not return an arbitrary secret message')
        result=self.run_strategy(SimpleNamespace(handle_data=broken),spec(days=1))
        error=summary(result)['outcome']['error']
        self.assertEqual(error['code'],'STRATEGY_ERROR'); self.assertEqual(error['scope']['function'],'broken')
        self.assertEqual(error['scope']['file'],'test_sdk.py')
        self.assertEqual(int(error['scope']['line']),inspect.getsourcelines(broken)[1]+2)
        self.assertNotIn('secret',json.dumps(error))
        with self.assertRaises(ContractError): saved[0].portfolio
        with self.assertRaises(ContractError): saved[1].request('portfolio','null')
        with self.assertRaises(ContractError): saved[2].resources_json()
        cancelled=_native.CancellationToken(); seen=[]
        def cancel(c,t):
            seen.append(t.sequence); saved.append(READ_ACTIVE.get().view); cancelled.cancel()
        result=self.run_strategy(SimpleNamespace(handle_tick=cancel),spec(days=1,securities=('A.SH',),frequency='tick'),cancellation=cancelled)
        self.assertEqual(summary(result)['outcome']['status'],'cancelled');self.assertEqual(seen,[1])
        with self.assertRaises(ContractError): saved[-1].current_json('[]')

    def test_resource_catching_does_not_turn_failed_run_into_success(self):
        source=spec(days=1,frequency='1m'); source['max_commands_per_boundary']=2
        def bars(c,d):
            for _ in range(4):
                try: order('A.SH',1)
                except ContractError: pass
        result=self.run_strategy(SimpleNamespace(handle_data=bars),source)
        self.assertEqual(summary(result)['outcome']['error']['code'],'RESOURCE_LIMIT')
        source=spec(days=1);source['result_budget_bytes']=1
        result=self.run_strategy(SimpleNamespace(),source)
        self.assertEqual(summary(result)['outcome']['error']['code'],'RESULT_BUDGET_EXCEEDED')
        source=spec(days=1);source['data_budget_bytes']=1
        with self.assertRaises(ContractError): self.run_strategy(SimpleNamespace(),source)

    def test_bounded_trade_pages_foreign_and_future_cursors(self):
        source=spec(days=1,securities=('A.SH',),frequency='1m')
        def bars(c,d): order('A.SH',1)
        result=self.run_strategy(SimpleNamespace(handle_data=bars),source)
        self.assertSucceeded(result)
        first=page(result,'trades',1); self.assertEqual(len(first['items']),1); self.assertIsNotNone(first['next_cursor'])
        second=page(result,'trades',1,first['next_cursor']); self.assertNotEqual(first['items'][0]['trade_id'],second['items'][0]['trade_id'])
        self.assertEqual(summary(result)['results']['max_page_read'],1)
        other=self.run_strategy(SimpleNamespace(handle_data=bars),source)
        self.assertSucceeded(other)
        with self.assertRaises(ContractError): page(other,'trades',1,first['next_cursor'])
        bad=first['next_cursor'].rsplit(':',1)[0]+':99999'
        with self.assertRaises(ContractError): page(result,'trades',1,bad)
        with self.assertRaises(ContractError): page(result,'trades',10001)

    def test_multiple_runs_context_and_registration_do_not_leak(self):
        states=[];old=[]
        def first(c,d): c.state.marker='first';old.append(c);states.append(c.params['window'])
        self.assertSucceeded(self.run_strategy(SimpleNamespace(handle_data=first),spec(days=1)))
        def second(c,d):
            self.assertFalse(hasattr(c.state,'marker'))
            with self.assertRaises(ContractError): old[0].portfolio
            with self.assertRaises(ContractError): subscribe(['A.SH'])
            with self.assertRaises(ContractError): _run_module(SimpleNamespace(),spec(days=1))
        self.assertSucceeded(self.run_strategy(SimpleNamespace(handle_data=second),spec(days=1)))

    def test_data_changed_and_missing_research_mapping_remain_terminal(self):
        source=spec(days=1)
        with ipc_spec(source) as (wired,gateway,routes):
            def bars(c,d):
                with self.assertRaises(ContractError) as caught: get_fundamentals(['A.SH'],fields=['revenue'])
                self.assertEqual(caught.exception.code,'CAPABILITY_UNAVAILABLE')
                get_price('A.SH',count=1)
                self.assertIsNotNone(READ_ACTIVE.get().session.window)
                gateway.changed=True
                try: get_price('A.SH',count=1)
                except ContractError: pass
            result=self.run_strategy(SimpleNamespace(handle_data=bars),wired,market_routes=routes)
            self.assertEqual(summary(result)['outcome']['error']['code'],'DATA_CHANGED')

    def test_nullable_market_projection_and_resampling_keep_unknown_as_none(self):
        from copy import deepcopy
        from support_sdk import batch
        source=spec(days=1,frequency='1m',securities=('A.SH',))
        events=deepcopy(source['source']['events'])
        fifth=deepcopy(events[-1])
        fifth['event']['interval_start_ns']=events[-1]['event']['interval_end_ns']
        fifth['event']['interval_end_ns']=str(int(fifth['event']['interval_start_ns'])+MINUTE)
        fifth['event']['identity'].update(sequence='5',stable_input_sequence='5')
        events.append(fifth)
        events[2]['event'].update(high=None,quantity=None)
        now=int(events[-1]['event']['interval_end_ns'])+1
        view=_native.ReadView(json.dumps(dict(now_ns=str(now),market_through=None)),10,128*1024)
        metadata=dict(schema_id='qf.market.v1',rows=5,time_unit='utc_nanoseconds',
            price_currency='CNY',quantity_unit='shares',actual_scope=dict(
                start_ns=events[0]['event']['interval_end_ns'],end_ns=events[-1]['event']['interval_end_ns'],securities=['A.SH']),limitations=[])
        view.push_market(json.dumps(metadata),batch(events))
        request=dict(securities=['A.SH'],fields=['high','quantity'],frequency='1m',
            start_ns=events[0]['event']['interval_start_ns'],end_ns=str(now),count_per_security=None,adjustment='none')
        rows=json.loads(view.prices_json(json.dumps(request),'1m','[]'))
        self.assertEqual(rows[2]['high'],None)
        self.assertEqual(rows[2]['quantity'],None)
        request.update(frequency='5m',fields=['open','high','low','close','quantity','status'])
        rows=json.loads(view.prices_json(json.dumps(request),'1m',json.dumps(source['calendar'])))
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['status'],'missing')
        self.assertTrue(all(rows[0][field] is None for field in ('open','high','low','close','quantity')))
        view.expire()

    def test_public_executable_signatures_match_contract(self):
        import quantfoundry.api as api
        tree=ast.parse((ROOT/'contracts/strategy_api.pyi').read_text())
        callbacks={'initialize','before_trading_start','after_trading_end','handle_data','handle_tick','on_order','on_trade'}
        for node in tree.body:
            if isinstance(node,ast.FunctionDef) and node.name not in callbacks:
                func=getattr(api,node.name)
                self.assertEqual(list(inspect.signature(func).parameters),[a.arg for a in (*node.args.posonlyargs,*node.args.args,*node.args.kwonlyargs)] + ([node.args.kwarg.arg] if node.args.kwarg else []),node.name)

    def test_query_resource_limit_is_terminal_even_if_strategy_catches_it(self):
        def bars(c,d):
            try: get_price('A.SH',count=10001)
            except ContractError: pass
        result=self.run_strategy(SimpleNamespace(handle_data=bars),spec(days=1))
        self.assertEqual(summary(result)['outcome']['error']['code'],'RESOURCE_LIMIT')

    def test_quote_tick_bid_ask_quantities_and_source_identity(self):
        source=spec(days=1,securities=('A.SH',),frequency='tick',ticks=2)
        source['config']['execution_model']='quote_tick_v1'
        for event in source['source']['events']:
            event['kind']='quote_tick'; row=event['event'];row.pop('price');row.pop('quantity')
            row.update(bid='9.99',ask='10.01',bid_quantity=100,ask_quantity=None)
        seen=[]
        def tick(c,t):
            self.assertEqual(t.kind,'quote');self.assertEqual(t.bid_quantity,100);self.assertIsNone(t.ask_quantity)
            self.assertEqual(get_current_data()['A.SH'].ask,Decimal('10.01'));seen.append(t.sequence)
        self.assertSucceeded(self.run_strategy(SimpleNamespace(handle_tick=tick),source))
        self.assertEqual(seen,[1,2])

    def test_quote_costs_and_equity_use_explicit_current_raw_mark(self):
        from support_sdk import MODEL
        source=spec(days=1,securities=('A.SH',),frequency='tick',ticks=2)
        source['config']['execution_model']='quote_tick_v1'
        for event in source['source']['events']:
            event['kind']='quote_tick'
            row=event['event'];row.pop('price');row.pop('quantity')
            row.update(bid='9.99',ask='10.01',bid_quantity=100,ask_quantity=100)
        stamp=source['calendar'][0]['before_open_ns']
        source['initial_marks']={'A.SH':dict(price='10',price_time_ns=stamp,known_ns=stamp,
            session='S1',basis='raw',source='synthetic accepted session print',
            origin=dict(kind='synthetic',reference=MODEL))}
        def tick(c,t):
            self.assertIsNone(t.last_price)
            if t.sequence==1:
                self.assertTrue(order_target('A.SH',10,style=LimitOrder('10.01')).accepted)
        result=self.run_strategy(SimpleNamespace(handle_tick=tick),source)
        report=self.assertSucceeded(result)
        trades=page(result,'trades')['items']
        self.assertEqual(len(trades),1)
        fee=Decimal(trades[0]['fee']);self.assertGreater(fee,0)
        self.assertEqual(Decimal(report['portfolio']['cash']),Decimal('10000')-Decimal('100.1')-fee)
        self.assertEqual(Decimal(report['portfolio']['total_value']),Decimal('9999.9')-fee)

    def test_record_and_logs_are_bounded_and_keep_exact_types(self):
        def bars(c,d):
            record(exact=Decimal('1.234567890123456789'),shares=3,tag='fixture',statistic=0.25)
            log.warning('中文'*10000)
        result=self.run_strategy(SimpleNamespace(handle_data=bars),spec(days=1))
        self.assertSucceeded(result)
        values=page(result,'records')['items'][0]['values']
        self.assertEqual(values['exact']['value'],'1.234567890123456789')
        self.assertEqual(values['statistic']['kind'],'statistic')
        message=page(result,'logs')['items'][0]
        self.assertTrue(message['truncated']);self.assertLessEqual(len(message['message'].encode()),4096)
        self.assertTrue(summary(result)['logs_truncated'])

    def test_exhausted_logs_are_visibly_truncated_at_exact_budget(self):
        count=0
        def bars(c,d):
            nonlocal count
            count+=1
            for _ in range(128 if count<=2 else 1): log.info('a'*4096)
        result=self.run_strategy(SimpleNamespace(handle_data=bars),spec(days=1,frequency='1m'))
        self.assertSucceeded(result)
        first=page(result,'logs')
        self.assertIsNotNone(first['next_cursor'])
        second=page(result,'logs',after=first['next_cursor'])
        self.assertEqual(len(first['items'])+len(second['items']),256)
        self.assertTrue(summary(result)['logs_truncated'])

    def test_rust_work_releases_gil_without_unsubscribed_tick_callbacks(self):
        started=threading.Event();done=threading.Event(); progress=[]
        def observer():
            started.wait(3)
            while not done.is_set(): progress.append(1);done.wait(.001)
        thread=threading.Thread(target=observer);thread.start()
        try:
            result=self.run_strategy(SimpleNamespace(initialize=lambda c:started.set()),spec(days=1,securities=('A.SH',),frequency='tick',ticks=2000))
            report=self.assertSucceeded(result)
        finally:done.set();started.set();thread.join(3)
        self.assertEqual(report['progress']['completed_events'],2000)
        self.assertEqual(report['callback_count'],1)
        self.assertTrue(progress)

    def test_full_example_imports_only_in_worker_with_old_engine_blocked(self):
        source=spec(days=4)
        with ipc_spec(source,batch_rows=2) as (wired,gateway,routes), tempfile.TemporaryDirectory() as directory:
            strategy=Path(directory)/'full_strategy.py'
            # Installed-wheel subprocess has no repository Python path. Force
            # refusal if any attempted old-core import is introduced later.
            blocker="""import sys, importlib.abc
class RefuseLegacy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith('app.backtesting'):
            raise AssertionError('LEGACY_ENGINE_RETIRED')
sys.meta_path.insert(0, RefuseLegacy())
"""
            strategy.write_text(blocker+(ROOT/'sdk/python/examples/complete_strategy.py').read_text())
            packet=dict(spec=wired,market_routes={key:dict(binding=value.binding,fields=value.fields) for key,value in routes.items()})
            process=subprocess.run([sys.executable,'-I','-m','quantfoundry._worker','--strategy',str(strategy),'--isolated-test'],
                input=json.dumps(packet),text=True,capture_output=True,timeout=20,cwd=directory)
            self.assertEqual(process.returncode,0,process.stdout+process.stderr)
            report=json.loads(process.stdout)
            self.assertEqual(report['portfolio']['cash'],'9897.00')
            self.assertEqual(report['portfolio']['total_value'],'9997.00')
            self.assertEqual(report['results']['counts']['trades'],3)
            self.assertFalse(report['production_executed'])
            self.assertEqual(gateway.streams,{})

    def test_worker_import_failure_has_file_function_line_without_absolute_path(self):
        with tempfile.TemporaryDirectory() as directory:
            strategy=Path(directory)/'broken_strategy.py'
            strategy.write_text('raise ValueError("private source message")\n')
            process=subprocess.run([sys.executable,'-I','-m','quantfoundry._worker','--strategy',str(strategy),'--isolated-test'],
                input=json.dumps({'spec':spec(days=1)}),text=True,capture_output=True,timeout=10,cwd=directory)
            self.assertEqual(process.returncode,1)
            error=json.loads(process.stdout)['outcome']['error']
            self.assertEqual(error['code'],'STRATEGY_ERROR');self.assertEqual(error['scope']['file'],'broken_strategy.py')
            self.assertEqual(error['scope']['line'],'1');self.assertNotIn(directory,json.dumps(error))

    def test_worker_import_keyboard_interrupt_is_cancelled(self):
        with tempfile.TemporaryDirectory() as directory:
            strategy=Path(directory)/'cancel_strategy.py';strategy.write_text('raise KeyboardInterrupt\n')
            process=subprocess.run([sys.executable,'-I','-m','quantfoundry._worker','--strategy',str(strategy),'--isolated-test'],
                input=json.dumps({'spec':spec(days=1)}),text=True,capture_output=True,timeout=10,cwd=directory)
            self.assertEqual(process.returncode,1)
            self.assertEqual(json.loads(process.stdout)['outcome'],{'status':'cancelled','partial':False})

    def test_native_source_closes_when_callback_raises_after_arrow_query(self):
        source=spec(days=1)
        with ipc_spec(source,batch_rows=1) as (wired,gateway,routes):
            saved=[]
            def broken(c,d):
                saved.append(get_price('A.SH',count=1))
                saved.append(READ_ACTIVE.get().view)
                raise RuntimeError('fixture')
            result=self.run_strategy(SimpleNamespace(handle_data=broken),wired,market_routes=routes)
            self.assertEqual(summary(result)['outcome']['status'],'failed')
            self.assertEqual(saved[0]['close'].tolist(),[Decimal('10')])
            with self.assertRaises(ContractError):saved[1].current_json('["A.SH"]')
        self.assertEqual(gateway.streams,{})

    def test_ipc_wait_obeys_worker_wall_deadline_and_cancellation(self):
        import time
        source=spec(days=1);source['wall_time_seconds']=1
        with ipc_spec(source,delay_seconds=2) as (wired,gateway,routes):
            started=time.monotonic()
            result=self.run_strategy(SimpleNamespace(),wired,market_routes=routes)
            elapsed=time.monotonic()-started
            self.assertEqual(summary(result)['outcome']['error']['code'],'RESOURCE_LIMIT')
            self.assertLess(elapsed,1.5)
        source['wall_time_seconds']=30
        token=_native.CancellationToken()
        with ipc_spec(source,delay_seconds=2) as (wired,gateway,routes):
            timer=threading.Timer(.1,token.cancel);timer.start()
            try:
                started=time.monotonic()
                result=self.run_strategy(SimpleNamespace(),wired,market_routes=routes,cancellation=token)
                self.assertEqual(summary(result)['outcome']['status'],'cancelled')
                self.assertLess(time.monotonic()-started,1)
            finally: timer.join()
        token=_native.CancellationToken();token.cancel()
        with ipc_spec(source) as (wired,gateway,routes):
            result=self.run_strategy(SimpleNamespace(),wired,market_routes=routes,cancellation=token)
            self.assertEqual(summary(result)['outcome'],{'status':'cancelled','partial':False})

    def test_syntax_error_reports_strategy_file_and_line(self):
        try: compile('def broken(:\n','/private/user_strategy.py','exec')
        except SyntaxError as exception:
            error=json.loads(_native.strategy_error_json(exception))
        self.assertEqual(error['scope']['file'],'user_strategy.py')
        self.assertEqual(error['scope']['line'],'1')
        self.assertNotIn('/private',json.dumps(error))

    def test_socket_backlog_connection_is_deadline_and_cancel_bounded(self):
        import socket,time
        with tempfile.TemporaryDirectory() as directory:
            listener=socket.socket(socket.AF_UNIX);endpoint=str(Path(directory)/'blocked.sock')
            listener.bind(endpoint);listener.listen(0)
            dummy=socket.socket(socket.AF_UNIX);dummy.connect(endpoint)
            self.addCleanup(listener.close);self.addCleanup(dummy.close)
            source=spec(days=1);source['wall_time_seconds']=1
            source['source']=dict(kind='ipc',socket_path=endpoint,binding='market',request=dict(
                securities=['A.SH','B.SH'],fields=['open','high','low','close','quantity'],frequency='1d',
                start_ns=source['calendar'][0]['before_open_ns'],end_ns=source['calendar'][0]['after_close_ns'],
                count_per_security=None,adjustment='none'))
            started=time.monotonic()
            result=self.run_strategy(SimpleNamespace(),source)
            self.assertEqual(summary(result)['outcome']['error']['code'],'RESOURCE_LIMIT')
            self.assertLess(time.monotonic()-started,1.5)
            source['wall_time_seconds']=30;token=_native.CancellationToken()
            timer=threading.Timer(.1,token.cancel);timer.start()
            try:
                started=time.monotonic()
                result=self.run_strategy(SimpleNamespace(),source,cancellation=token)
                self.assertEqual(summary(result)['outcome']['status'],'cancelled')
                self.assertLess(time.monotonic()-started,1)
            finally: timer.join()

    def test_unknown_quote_is_stale_in_both_callback_and_query(self):
        source=spec(days=1,securities=('A.SH',),frequency='tick',ticks=1)
        source['config']['execution_model']='quote_tick_v1'
        event=source['source']['events'][0];event['kind']='quote_tick'
        event['event'].pop('price');event['event'].pop('quantity')
        event['event'].update(bid=None,ask=None,bid_quantity=None,ask_quantity=None)
        seen=[]
        def tick(c,t):
            self.assertTrue(t.is_stale);self.assertIsNone(t.price_time_ns)
            self.assertTrue(get_current_data()['A.SH'].is_stale);seen.append(t)
        self.assertSucceeded(self.run_strategy(SimpleNamespace(handle_tick=tick),source))
        self.assertEqual(len(seen),1)

    def test_d09_dividend_registration_ex_and_close_payment_use_real_account(self):
        source=spec(days=2,securities=('A.SH',));first,second=source['calendar']
        from support_sdk import MODEL
        origin=dict(kind='synthetic',reference=MODEL)
        source['corporate_actions']=[dict(action_id='synthetic-dividend',security='A.SH',public_ns=first['before_open_ns'],
            record=dict(session='S1',phase='session_close'),ex=dict(session='S2',phase='before_open'),sequence=1,entitlement='all_held',
            kind=dict(kind='cash_dividend',per_share='0.2',payment=dict(session='S2',phase='session_close'),
                tax=dict(kind='synthetic_flat',rate='0'),settlement=dict(scale=2,rounding='half_even')),
            ex_raw_mark=dict(price='9.8',price_time_ns=second['before_open_ns'],known_ns=second['before_open_ns'],session='S2',
                basis='raw',source='synthetic D09 raw ex-price oracle',origin=origin),origin=origin)]
        for event in source['source']['events'][1:]:
            event['event'].update(open='9.8',high='10.8',low='8.8',close='9.8')
        observed=[]
        def before(c):
            if c.now_ns==int(first['before_open_ns']):
                self.assertTrue(order('A.SH',10,style=LimitOrder('10')).accepted)
            else:
                self.assertEqual(c.portfolio.receivables,Decimal('2'))
                self.assertEqual(c.portfolio.positions['A.SH'].market_value,Decimal('98'))
        def after(c):
            observed.append((c.portfolio.cash,c.portfolio.receivables,c.portfolio.total_value))
        result=self.run_strategy(SimpleNamespace(before_trading_start=before,after_trading_end=after),source)
        self.assertSucceeded(result)
        self.assertEqual(observed,[(Decimal('9899'),Decimal('0'),Decimal('9999')),(Decimal('9901'),Decimal('0'),Decimal('9999'))])
        self.assertEqual(len(page(result,'trades')['items']),1)

    def test_daily_weekly_monthly_schedules_use_calendar_and_registration_order(self):
        source=spec(days=3);seen=[]
        def initialize(c):
            run_daily(lambda c:seen.append(('first',c.now_ns)))
            run_daily(lambda c:seen.append(('second',c.now_ns)))
            run_weekly(lambda c:seen.append(('weekly',c.now_ns)),trading_day=2,time='before_open')
            run_monthly(lambda c:seen.append(('monthly',c.now_ns)),trading_day=-1)
        result=self.run_strategy(SimpleNamespace(initialize=initialize),source)
        self.assertSucceeded(result)
        closes=[int(s['after_close_ns']) for s in source['calendar']]
        self.assertEqual(seen,[('first',closes[0]),('second',closes[0]),
            ('weekly',int(source['calendar'][1]['before_open_ns'])),('first',closes[1]),('second',closes[1]),
            ('first',closes[2]),('second',closes[2]),('monthly',closes[2])])
        def invalid(c): run_daily(lambda c:None,time='10:15')
        result=self.run_strategy(SimpleNamespace(initialize=invalid),source)
        self.assertEqual(summary(result)['outcome']['error']['code'],'CAPABILITY_UNAVAILABLE')

    def test_result_files_are_private_even_with_permissive_umask_and_drop_releases(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            program="""import json,os,stat,sys
from pathlib import Path
from types import SimpleNamespace
from quantfoundry._worker import _run_module
os.umask(0o022)
result=_run_module(SimpleNamespace(),json.load(sys.stdin))
roots=list(Path(os.environ['TMPDIR']).glob('qf-results-*'))
assert len(roots)==1
assert stat.S_IMODE(roots[0].stat().st_mode)==0o700
files=list(roots[0].iterdir())
assert files and all(stat.S_IMODE(path.stat().st_mode)==0o600 for path in files)
del result
assert not list(Path(os.environ['TMPDIR']).glob('qf-results-*'))
print('private result owner released')
"""
            process=subprocess.run([sys.executable,'-I','-c',program],input=json.dumps(spec(days=1)),
                text=True,capture_output=True,timeout=10,env={**os.environ,'TMPDIR':directory})
            self.assertEqual(process.returncode,0,process.stdout+process.stderr)
