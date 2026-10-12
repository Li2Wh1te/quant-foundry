"""Trusted D10 worker glue. D12 owns OS/container isolation and supervision.

Importing this module executes no strategy. The standalone entrypoint currently
accepts explicitly named synthetic isolation tests; production admission remains
unavailable until D04 accepted facts and D12 process controls are connected.
"""
from __future__ import annotations
from contextvars import ContextVar
import importlib.util
import json
from pathlib import Path
import random
import sys
from threading import Lock
from types import SimpleNamespace
from copy import deepcopy
import time
import numpy as np
from . import _native
from .context import Context, BarData, _ACTIVE, _binding, _request, _order, _trade
from .data import _DataSession, _MarketRoute, _callback, _error, _json, CurrentQuote, Tick

_HOST: ContextVar[_Host | None] = ContextVar('qf_worker_host', default=None)
_RUNNING: ContextVar[bool] = ContextVar('qf_worker_running', default=False)
_RUN_LOCK = Lock()

def _host():
    host = _HOST.get()
    if host is None:
        raise _error('CAPABILITY_UNAVAILABLE', '策略宿主尚未绑定')
    return host

def _quote(event, now):
    source = event['event']
    kind = event['kind']
    time_ns = int(source['interval_end_ns'] if kind == 'bar' else source['time_ns'])
    if kind == 'bar':
        from decimal import Decimal
        return CurrentQuote(source['security'], time_ns, Decimal(source['close']), time_ns, time_ns < now, None)
    from decimal import Decimal
    identity = source['identity']
    price = Decimal(source['price']) if kind == 'trade_tick' else None
    unknown = price is None and source.get('bid') is None and source.get('ask') is None
    return Tick(source['security'], time_ns, price, time_ns if price is not None else None, unknown or time_ns < now, None,
        identity['channel'], None if identity['sequence'] is None else int(identity['sequence']),
        'trade' if kind == 'trade_tick' else 'quote',
        None if source.get('bid') is None else Decimal(source['bid']),
        None if source.get('ask') is None else Decimal(source['ask']),
        source.get('quantity'), source.get('bid_quantity'), source.get('ask_quantity'))

class _BridgeClient:
    def __init__(self, bridge, arrow_budget):
        self.bridge = bridge
        self.channel = SimpleNamespace(closed=False, arrow_budget=arrow_budget, close=self.close)
    def close(self):
        self.channel.closed = True
    def call(self, op, body):
        if self.channel.closed:
            raise _error('CANCELLED', '策略数据通道已关闭')
        encoded, payload = self.bridge.request('data', _json({'op':op, 'body':body}))
        response = json.loads(encoded)
        return SimpleNamespace(status=response['status'], body=response['body'], payload=bytes(payload))

