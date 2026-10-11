"""Monthly net-profit selection using verified public/effective times.

Requires historical net_profit; current snapshots/report dates are insufficient.
Industry and index membership are not assumed by this example.
"""
from quantfoundry.api import get_open_orders, log, order_target_percent, record, run_daily, get_fundamentals, run_monthly


def initialize(context):
    context.state.pool = list(context.params["securities"])
    context.state.selected = None
    run_monthly(select_stocks, trading_day=1, time="after_close")
    run_daily(_continue_rotation, time="after_close")


def select_stocks(context):
    field = context.params.get("fundamental_field", "net_profit")
    frame = get_fundamentals(context.state.pool, fields=[field], limit=10000)
    positive = frame[frame[field].notna() & (frame[field] > 0)]
    selected = positive.sort_values([field, "security"], ascending=[False, True])
    context.state.selected = set(selected.head(context.params.get("top_n", 1))["security"])

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
