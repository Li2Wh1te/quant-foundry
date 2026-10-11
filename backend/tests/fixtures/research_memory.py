"""Isolated native/SDK working-set smoke, not CurrentStore/D18 acceptance.

Replays bounded Arrow packets in memory to measure projection, JSON, Decimal,
DataFrame and a retained returned frame in one independent process. Actual
CurrentStore/IPC consistency is exercised separately by the D05 suite.
"""
import json
from pathlib import Path
import resource
import sys
from types import SimpleNamespace
from decimal import Decimal

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import pyarrow as pa
from app.backtest_service.data_gateway import MARKET_SCHEMA, DataBatch
from quantfoundry import ContractError
from quantfoundry.data import _DataSession, _MarketRoute, _callback, get_price

BASE = 1767225600000000123
VALUE = '10.12345678901234567890123456'
LABEL = 'synthetic-' + 'x'*118
SYMBOLS = [f'A{i:04d}.SH' for i in range(5000)]


class Replay:
    def __init__(self):
        self.channel = SimpleNamespace(arrow_budget=64*1024*1024, closed=False)
        self.position = self.stop = self.input_rows = 0

    def call(self, operation, body):
        if operation == 'check':
            return SimpleNamespace(body=dict(check='unchanged', limits=dict(batch_rows=10000),
                context=dict(scope_id='synthetic-memory-only', dependencies=[])))
        if operation == 'open_stream':
            current = body['request']['start_ns'] == str(BASE)
            self.position, self.stop = (100000, 105000) if current else (0, 100000)
            return SimpleNamespace(body=dict(stream_id='synthetic-memory-only'))
        if operation == 'next':
            if self.position == self.stop:
                return SimpleNamespace(status='eof')
            samples = []
            for i in range(self.position, min(self.position+10000, self.stop)):
                symbol = SYMBOLS[i//20] if i < 100000 else SYMBOLS[i-100000]
                time = BASE-20+i%20 if i < 100000 else BASE
                row = {name: None for name in MARKET_SCHEMA.names}
                row.update(kind='bar', security=symbol, session=LABEL, source_session=LABEL,
                    channel=LABEL, time_ns=time, interval_start_ns=time-1, interval_end_ns=time,
                    sequence=i+1, stable_input_sequence=i+1, open=VALUE, high=VALUE,
                    low=VALUE, close=VALUE, quantity=1)
                samples.append(row)
            self.position += len(samples); self.input_rows += len(samples)
            batch = DataBatch(pa.Table.from_pylist(samples, schema=MARKET_SCHEMA),
                dict(schema_id='qf.market.v1', rows=len(samples), price_currency='CNY',
                     quantity_unit='shares', time_unit='utc_nanoseconds'))
            return SimpleNamespace(status='batch', body=batch.metadata, payload=batch.ipc(64*1024*1024))
        raise AssertionError(operation)


def main():
    replay = Replay()
    view_bytes = 1024*1024*1024
    allocated = view_bytes + 8*1024*1024 + 2*64*1024*1024 + 2*1024*1024
    for budget in (None, view_bytes):
        try:
            _DataSession(replay, universe=SYMBOLS, frequency='1d', market_routes={},
                         max_rows=105000, max_bytes=view_bytes, run_data_budget_bytes=budget)
        except ContractError as error:
            assert error.code == 'RESOURCE_LIMIT'
        else:
            raise AssertionError('oversized session omitted its total run data grant')
    session = _DataSession(replay, universe=SYMBOLS, frequency='1d',
        market_routes={'1d': _MarketRoute('synthetic', ('open', 'high', 'low', 'close', 'quantity'))},
        max_rows=105000, max_bytes=view_bytes, run_data_budget_bytes=allocated)
    boundary = dict(now_ns=str(BASE), market_through=dict(time_ns=str(BASE), phase='market',
        security=SYMBOLS[-1], identity=dict(source_session=LABEL, channel=LABEL,
        sequence='105000', stable_input_sequence='105000')))
    with _callback(session, boundary) as active:
        frame = get_price(SYMBOLS, count=20, fields=['open', 'high', 'low', 'close', 'quantity'])
        assert len(frame) == 100000 and frame.iloc[0]['close'] == Decimal(VALUE)
        assert frame.iloc[0]['time_ns'] == BASE-19 and frame.iloc[-1]['time_ns'] == BASE
        reserved = json.loads(active.view.resources_json())['working_set_bytes']
        active.view.reset()
        reset = json.loads(active.view.resources_json())
        # Keep the prior public frame alive while a subsequent conversion runs.
        next_frame = get_price(SYMBOLS, count=20, fields=['close'])
        assert len(next_frame) == len(frame) and frame.iloc[0]['close'] == Decimal(VALUE)
    print(json.dumps(dict(points=len(frame), window_input_rows=105000, replayed_rows=replay.input_rows,
        peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
        allocated_data_bytes=allocated, view_bytes=view_bytes,
        reserved_working_set_bytes=reserved,
        reset_capacities=[reset['price_capacity'], reset['research_capacity']])))


if __name__ == '__main__':
    main()
