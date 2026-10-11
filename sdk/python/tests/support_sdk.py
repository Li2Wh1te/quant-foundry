"""Explicit isolated market/rule fixtures. No Python order/account simulator."""
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
import json
import socket
import tempfile
import threading
import pyarrow as pa
from quantfoundry._transport import Channel, Frame, TransportError
from quantfoundry.data import _MarketRoute

MODEL = 'synthetic_d10_callback_oracles'
MINUTE = 60_000_000_000
MIDNIGHT = 1767196800000000000

def spec(*, days=3, securities=('A.SH','B.SH'), frequency='1d', ticks=6, run_id='isolated-d10'):
    calendar, facts, events = [], [], []
    origin = dict(kind='synthetic', reference=MODEL)
    rule = dict(exchange='shanghai',product='main_board_stock',effective={'from':'2000-01-01','through':'2030-01-01'},
        origin=origin,session_template='china_auction',price_tick='0.01',minimum_buy=1,buy_step=1,minimum_sell=1,sell_step=1,
        maximum_limit=1000000,maximum_market=1000000,sell_availability='same_session',normal_limit_rate='0.1',risk_limit_rate='0.1',
        ipo_no_limit_sessions=0,relisting_no_limit=False,risk_rules_verified=True,risk_buy_cap=None,risk_requires_limit_order=False,
        continuous_cage=None,no_limit_call_policy='shanghai_main_board',market_requires_daily_limit=False,band_rounding='half_up')
    fees = dict(commission_rate='0',minimum_commission='1',currency='CNY',settlement_scale=2,rounding='half_even',components=[],synthetic_model=MODEL)
    for day in range(days):
        midnight = MIDNIGHT + day*1440*MINUTE
        stamp = (date(2026,1,1)+timedelta(days=day)).isoformat()
        key = f'S{day+1}'
        windows = lambda ranges:[dict(start_ns=str(midnight+a*MINUTE),end_ns=str(midnight+b*MINUTE)) for a,b in ranges]
        row = dict(session=dict(key=key,exchange_timezone='Asia/Shanghai',open_ns=str(midnight+555*MINUTE),close_ns=str(midnight+900*MINUTE)),
            date=stamp,local_midnight_ns=str(midnight),before_open_ns=str(midnight+550*MINUTE),after_close_ns=str(midnight+901*MINUTE),
            market_windows=windows([(555,565),(570,690),(780,900)]),bar_windows=windows([(570,690),(780,900)]),
            week=dict(id='synthetic-week',trading_day=day+1,total_trading_days=days),
            month=dict(id='2026-01',trading_day=day+1,total_trading_days=days))
        calendar.append(row)
        for security in securities:
            instrument = dict(security=security,exchange='shanghai',product='main_board_stock')
            day_facts = dict(security=security,date=stamp,session=key,
                listing=dict(first_trading_date='2000-01-01',last_trading_date=None,verified_through='2030-01-01',kind='ipo',session_ordinal=1000+day),
                status='trading',risk='normal',price_limit=dict(kind='limited',rate='0.1',lower='1',upper='1000'),
                fund_price_band=None,underlying_same_session=None,delisting_session_ordinal=None)
            facts.append(dict(instrument=instrument,day=day_facts,permissions=dict(buy=True,sell=True,product_buy=True,risk_disclosure=True,delisting_buy=True,risk_bought_or_open=0),
                submission_reference=None,buy_fees=fees,sell_fees=fees,investor='resident_individual',origin=origin,
                sale_proceeds='immediate',sale_proceeds_basis='synthetic explicit cash timing',disposal_settlement_date=stamp,holding_bands={}))
            identity = lambda seq:dict(source_session=key,channel='synthetic-d10-market',sequence=str(seq),stable_input_sequence=str(seq))
            base = dict(security=security,session=key,units=dict(price_currency='CNY',quantity_unit='shares'))
            if frequency == 'tick':
                for i in range(ticks):
                    # Long callback fixtures remain within their declared
                    # price band; repeated values still have distinct identities.
                    events.append(dict(kind='trade_tick',event=dict(**base,time_ns=str(midnight+570*MINUTE+123),price=f'{10+i%90}.01',quantity=1000,identity=identity(i+1))))
            else:
                count = 1 if frequency=='1d' else 4
                for i in range(count):
                    start = midnight+570*MINUTE if frequency=='1d' else midnight+(570+i)*MINUTE
                    end = midnight+900*MINUTE if frequency=='1d' else start+MINUTE
                    events.append(dict(kind='bar',event=dict(**base,interval_start_ns=str(start),interval_end_ns=str(end),
                        open='10',high='11',low='9',close='10',quantity=1000,identity=identity(i+1))))
    events.sort(key=event_key)
    config = dict(api_schema='qf.backtest.v2',strategy_revision_id='synthetic-strategy',account_id='synthetic-account',initial_cash='10000',
        start=calendar[0]['date'],end=calendar[-1]['date'],frequency=frequency,universe=list(securities),parameters={'window':2,'nested':{'weights':[1,2]}},
        execution_model='trade_tick_v1' if frequency=='tick' else 'bar_next_interval_v1',participation_rate='1',slippage_bps='0',seed=23,
        reference_calendar='synthetic-d10-calendar')
    return dict(run_id=run_id,config=config,calendar=calendar,calendar_reference='synthetic-d10-calendar',next_session='NEXT',rules=[rule],facts=facts,
        corporate_actions=[],initial_marks={},source=dict(kind='fixture',events=events),synthetic_model=MODEL,
        result_budget_bytes=16*1024*1024,data_budget_bytes=64*1024*1024,max_active_orders=1000,max_commands_per_boundary=1000,chunk_events=32,wall_time_seconds=30)

