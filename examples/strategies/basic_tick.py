"""Per trade Tick callback; this does not simulate an exchange queue.

Needs exact ns, session/channel/sequence, trade price/quantity and dated rules.
"""
from decimal import Decimal
from quantfoundry.api import log, order_target_value, record


def initialize(context):
    context.state.security = context.params["security"]
    context.state.threshold = Decimal(context.params["threshold"])
    context.state.callbacks = 0


def handle_tick(context, tick):
    if tick.security != context.state.security or tick.kind != "trade" or tick.last_price is None:
        return
    context.state.callbacks += 1
    target = "1000" if tick.last_price < context.state.threshold else "0"
    result = order_target_value(context.state.security, target)
    if not result.accepted:
        log.warning(f"{tick.security}: {result.reason_code}: {result.message}")
    record(tick_callbacks=context.state.callbacks, tick_time_ns=str(tick.time_ns))
