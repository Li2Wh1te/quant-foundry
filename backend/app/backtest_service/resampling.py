"""Bounded execution bars from an admitted finer CurrentStore stream.

OHLC/quantity and completeness are computed by the existing Rust D05
resampler. Only one declared calendar bucket is buffered; no market copy is
written. Source reads, cached answers and finalization keep the normal gates.
"""
import json
import time

import pyarrow as pa
import pyarrow.compute as pc

from .data_gateway import GatewayError, MARKET_SCHEMA, MarketBinding
from .formal_runtime import RunStoreGateway

MINUTE_NS = 60_000_000_000
MINUTE_FREQUENCIES = ('1m', '5m', '15m', '30m', '60m')
BAR_FIELDS = ('open', 'high', 'low', 'close', 'quantity')


def can_resample(source, target):
    return (source in MINUTE_FREQUENCIES and target in MINUTE_FREQUENCIES
            and int(source[:-1]) < int(target[:-1])
            and int(target[:-1]) % int(source[:-1]) == 0)


class ResampledBarGateway(RunStoreGateway):
    def __init__(self, *args, calendar, target_frequency, **kwargs):
        super().__init__(*args, **kwargs)
        markets = [b for b in self.bindings.values() if isinstance(b, MarketBinding)]
        if (len(markets) != 1 or not 1 <= len(calendar) <= 10000
                or not can_resample(markets[0].frequency, target_frequency)):
            raise GatewayError('CAPABILITY_UNAVAILABLE', '降采样需要已接受的细粒度行情和会话')
        self.calendar = tuple(calendar)
        self.target_frequency = target_frequency
        self.source_frequency = markets[0].frequency
        self.resampled_cache = {}
        self.resampled_query = None
        self.input_rows = self.output_rows = 0
        self.replay_raw_rows = self.replay_hour_rows = 0
        self.stats['parent_rust_resampling_and_conversion_seconds'] = 0.0

    def _request(self, name, request, context):
        actual = (dict(request, frequency=self.source_frequency)
                  if type(request) is dict and request.get('frequency') == self.target_frequency else request)
        return super()._request(name, actual, context)

    def _buckets(self, start, end, count):
        duration = int(self.target_frequency[:-1]) * MINUTE_NS
        # A count window searches backwards until EACH security has enough
        # observed complete bars. Calendar buckets are candidates, not bars.
        # Generate coordinates lazily, including long calendars; do not first
        # allocate all intervals or discard valid older per-security history.
        sessions = reversed(self.calendar) if count is not None else self.calendar
        for session in sessions:
            windows = reversed(session['bar_windows']) if count is not None else session['bar_windows']
            for window in windows:
                begin, stop = int(window['start_ns']), int(window['end_ns'])
                if stop < start or begin >= end or stop <= self.grant.start_ns:
                    continue
                buckets = (stop - begin + duration - 1) // duration
                first = max(0, (start - begin + duration - 1) // duration - 1,
                            (self.grant.start_ns - begin + duration - 1) // duration)
                last = buckets - 1 if stop <= end else min(buckets - 1, (end - begin) // duration - 1)
                indices = range(last, first - 1, -1) if count is not None else range(first, last + 1)
                for index in indices:
                    lower = begin + index * duration
                    finish = min(lower + duration, stop)
                    if start <= finish <= end and lower >= self.grant.start_ns:
                        yield session, lower, finish

    def _count_window(self, name, request, context, start, end, count):
        remaining = {s: count for s in request['securities']}
        parts, rows, size = [], 0, 0
        initial_bytes = self.stats['selected_file_bytes']
        initial_files = self.stats['files_read']
        deadline = time.monotonic() + self.store.limits.query_timeout_ms / 1000
        for index, (session, begin, finish) in enumerate(self._buckets(start, end, count)):
            if not remaining:
                break
            self.check(context)
            if index >= self.store.limits.query_rows or time.monotonic() >= deadline:
                raise GatewayError('RESOURCE_LIMIT', '逐标的降采样补取超过查询桶数或时间预算')
            wanted = tuple(sorted(remaining))
            table = self.resampled_cache.get(finish)
            if table is not None:
                table = table.filter(pc.is_in(table['security'], value_set=pa.array(wanted, type=pa.string())))
            else:
                table = self._bucket(name, dict(request, securities=list(wanted)), context,
                                     session, begin, finish, False)
            if (self.stats['selected_file_bytes'] - initial_bytes > self.store.limits.query_scan_bytes
                    or self.stats['files_read'] - initial_files > self.store.limits.query_files
                    or time.monotonic() >= deadline):
                raise GatewayError('RESOURCE_LIMIT', '逐标的降采样补取超过累计读取预算')
            # _bucket retains the existing Rust completeness/quality refusal.
            # An absent bucket contributes nothing; a damaged bucket still fails.
            for security in table['security'].to_pylist():
                remaining[security] -= 1
                if remaining[security] == 0:
                    del remaining[security]
            if table.num_rows:
                parts.append(table)
                rows += table.num_rows
                size += table.nbytes
                if rows > self.cache_rows or size > self.cache_bytes:
                    raise GatewayError('RESOURCE_LIMIT', '降采样历史窗口超过内存预算')
        self.check(context)
        # Reaching the authorized beginning is a genuine short history. Budget
        # exhaustion above raises and must never publish/cache a short answer.
        table = pa.concat_tables(parts) if parts else pa.Table.from_batches([], schema=MARKET_SCHEMA)
        if table.num_rows:
            table = table.sort_by([('time_ns', 'ascending'), ('security', 'ascending')])
        return table

    def _trim_cache(self):
        def usage():
            tables = list(self.resampled_cache.values())
            if self.resampled_query is not None:
                tables.append(self.resampled_query[1])
            tables += [table for _, table, _ in self.cache.values()]
            return sum(t.num_rows for t in tables), sum(t.nbytes for t in tables)
        while self.resampled_cache:
            rows, size = usage()
            if len(self.resampled_cache) <= 32 and rows <= self.cache_rows and size <= self.cache_bytes:
                break
            del self.resampled_cache[next(iter(self.resampled_cache))]
        rows, size = usage()
        if rows > self.cache_rows or size > self.cache_bytes:
            self.resampled_query = None
            raise GatewayError('RESOURCE_LIMIT', '降采样窗口超过缓存预算')
        self.stats['cache_rows'] = rows

    def _bucket(self, name, request, context, session, begin, finish, source):
        from quantfoundry import _native
        binding = self.bindings[name]
        factor = (finish - begin) // (int(self.source_frequency[:-1]) * MINUTE_NS)
        if factor * len(request['securities']) > self.cache_rows:
            raise GatewayError('RESOURCE_LIMIT', '单个降采样桶超过输入行数预算')
        raw_request = dict(request, frequency=self.source_frequency, fields=list(BAR_FIELDS),
                           start_ns=str(begin), end_ns=str(finish), count_per_security=None)
        parts, rows, size = [], 0, 0
        for batch in super().read(name, raw_request, context, page_rows=self.batch_rows):
            # A fine bar ending at begin belongs to the preceding bucket.
            table = batch.table.filter(pc.greater_equal(batch.table['interval_start_ns'], begin))
            if any(table[field].null_count for field in BAR_FIELDS):
                raise GatewayError('CAPABILITY_UNAVAILABLE', '细粒度成交价量缺失，不能降采样为可成交 Bar')
            rows += table.num_rows
            size += table.nbytes
            if rows > self.cache_rows or size > self.batch_bytes:
                raise GatewayError('RESOURCE_LIMIT', '单个降采样桶超过输入内存预算')
            if table.num_rows:
                parts.append(table)
        if not parts:
            return pa.Table.from_batches([], schema=MARKET_SCHEMA)
        table = pa.concat_tables(parts)
        self.input_rows += rows
        if source:
            self.replay_raw_rows += rows
        # This private conversion view is after the completed source boundary.
        # It has no strategy market-key cursor; all input remains <= finish.
        # The public worker view separately enforces its exact callback key.
        if finish >= 2**63 - 1:
            raise GatewayError('NUMERIC_RANGE_UNSUPPORTED', '降采样完成边界超出纳秒范围')
        started = time.monotonic()
        view = _native.ReadView(json.dumps(dict(now_ns=str(finish + 1), market_through=None)),
                                self.cache_rows, self.batch_bytes)
        try:
            for offset in range(0, rows, self.batch_rows):
                batch = self._batch(table.slice(offset, self.batch_rows), binding)
                view.push_market(json.dumps(batch.metadata), batch.ipc(self.batch_bytes))
            output = json.loads(view.prices_json(json.dumps(dict(
                request, fields=[*BAR_FIELDS, 'interval_start_ns', 'interval_end_ns', 'status', 'is_partial'],
                start_ns=str(begin), end_ns=str(finish), count_per_security=None)),
                self.source_frequency, json.dumps([session])))
        except _native.ContractError as error:
            code = getattr(error, 'code', 'INVALID_CONTRACT')
            if code not in ('INVALID_CONTRACT', 'RESOURCE_LIMIT', 'NUMERIC_RANGE_UNSUPPORTED',
                            'RULE_UNAVAILABLE', 'CAPABILITY_UNAVAILABLE'):
                code = 'INVALID_CONTRACT'
            raise GatewayError(code, '细粒度行情或已接受会话不符合 Rust 降采样契约') from None
        finally:
            view.expire()
            self.stats['parent_rust_resampling_and_conversion_seconds'] += time.monotonic() - started
        result = []
        partial = False
        for row in output:
            if row['status'] != 'observed' or row['quantity'] is None:
                raise GatewayError('CAPABILITY_UNAVAILABLE', '降采样桶缺少完整细粒度价量，不能用于成交')
            partial |= row['is_partial']
            result.append(dict(kind='bar', security=row['security'], time_ns=int(row['time_ns']),
                interval_start_ns=int(row['interval_start_ns']), interval_end_ns=int(row['interval_end_ns']),
                session=session['session']['key'], source_session=session['session']['key'],
                channel='qf-resample-' + self.source_frequency + '-' + self.target_frequency,
                sequence=begin + 2**63, stable_input_sequence=finish + 2**63,
                **{field: (int(row[field]) if field == 'quantity' else row[field]) for field in BAR_FIELDS}))
        if partial:
            # Research APIs expose is_partial. Execution must not turn a short
            # terminal bucket into a purported complete target-frequency bar.
            raise GatewayError('CAPABILITY_UNAVAILABLE', '声明会话末段不足目标周期；请使用细粒度执行频率')
        self.output_rows += len(result)
        if source:
            self.replay_hour_rows += len(result)
        self.check(context)
        return pa.Table.from_pylist(result, schema=MARKET_SCHEMA)

    def read(self, name, request, context, *, page_rows=None):
        if request.get('frequency') != self.target_frequency:
            yield from super().read(name, request, context, page_rows=page_rows)
            return
        binding, start, end, count = self._request(name, request, context)
        self.check(context)
        if count is not None and count * len(request['securities']) > self.cache_rows:
            raise GatewayError('RESOURCE_LIMIT', '降采样历史窗口超过行数预算')
        source = (count is None and start == self.grant.start_ns and end == self.grant.end_ns
                  and set(request['securities']) == set(self.grant.universe))
        key = json.dumps([name, request], sort_keys=True, separators=(',', ':'))
        parts = []
        if count is not None and self.resampled_query is not None and self.resampled_query[0] == key:
            parts = [self.resampled_query[1]]
        elif count is not None:
            parts = [self._count_window(name, request, context, start, end, count)]
        else:
            for session, begin, finish in self._buckets(start, end, count):
                self.check(context)
                table = self.resampled_cache.get(finish)
                if table is not None:
                    table = table.filter(pc.is_in(table['security'], value_set=pa.array(request['securities'], type=pa.string())))
                else:
                    table = self._bucket(name, request, context, session, begin, finish, source)
                    if source:
                        self.resampled_cache[finish] = table
                        self._trim_cache()
                for offset in range(0, table.num_rows, min(page_rows or self.batch_rows, self.batch_rows)):
                    self.check(context)
                    yield self._batch(table.slice(offset, min(page_rows or self.batch_rows, self.batch_rows)), binding)
        if count is not None:
            table = pa.concat_tables(parts) if parts else pa.Table.from_batches([], schema=MARKET_SCHEMA)
            self.resampled_query = (key, table)
            self._trim_cache()
            for offset in range(0, max(1, table.num_rows), min(page_rows or self.batch_rows, self.batch_rows)):
                self.check(context)
                yield self._batch(table.slice(offset, min(page_rows or self.batch_rows, self.batch_rows)), binding)
        self.check(context)

    def close(self, context):
        self.resampled_cache.clear()
        self.resampled_query = None
        return super().close(context)
