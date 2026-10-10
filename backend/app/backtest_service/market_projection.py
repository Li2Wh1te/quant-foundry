"""Fixed Arrow column projections of the existing CurrentStore node registry."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.compute as pc

from app.data_store.adapters.registry import BY_ID, SYNTHETIC
from .data_gateway import MarketBinding


def _date(ns):
    # Partition hint only; comparisons retain original int64 nanoseconds.
    return datetime.fromtimestamp(ns//1_000_000_000, tz=ZoneInfo('Asia/Shanghai')).date().isoformat()


def current_market_bindings():
    """Provider reported prices are never promoted to raw execution input."""
    result = []
    for entry_id in ('E50', 'E51', 'E52', 'E70'):
        entry = BY_ID[entry_id]
        model = entry.projection[-2]
        date_col = entry.layout.columns[model, 'trade_date'][0]
        fields = {f: entry.layout.columns[model, f if entry_id == 'E70' else 'reported_'+f][0]
                  for f in ('open', 'high', 'low', 'close')}
        result.append(MarketBinding(entry_id, entry.spec, '1d', fields, {},
            lambda security, start, end: ((security,), (security+'\0',)), date_col,
            unavailable_reason='raw_execution_basis_or_units_unverified',
            limitations=tuple(entry.describe_capability()['limitations'])))
    return tuple(result)


def synthetic_market_binding(entry, representation, *, frequency='tick'):
    """Explicit oracle contract: CNY/raw, integer shares, named source session.

    Reads the two existing synthetic registry layouts through the real store.
    normalize is used only when constructing isolated formal input, never here.
    D03/supervisor separately accept its calendar/session intervals. This is not
    registered by default and cannot advertise production data availability.
    """
    if entry not in SYNTHETIC or ':' not in representation:
        raise ValueError('explicit synthetic registry contract required')
    model = entry.projection[-2]
    def column(f):
        return entry.layout.columns[model, f][0]
    tick = entry.native == 'tick'
    fields = {'security': column('source_code'), 'session': '_session', 'source_session': '_session',
              'time_ns': column('event_ns' if tick else 'start_ns')}
    if tick:
        fields.update(price=column('price'), quantity=column('quantity'), channel=column('channel'),
                      sequence=column('sequence'), stable_input_sequence=column('sequence'))
        constants, duration = {'kind': 'trade_tick'}, 0
    else:
        if frequency not in ('1m','5m','15m','30m','60m'):
            raise ValueError('unsupported synthetic frequency')
        duration = int(frequency[:-1])*60*1_000_000_000
        fields.update({f: column(f) for f in ('open','high','low','close')})
        fields.update(quantity=column('volume'), interval_start_ns=column('start_ns'),
                      interval_end_ns='_end', stable_input_sequence='_identity')
        constants = {'kind': 'bar', 'channel': frequency}

    def transform(table):
        session = pc.binary_join_element_wise(pc.cast(table[column('trading_date')], pa.string()),
                                              table[column('session')], ':')
        table = table.append_column('_session', session)
        if not tick:
            end = pc.add_checked(table[column('start_ns')], duration)
            identity = pc.cast(pc.add_checked(pc.cast(table[column('start_ns')], pa.decimal128(20,0)),
                                             pa.scalar(2**63, type=pa.decimal128(20,0))), pa.uint64())
            table = table.append_column('_end', end).append_column('_identity', identity)
        return table

    guards = ((column('currency'), 'CNY'), (column('price_basis'), 'raw'))
    if not tick:
        guards += ((column('frequency'), frequency),)
    return MarketBinding(entry.id, entry.spec, frequency, fields, constants,
        lambda security, start, end: ((representation, security, _date(start)),
                                      (representation, security, _date(end)+'\uffff')),
        column('event_ns' if tick else 'start_ns'), time_offset_ns=duration,
        guards=guards, transform=transform,
        extra_columns=(column('trading_date'), column('session')),
        derived_columns=('_session', '_end', '_identity'),
        selectors=() if tick else ((column('frequency'), '=', frequency),),
        limitations=('synthetic_business_oracle_not_production_data', 'current_revision_not_snapshot'))
