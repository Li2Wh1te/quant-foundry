"""Real Python workloads. Decimal signals, Rust orders/account/costs/results."""

MOMENTUM = """from quantfoundry.api import get_price, order_target_percent, record, log
from decimal import Decimal

def initialize(c):
    c.state.pool=tuple(c.params['securities'])
    c.state.calls=0
    c.state.order_limit_rejections=0

def checked_order(c,result):
    if result.accepted:
        return
    # The fixed synthetic price path can compound beyond the unchanged dated
    # rule's per-order maximum. A correct refusal is a business result, not a
    # strategy crash. Keep EVERY target attempt, rule and callback; record the
    # refusal explicitly. Other order/account/contract failures still abort.
    if result.reason_code=='INVALID_ORDER' and result.message=='超过单笔申报数量上限':
        c.state.order_limit_rejections+=1
        log.warning(result.reason_code+': '+result.message)
        return
    log.error(result.reason_code+': '+result.message)
    raise RuntimeError(result.reason_code)

def handle_data(c,data):
    c.state.calls+=1
    frame=get_price(c.state.pool,count=c.params['window'],fields=['close'])
    ordered=frame.sort_values(['security','time_ns'])
    grouped=ordered.groupby('security',sort=True)['close'].agg(['size','first','last'])
    valid=grouped[grouped['size']==c.params['window']]
    # Object arithmetic preserves the original Decimal prices.
    scores=(valid['last']/valid['first']-Decimal(1)).to_dict()
    if scores:
        selected=set(sorted(scores,key=lambda s:(-scores[s],s))[:5])
        positions=c.portfolio.positions
        waiting=False
        for security,position in positions.items():
            if security not in selected:
                result=order_target_percent(security,'0')
                checked_order(c,result)
                waiting|=position.quantity>0
        if not waiting:
            for security in sorted(selected):
                result=order_target_percent(security,'0.15')
                checked_order(c,result)
    record(callbacks=c.state.calls,equity=c.portfolio.total_value,
           order_limit_rejections=c.state.order_limit_rejections)
"""
INTRADAY = """from quantfoundry.api import get_price, get_current_data, order_target_percent, record, log, LimitOrder

def initialize(c):
    c.state.pool=tuple(c.params['securities']);c.state.calls=0

def handle_data(c,data):
    c.state.calls+=1
    frame=get_price(c.state.pool,count=10,fields=['close'])
    if c.state.calls%240==2 or c.state.calls==2:
        for security in c.state.pool[:5]:
            result=order_target_percent(security,'0.05',style=LimitOrder(get_current_data([security])[security].last_price))
            if not result.accepted:
                log.error(result.reason_code+': '+result.message)
                raise RuntimeError(result.reason_code)
    if c.state.calls%240==239:
        for security in c.state.pool[:5]:
            result=order_target_percent(security,'0',style=LimitOrder(get_current_data([security])[security].last_price))
            if not result.accepted:
                log.error(result.reason_code+': '+result.message)
                raise RuntimeError(result.reason_code)
    if c.state.calls%240==0:
        record(callbacks=c.state.calls,window_rows=len(frame),equity=c.portfolio.total_value)

def after_trading_end(c):
    record(callbacks=c.state.calls,equity=c.portfolio.total_value)
"""
TICKS = """from quantfoundry.api import order_target,record,LimitOrder

def initialize(c):
    c.state.calls=0;c.state.security=c.params['securities'][0]

def handle_tick(c,tick):
    c.state.calls+=1
    if c.state.calls==1:
        expected=c.params['expected_first']
        actual=tick.last_price if expected['field']=='price' else tick.ask
        assert str(c.now_ns)==expected['time_ns'] and str(actual)==expected['value']
    if c.state.calls%100000==1 or c.state.calls==1:
        target=10 if (c.state.calls//100000)%2==0 else 0
        result=order_target(c.state.security,target,style=LimitOrder('10.02' if target else '10.00'))
        if not result.accepted: raise RuntimeError(result.reason_code)

def after_trading_end(c):
    record(callbacks=c.state.calls,equity=c.portfolio.total_value)
"""

HOT_PROBE = """from quantfoundry.api import get_price
import time

def initialize(c): c.state.calls=0

def handle_tick(c,tick):
    c.state.calls+=1
    if c.state.calls==100:
        print('D18_HOT_PROBE_STARTED',flush=True)
        # The coordinator measures the unchanged 15-second detection window
        # from the actual CurrentStore commit, not from staging its correction.
        # Original worker wall/resource limits still bound this test strategy.
        while True:
            get_price([tick.security],count=1,frequency='tick',fields=['price' if tick.kind=='trade' else 'ask'])
            time.sleep(.01)
"""