class _Host:
    def __init__(self, module, spec, routes, research_routes):
        self.module, self.spec = module, spec
        self.context = Context(spec['config']['parameters'], spec['calendar'][0]['session']['exchange_timezone'])
        self.callbacks = {}
        self.callback_metrics = dict(count=0, seconds=0.0, by_kind={})
        total = spec['data_budget_bytes']
        self.packet_bytes = min(total // 8, 64*1024*1024)
        cache_bytes = max(1, self.packet_bytes//8, min(32*1024*1024,total//32))
        # The native source retains at most three packet buffers. Strategy
        # windows have a separate credit; equating the whole window to one
        # Arrow packet incorrectly rejected multi-batch cross sections.
        data_grant = total - 3*self.packet_bytes
        view_bytes = min(_native.MAX_VIEW_BYTES,
                         data_grant - 4*cache_bytes - 2*self.packet_bytes - 2*1024*1024)
        if view_bytes <= 0:
            raise _error('RESOURCE_LIMIT', '数据窗口和 IPC 未获得足够的完整运行预算')
        self.session = _DataSession(SimpleNamespace(channel=SimpleNamespace(arrow_budget=self.packet_bytes)),
            universe=spec['config']['universe'], frequency=spec['config']['frequency'],
            market_routes=routes, research_routes=research_routes, calendar=spec['calendar'],
            max_rows=spec.get('data_max_rows', 10000), max_bytes=view_bytes, cache_bytes=cache_bytes,
            run_data_budget_bytes=data_grant)
        self.handlers = {name: callable(getattr(module, method, None)) for name, method in (
            ('bars','handle_data'), ('ticks','handle_tick'), ('before_open','before_trading_start'),
            ('after_close','after_trading_end'), ('orders','on_order'), ('trades','on_trade'))}

    def register(self, func, cadence, trading_day, time):
        if len(self.callbacks) >= 1024:
            raise _error('RESOURCE_LIMIT', '调度注册超过预算')
        key = f'schedule-{len(self.callbacks)}'
        _request('schedule', {'callback':key,'cadence':cadence,'trading_day':trading_day,'time':time})
        self.callbacks[key] = func

    def dispatch(self, kind, encoded, bridge):
        callback_started = time.monotonic()
        if _ACTIVE.get() is not None:
            raise _error('INVALID_CONTRACT', '策略不能递归进入回调')
        payload = json.loads(encoded)
        self.context._now_ns = int(payload['boundary']['now_ns'])
        token = _ACTIVE.set((self.context, bridge))
        host_token = _HOST.set(self)
        self.session.client = _BridgeClient(bridge, self.packet_bytes)
        try:
            with _callback(self.session, payload['boundary'], native_bridge=bridge):
                data = payload['data']
                func = self.callbacks[data['callback']] if kind == 'scheduled' else getattr(self.module, kind, None)
                if not callable(func):
                    return
                if kind == 'handle_data':
                    func(self.context, BarData(int(data['time_ns']), tuple(data['securities'])))
                elif kind == 'handle_tick':
                    func(self.context, _quote(data, self.context.now_ns))
                elif kind == 'on_order':
                    func(self.context, _order(data))
                elif kind == 'on_trade':
                    func(self.context, _trade(data))
                else:
                    func(self.context)
        finally:
            elapsed = time.monotonic()-callback_started
            self.callback_metrics['count'] += 1
            self.callback_metrics['seconds'] += elapsed
            item = self.callback_metrics['by_kind'].setdefault(kind, dict(count=0, seconds=0.0))
            item['count'] += 1
            item['seconds'] += elapsed
            self.session.client = None
            _HOST.reset(host_token)
            _ACTIVE.reset(token)

    def close(self):
        self.session.cache.clear()
        self.session.cache_size = 0
        self.session.window, self.session.window_size = None, 0
        self.callbacks.clear()

def _run_module(module, spec, *, market_routes=None, research_routes=None, cancellation=None, _seeded=False, _progress=None):
    """Internal integration seam, called in a supervisor-owned worker or tests."""
    if _RUNNING.get():
        raise _error('INVALID_CONTRACT', '不能递归运行策略')
    if not _RUN_LOCK.acquire(blocking=False):
        raise _error('INVALID_CONTRACT', '同一worker进程只允许一个运行；并发run须使用独立worker')
    # Normalize through the same Rust RunConfig before reading seed/parameters.
    token = _RUNNING.set(True)
    host = None
    try:
        spec = deepcopy(spec)
        calendar = spec.pop('calendar')
        spec = json.loads(_json(spec))
        spec['calendar'] = calendar
        spec['config'] = json.loads(_native.validate_run_config_json(_json(spec['config'])))
        if not _seeded:
            random.seed(spec['config']['seed'])
            np.random.seed(spec['config']['seed'])
        if not spec['calendar']:
            raise _error('RULE_UNAVAILABLE', '缺少已接受运行日历')
        host = _Host(module, spec, market_routes or {}, research_routes or {})
        from ._results import RunResults
        compact_spec = dict(spec, calendar=[])
        calendar_input = json.dumps(calendar, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        if len(calendar_input.encode()) > 16*1024*1024 or not 1 <= len(calendar) <= 10000:
            raise _error('RESOURCE_LIMIT', '运行日历超过有界元数据预算')
        result = RunResults(_native.run_strategy(_json(compact_spec), _json(host.handlers), host.dispatch,
            cancellation if cancellation is not None else _native.CancellationToken(), calendar_input, _progress))
        result._callback_metrics = host.callback_metrics
        return result
    finally:
        if host is not None:
            host.close()
        _RUNNING.reset(token)
        _RUN_LOCK.release()

def main():
    if sys.argv[1:] == ['--supervised']:
        from ._supervised import main as supervised_main
        return supervised_main()
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--strategy', required=True)
    parser.add_argument('--isolated-test', action='store_true')
    args = parser.parse_args()
    result = None
    try:
        source = sys.stdin.buffer.read(1024*1024+1)
        if len(source) > 1024*1024:
            raise _error('RESOURCE_LIMIT', '工作进程控制消息超限')
        packet = json.loads(source)
        spec = packet['spec']
        if not args.isolated_test or not spec.get('synthetic_model'):
            raise _error('CAPABILITY_UNAVAILABLE', 'D04正式事实映射与D12进程隔离尚未联调；此入口只允许明确隔离测试')
        spec['config'] = json.loads(_native.validate_run_config_json(_json(spec['config'])))
        random.seed(spec['config']['seed'])
        np.random.seed(spec['config']['seed'])
        # Import occurs here, in the worker process, never in a web import path.
        path = Path(args.strategy)
        module_spec = importlib.util.spec_from_file_location('_qf_user_strategy', path)
        if module_spec is None or module_spec.loader is None:
            raise _error('STRATEGY_ERROR', '策略模块不可加载')
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        routes = {name:_MarketRoute(row['binding'], tuple(row['fields'])) for name,row in packet.get('market_routes',{}).items()}
        result = _run_module(module, spec, market_routes=routes, research_routes=packet.get('research_routes'), _seeded=True)
        summary = json.loads(result.summary_json())
        # Entrypoint returns a bounded summary; D12/D13 will consume the result
        # writer and own retained result lifetime, rather than download all rows.
        print(_json(summary))
        return 0 if summary['outcome']['status'] == 'succeeded' else 1
    except BaseException as error:
        diagnostic = json.loads(_native.strategy_error_json(error))
        outcome = ({'status':'cancelled','partial':False} if diagnostic['code']=='CANCELLED'
            else {'status':'failed','error':diagnostic,'partial':False})
        print(_json({'outcome':outcome,'production_executed':False}))
        return 1
    finally:
        if result is not None:
            result.close()

if __name__ == '__main__':
    # -m uses __main__; API lazy imports must resolve the same ContextVars.
    sys.modules['quantfoundry._worker'] = sys.modules[__name__]
    raise SystemExit(main())
