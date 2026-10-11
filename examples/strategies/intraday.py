"""Use the same callback with real 1m or 60m bars; no interpolation.

Needs completed raw OHLC/quantity, explicit sessions, rules and costs.
"""
from quantfoundry.api import get_price, log, order_target_percent


def initialize(context):
    context.state.security = context.params["security"]


def handle_data(context, data):
    # The final close has no next interval in this configured run.
    if context.current_dt.date().isoformat() >= context.params["end_date"] and context.current_dt.hour >= 15:
        return
    window = context.params.get("window", 10)
    frame = get_price([context.state.security], count=window, fields=["close"])
    prices = list(frame.sort_values("time_ns")["close"])
    if len(prices) == window and all(p is not None for p in prices):
        target = "0.5" if prices[-1] * len(prices) > sum(prices) else "0"
        result = order_target_percent(context.state.security, target)
        if not result.accepted:
            log.warning(f"{context.state.security}: {result.reason_code}: {result.message}")
