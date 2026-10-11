"""Actual D04 server arbitration with synthetic data-only generators.

These tests require no filesystem admission and are not CurrentStore acceptance.
The separate installed-wheel integration test uses real admitted CurrentStore.
"""
from contextlib import contextmanager
import importlib.util
from pathlib import Path
import socket
import sys
import threading
from types import SimpleNamespace

import pyarrow as pa
import pytest

from app.backtest_service.data_gateway import DataBatch, GatewayError, MARKET_SCHEMA, serve

_loader = importlib.util.spec_from_file_location('d10_stream_transport',
    Path(__file__).resolve().parents[2]/'sdk/python/quantfoundry/_transport.py')
transport = importlib.util.module_from_spec(_loader)
sys.modules[_loader.name] = transport
_loader.loader.exec_module(transport)


class _ProtocolFixture:
    """Synthetic batches only; no account, matching or storage implementation."""
    batch_rows = 2
    batch_bytes = 65536

    def __init__(self):
        self.grant = SimpleNamespace(run_id='synthetic-stream-test')
        self.cancelled = lambda: False
        self.active = set()
        self.closed_streams = set()
        self.context = SimpleNamespace(closed=False, to_dict=lambda: {
            'scope_id':'synthetic-stream-context', 'dependencies':[]})

    def _request(self, name, query, context):
        assert name == 'synthetic-market' and context is self.context
        assert set(query) == {'label'}

    def read(self, name, query, context, *, page_rows):
        label = query['label']
        self.active.add(label)
        try:
            if label == 'changed-query':
                raise GatewayError('DATA_CHANGED','synthetic dependency changed')
            for page in range(2):
                rows = [dict(kind='trade_tick',security='A.SH',session='synthetic',
                    source_session='synthetic',channel='synthetic',sequence=page*page_rows+i,
                    stable_input_sequence=page*page_rows+i,time_ns=100+page*page_rows+i,
                    price='1'*self.batch_bytes if label=='oversized-query' else '10',
                    quantity=1) for i in range(page_rows)]
                table = pa.Table.from_pylist(rows, schema=MARKET_SCHEMA)
                yield DataBatch(table,dict(schema_id='qf.market.v1',time_unit='utc_nanoseconds',
                    price_currency='CNY',quantity_unit='shares',rows=page_rows,
                    actual_scope=dict(start_ns=str(rows[0]['time_ns']),end_ns=str(rows[-1]['time_ns']),
                        securities=['A.SH']),limitations=['synthetic_protocol_test']))
        finally:
            self.active.remove(label)
            self.closed_streams.add(label)

    def check(self, context):
        assert context is self.context and not context.closed

    def close(self, context):
        assert not self.active
        context.closed = True


@contextmanager
def _connection():
    gateway = _ProtocolFixture()
    parent, child = socket.socketpair()
    failures = []
    def run():
        try:
            serve(transport.Channel(parent,timeout=2),gateway,gateway.context)
        except transport.TransportError:
            pass
        except BaseException as error:
            failures.append(error)
    thread = threading.Thread(target=run)
    thread.start()
    client = transport.Client(transport.Channel(child,timeout=2),gateway.grant.run_id)
    try:
        yield gateway, client
    finally:
        client.channel.close()
        thread.join(3)
        assert not thread.is_alive() and not failures and gateway.context.closed, failures
        assert not gateway.active


def _open(client, label, credit=2):
    return client.call('open_stream',dict(binding='synthetic-market',request={'label':label},
        max_rows=credit)).body['stream_id']


def _next(client, stream):
    frame = client.call('next',{'stream_id':stream})
    if frame.status == 'eof':
        return None
    assert frame.status == 'batch' and frame.body['rows'] <= 2
    return pa.ipc.open_stream(frame.payload).read_all()['sequence'].to_pylist()


@pytest.mark.parametrize('release', ['close_stream','eof'])
def test_source_continues_while_callback_query_releases_and_reuses_credit(release):
    with _connection() as (gateway, client):
        limits = client.call('check',{}).body['limits']
        assert limits == dict(batch_rows=2,batch_bytes=65536,max_active_streams=2,
            stream_row_credits=4,stream_batch_bytes=131072)
        source = _open(client,'source')
        assert _next(client,source) == [0,1]
        query = _open(client,'query',credit=1)
        assert _next(client,query) == [0]
        assert gateway.active == {'source','query'}
        if release == 'eof':
            assert _next(client,query) == [1]
            assert _next(client,query) is None
        else:
            client.call('close_stream',{'stream_id':query})
        assert gateway.active == {'source'}
        query = _open(client,'next-query')
        assert _next(client,query) == [0,1]
        assert _next(client,source) == [2,3]
        assert _next(client,source) is None
        client.call('close',{})
    assert gateway.closed_streams == {'source','query','next-query'}


@pytest.mark.parametrize('ending', ['excess_stream','excess_rows','cancel','disconnect'])
def test_two_active_streams_terminal_limit_cancel_disconnect_release_both(ending):
    with _connection() as (gateway, client):
        source = _open(client,'source')
        query = _open(client,'query')
        _next(client,source)
        _next(client,query)
        assert gateway.active == {'source','query'}
        if ending in {'excess_stream','excess_rows'}:
            if ending == 'excess_rows':
                # Leave a free slot so the row-credit assertion cannot be
                # satisfied solely by the third-stream rejection.
                client.call('close_stream',{'stream_id':query})
            with pytest.raises(transport.TransportError) as caught:
                _open(client,'third',credit=3 if ending=='excess_rows' else 1)
            assert caught.value.code == 'RESOURCE_LIMIT'
        elif ending == 'cancel':
            client.call('cancel',{})
        else:
            client.channel.close()
    assert gateway.closed_streams == {'source','query'}


@pytest.mark.parametrize(('label','code'),[
    ('oversized-query','RESOURCE_LIMIT'),('changed-query','DATA_CHANGED')])
def test_query_byte_budget_and_data_change_release_retained_source(label,code):
    with _connection() as (gateway, client):
        source = _open(client,'source')
        _next(client,source)
        query = _open(client,label)
        with pytest.raises(transport.TransportError) as caught:
            _next(client,query)
        assert caught.value.code == code
    assert gateway.closed_streams == {'source',label}