def event_key(event):
    row=event['event']; identity=row['identity']
    return (int(row.get('time_ns',row.get('interval_end_ns'))),row['security'],identity['source_session'],identity['channel'],int(identity['sequence']))

TEXT = ('kind','security','session','source_session','channel','open','high','low','close','price','bid','ask')
INTS = ('time_ns','interval_start_ns','interval_end_ns','quantity','bid_quantity','ask_quantity')
SEQS = ('sequence','stable_input_sequence')
SCHEMA = pa.schema([*(pa.field(n,pa.string()) for n in TEXT),*(pa.field(n,pa.int64()) for n in INTS),*(pa.field(n,pa.uint64()) for n in SEQS)], metadata={b'schema_id':b'qf.market.v1',b'time_unit':b'utc_nanoseconds',b'exchange_timezone':b'Asia/Shanghai',b'price_currency':b'CNY',b'quantity_unit':b'shares',b'price_basis':b'raw'})

def batch(events):
    rows=[]
    for event in events:
        source=event['event']; identity=source['identity']
        row={n:source.get(n) for n in (*TEXT,*INTS,*SEQS)}
        row.update(kind=event['kind'],time_ns=int(source.get('time_ns',source.get('interval_end_ns'))),
            source_session=identity['source_session'],channel=identity['channel'],sequence=int(identity['sequence']),stable_input_sequence=int(identity['stable_input_sequence']))
        for name in ('interval_start_ns','interval_end_ns'):
            if row[name] is not None: row[name]=int(row[name])
        rows.append(row)
    table=pa.Table.from_pylist(rows,schema=SCHEMA)
    out=pa.BufferOutputStream()
    with pa.ipc.new_stream(out,SCHEMA) as writer: writer.write_table(table)
    return out.getvalue().to_pybytes()

