"""Daily ETF momentum; requires raw bars, dated ETF rules and costs.

Synthetic tests use explicit fixture rules. These are not live ETF quotes.
"""
from decimal import Decimal, localcontext
from quantfoundry.api import get_open_orders, log, order_target_percent, record, run_daily, get_price


def initialize(context):
    context.state.pool = list(context.params["securities"])
    context.state.selected = None
    run_daily(rebalance, time="after_close")


def rebalance(context):
    window = context.params.get("window", 20)
    frame = get_price(context.state.pool, count=window, fields=["close"], adjustment="none")
    scores = {}
    for security, group in frame.groupby("security", sort=True):
        prices = list(group.sort_values("time_ns")["close"])
        if len(prices) == window and all(p is not None for p in prices) and prices[0] > 0:
            with localcontext() as arithmetic:
                arithmetic.prec = 38
                scores[security] = prices[-1] / prices[0] - Decimal(1)
    if scores:
        context.state.selected = {max(sorted(scores), key=scores.get)}
        _continue_rotation(context)

def _target(security, percent):
    result = order_target_percent(security, percent)
    if not result.accepted:
        log.warning(f"{security}: {result.reason_code}: {result.message}")
    return result


def _continue_rotation(context):
    # No new DAY order after the last declared session; retain terminal holdings.
    if context.current_dt.date().isoformat() >= context.params["end_date"]:
        return
    selected = context.state.selected
    if selected is None:
        return
    waiting = False
    for security in context.state.pool:
        if security not in selected:
            position = context.portfolio.positions.get(security)
            _target(security, "0")
            waiting |= bool(position and position.quantity > 0)
    # Accepted sells have no spendable proceeds. Check actual holdings AND
    # remaining sell orders at this later callback before submitting buys.
    waiting |= any(o.side == "sell" for o in get_open_orders().values())
    if waiting:
        record(waiting_for_sales=1)
        return
    for security in sorted(selected):
        _target(security, context.params.get("weight", "0.6"))
    record(waiting_for_sales=0)
