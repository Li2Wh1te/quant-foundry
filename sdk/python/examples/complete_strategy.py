"""Complete callback example; used with named isolated fixtures in SDK tests.

Buy A, wait for its real fill, sell A, wait for the real sale, then buy B.
No pending sale is treated as cash. This is an API example, not trading advice.
"""
from quantfoundry.api import (
    get_price, get_current_data, get_trades, get_order, order, order_target,
    cancel_order, LimitOrder, record, log, run_daily, subscribe,
)

def initialize(context):
    context.state.phase = 'start'
    context.state.sessions = 0
    subscribe(['A.SH', 'B.SH'], events='bar')
    run_daily(close_record, time='after_close')

def before_trading_start(context):
    context.state.sessions += 1
    # First-session prices are explicitly unknown before the first event.
    quote = get_current_data()['A.SH']
    record(known_price=int(quote.last_price is not None))

def handle_data(context, data):
    frame = get_price(['A.SH', 'B.SH'], count=context.params['window'])
    assert frame['time_ns'].max() <= context.now_ns
    assert tuple(data.securities) == ('A.SH', 'B.SH')
    if context.state.phase == 'start':
        temporary = order('B.SH', 1, style=LimitOrder('9'))
        assert temporary.accepted and cancel_order(temporary.order_id).accepted
        result = order_target('A.SH', 10)
        assert result.accepted
        assert order_target('A.SH', 10).unchanged
        assert context.portfolio.frozen_cash == 101
        context.state.phase = 'waiting_A_buy'

def on_order(context, changed_order):
    # The notification describes that transition; current state may be later
    # when several commands happened in one callback.
    current = get_order(changed_order.order_id)
    log.info(f'{current.security}: {changed_order.status}')

def on_trade(context, trade):
    assert get_trades(limit=1).items
    if trade.security == 'A.SH' and trade.side == 'buy':
        assert context.portfolio.positions['A.SH'].quantity == 10
        assert order_target('A.SH', 0).accepted
        context.state.phase = 'waiting_A_sale'
    elif trade.security == 'A.SH' and trade.side == 'sell':
        # Only executed proceeds are available now.
        assert context.portfolio.available_cash == 9998
        assert order_target('B.SH', 10).accepted
        context.state.phase = 'waiting_B_buy'
    elif trade.security == 'B.SH' and trade.side == 'buy':
        context.state.phase = 'complete'

def close_record(context):
    record(cash=context.portfolio.cash, equity=context.portfolio.total_value)

def after_trading_end(context):
    if context.state.sessions == 4:
        assert context.state.phase == 'complete'
        assert context.portfolio.cash == 9897
        assert context.portfolio.total_value == 9997
        assert context.portfolio.positions['B.SH'].quantity == 10