class Gateway:
    """Test data-only peer for the actual D04 framing and Rust Arrow reader."""
    def __init__(self, spec, path, *, batch_rows=4, delay_seconds=0):
        self.spec,self.path,self.batch_rows=spec,path,batch_rows
        self.events=spec['source']['events']
        self.calls=Counter(); self.streams={}; self.max_batch=0; self.failure=None; self.changed=False
        self.delay_seconds = delay_seconds
        self.stopping = threading.Event()
        self.listener=socket.socket(socket.AF_UNIX); self.listener.bind(str(path)); self.listener.listen(1)
        self.thread=threading.Thread(target=self.serve,daemon=True); self.thread.start()
    def serve(self):
        channel=None
        try:
            peer,_=self.listener.accept(); channel=Channel(peer,timeout=10)
            while True:
                request=channel.receive(); self.calls[request.op]+=1
                status,body,payload='ok',{},b''
                if request.op=='check':
                    if self.calls['check'] == 1 and self.delay_seconds:
                        threading.Event().wait(self.delay_seconds)
                    if self.changed:
                        status,body='error',dict(code='DATA_CHANGED',operation='data_gateway',message='synthetic dependency changed',scope={})
                    else:
                        body=dict(check='unchanged',limits=dict(batch_rows=self.batch_rows),context=dict(scope_id='synthetic-d10-scope',dependencies=[]))
                elif request.op=='open_stream':
                    if len(self.streams) >= 2:
                        channel.send(Frame(request.run_id,request.request_id,request.op,'error',
                            dict(code='RESOURCE_LIMIT',operation='data_gateway',message='synthetic stream credit exceeded',scope={})))
                        break
                    query=request.body['request']
                    selected=[event for event in self.events if event['event']['security'] in query['securities']
                        and int(event['event'].get('time_ns',event['event'].get('interval_end_ns'))) <= int(query['end_ns'])
                        and (query['start_ns'] is None or int(event['event'].get('time_ns',event['event'].get('interval_end_ns'))) >= int(query['start_ns']))]
                    count=query['count_per_security']
                    if count is not None:
                        selected=[e for s in query['securities'] for e in [v for v in selected if v['event']['security']==s][-count:]]
                    selected.sort(key=event_key)
                    stream=f'stream-{self.calls[request.op]}'
                    self.streams[stream]=(selected,min(request.body['max_rows'],self.batch_rows)); body=dict(stream_id=stream)
                elif request.op=='next':
                    items,credit=self.streams[request.body['stream_id']]
                    if not items:
                        self.streams.pop(request.body['stream_id']); status='eof'
                    else:
                        rows=items[:credit]; del items[:credit]; self.max_batch=max(self.max_batch,len(rows)); payload=batch(rows)
                        times=[event_key(e)[0] for e in rows]
                        body=dict(schema_id='qf.market.v1',rows=len(rows),time_unit='utc_nanoseconds',price_currency='CNY',quantity_unit='shares',
                            actual_scope=dict(start_ns=str(min(times)),end_ns=str(max(times)),securities=sorted({e['event']['security'] for e in rows})),
                            limitations=['synthetic_business_oracle_not_production_data'])
                        status='batch'
                elif request.op=='close_stream': self.streams.pop(request.body['stream_id'],None)
                elif request.op in ('close','cancel'):
                    channel.send(Frame(request.run_id,request.request_id,request.op,'ok',{})); break
                channel.send(Frame(request.run_id,request.request_id,request.op,status,body,payload))
        except TransportError as error:
            if error.code != 'CANCELLED': self.failure=error
        except BaseException as error:
            if not self.stopping.is_set(): self.failure=error
        finally:
            self.streams.clear()
            if channel is not None: channel.close()
            self.listener.close()
    def close(self):
        self.stopping.set()
        try: self.listener.shutdown(socket.SHUT_RDWR)
        except OSError: pass
        self.listener.close(); self.thread.join(12)
        if self.thread.is_alive(): raise AssertionError('synthetic gateway did not release')
        if self.failure is not None: raise self.failure

@contextmanager
def ipc_spec(source_spec, *, batch_rows=4, delay_seconds=0):
    with tempfile.TemporaryDirectory(prefix='qf-d10-fixture-') as directory:
        gateway=Gateway(deepcopy(source_spec),Path(directory)/'data.sock',batch_rows=batch_rows, delay_seconds=delay_seconds)
        wired=deepcopy(source_spec)
        fields=('price','quantity','bid','ask','bid_quantity','ask_quantity') if wired['config']['frequency']=='tick' else ('open','high','low','close','quantity')
        request=dict(securities=wired['config']['universe'],fields=list(fields),frequency=wired['config']['frequency'],
            start_ns=wired['calendar'][0]['before_open_ns'],end_ns=wired['calendar'][-1]['after_close_ns'],count_per_security=None,adjustment='none')
        wired['source']=dict(kind='ipc',socket_path=str(gateway.path),binding='market',request=request)
        try:
            yield wired,gateway,{wired['config']['frequency']:_MarketRoute('market',tuple(fields))}
        finally: gateway.close()
