"""Weekly stock selection, then daily completion of sells before buys.

Requires batched raw prices, dated stock rules, sellability and fee facts.
"""
from quantfoundry.api import get_open_orders, log, order_target_percent, record, run_daily, get_price, run_weekly


def initialize(context):
    context.state.pool = list(context.params["securities"])
    context.state.selected = None
    run_weekly(select_stocks, trading_day=context.params.get("decision_day", 1), time="after_close")
    run_daily(_continue_rotation, time="after_close")


def select_stocks(context):
    window = context.params.get("window", 20)
    frame = get_price(context.state.pool, count=window, fields=["close"], adjustment="none")
    eligible = []
    for security, group in frame.groupby("security", sort=True):
        prices = list(group.sort_values("time_ns")["close"])
        if len(prices) == window and all(p is not None for p in prices):
            # Decimal sum; Pandas.mean must not coerce prices to float.
            if prices[-1] * len(prices) > sum(prices):
                eligible.append(security)
    if len(frame) >= window * len(context.state.pool):
        context.state.selected = set(sorted(eligible)[:context.params.get("top_n", 1)])

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
