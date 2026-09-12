"""Raw-close momentum example; review parameters before running a backtest."""
from decimal import Decimal, ROUND_DOWN


def run(context, parameters):
    lookback = parameters.get("lookback", 60)
    holding_size = parameters.get("holding_size", 3)
    if type(lookback) is not int or lookback < 2:
        raise ValueError("lookback must be an integer of at least 2")
    if type(holding_size) is not int or holding_size < 1:
        raise ValueError("holding_size must be a positive integer")
    candidates = context.universe.query(
        exchanges=["SSE", "SZSE"], asset_classes=["etf"]
    )
    ranked = []
    for candidate in candidates:
        # Data-contract failures must remain visible; do not hide them as hold.
        bars = context.data.bars(candidate.instrument_id, lookback_sessions=lookback)
        if len(bars) < lookback:
            continue
        closes = [bar.values.get("close") for bar in bars]
        if any(value is None or not value.is_finite() or value <= 0 for value in closes):
            continue
        momentum = closes[-1] / closes[0] - Decimal(1)
        ranked.append((momentum, str(candidate.instrument_id)))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    leaders = ranked[:holding_size]
    if not leaders:
        return {"mode": "hold"}
    # Round down so the total target cannot exceed one; retain residual cash.
    weight = (Decimal(1) / Decimal(len(leaders))).quantize(
        Decimal("0.00000001"), rounding=ROUND_DOWN
    )
    return {"mode": "target_weights", "targets": {item[1]: str(weight) for item in leaders}}
