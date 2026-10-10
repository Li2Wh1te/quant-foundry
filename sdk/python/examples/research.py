"""Run inside a D10 callback. Uses the public API and no storage identifiers."""
from decimal import localcontext
from quantfoundry.api import get_price, get_fundamentals, Filter
from quantfoundry.indicators import sma


def price_research(securities, *, count=20, field='close'):
    """Return originals, float statistics, and strict warmup/missing status."""
    frame = get_price(securities, count=count, fields=[field])
    summaries = {}
    for security, window in frame.groupby('security', sort=True):
        originals = window[field]
        statistic = sma(originals, count).iloc[-1]
        status = statistic['status']
        above_exact_mean = None
        if status == 'available':
            # Original prices remain Decimal; the float SMA is research output.
            with localcontext() as context:
                context.prec = 160
                above_exact_mean = originals.iloc[-1]*len(originals) > sum(originals)
        summaries[security] = dict(status=status, sma_float=float(statistic['value']) if status=='available' else None,
                                   above_exact_mean=above_exact_mean, rows=len(window))
    return frame, summaries


def known_public_financials(securities):
    """Raises CAPABILITY_UNAVAILABLE when historical disclosure is unverified."""
    return get_fundamentals(securities, fields=['revenue', 'net_profit'],
        filters=[Filter('net_profit', 'gt', '0')], order_by=['-net_profit'], limit=100)
