/* D08 synthetic contract/transport tests. No live-data or UI acceptance claim. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
require.extensions['.ts'] = (module, filename) => {
  const compiled = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }, fileName: filename
  });
  module._compile(compiled.outputText, filename);
};
const data = require('../src/features/market-data/data/index.ts');
const validation = require('../src/features/market-data/data/validation.ts');
const lifecycle = require('../src/features/data-assets/data/lifecycle.ts');
const cap = (name = 'raw') => ({ name, availability: 'available', request_checked: true,
  mapping_implemented: true, metadata_known: true, limitations: [] });
const meta = (token = 'synthetic-current-A') => ({ read_token: token, read_at: '2026-01-07T08:00:00Z',
  business_as_of: '2026-01-07', scope: 'synthetic current ETF', source_labels: ['合成正式夹具'],
  coverage: { verified: false, state: 'unknown', missing_dates: null, note: '未核验日期覆盖' }, limitations: [] });
const resolution = () => ({ instrument_key: 'instrument:00000000-0000-0000-0000-000000000001', symbol: '510300.SH', asset_type: 'etf', meta: meta() });
const bars = () => ({ items: [{ bucket_start: '2026-01-06', bucket_end: '2026-01-06', first_trade_date: '2026-01-06',
  last_trade_date: '2026-01-06', open: '10.000000000000000001', high: '11', low: '9', close: '10.5',
  volume: '9007199254740993', amount: null, completion: 'unknown' }], series_key: 'synthetic-etf-raw', instrument_key: 'synthetic-etf-001',
  period: 'day', next_cursor: null, units: { price: '元', volume: '份', amount: null },
  window: { start: '2026-01-06', end: '2026-01-07', as_of: '2026-01-07' },
  basis: { adjustment: 'none', policy: 'synthetic-raw', as_of: '2026-01-07', anchor: null, note: '合成口径' }, meta: meta() });
const barRequest = () => ({ instrument_key: 'synthetic-etf-001', series_key: 'synthetic-etf-raw', period: 'day', adjustment: 'none' });
const fail = code => error => error instanceof data.MarketApiError && error.code === code;
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
const tick = () => new Promise(resolve => setImmediate(resolve));

async function browser(fetcher, run) {
  const previousFetch = global.fetch, previousWindow = global.window;
  let token = 'synthetic-browser-token';
  global.window = { sessionStorage: { getItem: () => token } };
  global.fetch = fetcher;
  try { await run(value => { token = value; }); }
  finally { lifecycle.invalidateDataAssetsSession(); global.fetch = previousFetch; global.window = previousWindow; }
}
const json = (v, status = 200) => new Response(JSON.stringify(v), { status, headers: { 'Content-Type': 'application/json' } });

test('default client uses authenticated read-only formal URL, no-store and business DTOs', async () => {
  const requests = [];
  await browser(async (url, options) => { requests.push([url, options]); return json(resolution()); }, async () => {
    const result = await data.marketClient.resolve('510300', 'etf');
    assert.equal(result.symbol, '510300.SH'); assert.equal(result.meta.coverage.missing_dates, null);
    assert.equal(requests[0][0], '/api/admin/market/instruments/resolve?symbol=510300&asset_type=etf');
    assert.equal(requests[0][1].headers.Authorization, 'Bearer synthetic-browser-token');
    assert.equal(requests[0][1].method, 'GET'); assert.equal(requests[0][1].cache, 'no-store');
    assert.ok(requests[0][1].signal instanceof AbortSignal);
  });
});

test('capabilities distinguish mapping, metadata, request acceptance and independent adjustment limitations', () => {
  const value = { catalogs: [{ catalog_key: 'etf-current', label: '当前ETF', asset_type: 'etf', capability: cap('etf-current') }],
    capabilities: [cap(), { ...cap('pre'), availability: 'unavailable', request_checked: false,
      limitations: [{ code: 'FACTOR_ANCHOR_UNVERIFIED', message: '缺少锚点依据' }] }], read_at: meta().read_at };
  assert.equal(validation.parseCapabilities(value).capabilities[0].availability, 'available');
  assert.equal(validation.parseCapabilities(value).capabilities[1].availability, 'unavailable');
  for (const field of ['mapping_implemented', 'metadata_known', 'request_checked']) {
    const bad = structuredClone(value); delete bad.capabilities[0][field];
    assert.throws(() => validation.parseCapabilities(bad), fail('INVALID_RESPONSE'));
  }
});

test('Decimal strings, huge quantities and nulls remain exact through client and formatting', async () => {
  const client = data.createMarketClient(async () => bars());
  const result = await client.bars(barRequest());
  assert.equal(result.items[0].open, '10.000000000000000001');
  assert.equal(result.items[0].volume, '9007199254740993'); assert.equal(result.items[0].amount, null);
  assert.equal(data.exactMarketValue(result.items[0].open), '10.000000000000000001');
  assert.equal(data.exactMarketValue(null), '—');
  for (const bad of [10.000000000000000001, 9007199254740993, '1e12', 'NaN', 'Infinity', '', '01']) {
    const fixture = bars(); fixture.items[0].open = bad;
    await assert.rejects(data.createMarketClient(async () => fixture).bars(barRequest()), fail('INVALID_RESPONSE'));
  }
});

test('client rejects excess output, bad window/date, price-basis mismatch and oversize cursors', async () => {
  const changes = [v => { v.items[0].bucket_start = '2026-02-30'; }, v => { v.window.end = '2026-01-08'; },
    v => { v.items.push({ ...v.items[0] }); }, v => { v.next_cursor = 'a'.repeat(8193); },
    v => { v.basis.adjustment = 'pre'; }, v => { v.meta.coverage.missing_dates = []; },
    v => { v.items = Array.from({ length: 2001 }, () => v.items[0]); }];
  for (const change of changes) {
    const value = bars(); change(value);
    await assert.rejects(data.createMarketClient(async () => value).bars(barRequest()), fail('INVALID_RESPONSE'));
  }
  const value = bars(); value.series_key = 'different';
  await assert.rejects(data.createMarketClient(async () => value).bars(barRequest()), fail('INVALID_RESPONSE'));
});

test('business requests validate pagination and fixed as_of before transport', async () => {
  let calls = 0;
  const client = data.createMarketClient(async () => { calls++; return bars(); });
  for (const patch of [{ limit: 2001 }, { limit: 0 }, { limit: 1.5 }, { cursor: 'old-cursor' },
    { cursor: 'a'.repeat(8193), end: '2026-01-07', as_of: '2026-01-07' },
    { end: '2026-01-08', as_of: '2026-01-07' }, { period: 'minute' }, { adjustment: 'forward' }]) {
    await assert.rejects(client.bars({ ...barRequest(), ...patch }), fail('INVALID_REQUEST'));
  }
  assert.equal(calls, 0);
  await assert.rejects(client.instruments({ catalog_key: 'etf-current', limit: 201 }), fail('INVALID_REQUEST'));
  await assert.rejects(client.calendar({ exchange: 'SSE', start: '2026-01-08', end: '2026-01-07' }), fail('INVALID_REQUEST'));
});

test('catalog/calendar parsers keep unknown business facts and cannot mistake nodes for instruments', () => {
  const instrument = { instrument_key: 'synthetic-one', symbol: '510300.SH', name: null, asset_type: 'etf', exchange: null,
    list_status: null, list_date: null, index_name: null, manager_name: null, management_fee: null,
    management_fee_unit: null, capabilities: [] };
  const page = { catalog_key: 'etf-current', items: [instrument], total: null, next_cursor: null, meta: meta() };
  assert.equal(validation.parseInstruments(page).items[0].management_fee, null);
  assert.equal(validation.parseInstruments(page).total, null);
  const rawNode = { representation: 'internal', subject: '510300.SH', object_key: 'current', member_key: 'root', f0_name: 'ETF' };
  assert.throws(() => validation.parseInstruments({ ...page, items: [rawNode] }), fail('INVALID_RESPONSE'));
  const calendar = validation.parseCalendar({ items: [{ date: '2026-01-06', exchange: 'SSE', state: 'unknown',
    previous_trading_date: null, next_trading_date: null }], meta: meta() });
  assert.equal(calendar.items[0].state, 'unknown'); assert.equal(calendar.items[0].next_trading_date, null);
});

test('verified partial coverage preserves proved gaps without declaring complete coverage', () => {
  const value = bars();
  value.meta.coverage = { verified: true, state: 'partial', missing_dates: ['2026-01-07'], note: '已核验范围存在缺日' };
  const parsed = validation.parseBars(value);
  assert.equal(parsed.meta.coverage.state, 'partial');
  assert.deepEqual(parsed.meta.coverage.missing_dates, ['2026-01-07']);
});

test('401/403 invalidate page sessions immediately without reading stalled error bodies', async () => {
  for (const status of [401, 403]) {
    const session = data.createMarketReadSession();
    await session.read('same-query', async () => resolution());
    let bodyReads = 0;
    await browser(async () => ({ status, ok: false, text: () => { bodyReads++; return new Promise(() => {}); } }), async () => {
      await assert.rejects(data.marketClient.resolve('510300.SH'), fail(status === 401 ? 'AUTH_REQUIRED' : 'PERMISSION_DENIED'));
      assert.equal(session.state.data, null); assert.equal(session.state.canContinue, false); assert.equal(bodyReads, 0);
    });
    session.dispose();
  }
});

test('the originating session preserves 401/403 for the existing auth flow after clearing content', async () => {
  for (const status of [401, 403]) {
    const session = data.createMarketReadSession();
    await session.read('same-query', async () => resolution());
    await browser(async () => ({ status, ok: false }), async () => {
      await assert.rejects(session.read('same-query', signal => data.marketClient.resolve('510300.SH', 'etf', { signal })),
        fail(status === 401 ? 'AUTH_REQUIRED' : 'PERMISSION_DENIED'));
      assert.equal(session.state.data, null); assert.equal(session.state.canContinue, false);
      assert.equal(session.state.error.status, status);
    });
    session.dispose();
  }
});

test('denial from another panel fences an injected late success without erasing the denial', async () => {
  const session = data.createMarketReadSession(), late = deferred();
  const result = session.read('same', async () => late.promise);
  const cancelled = assert.rejects(result, error => error.name === 'AbortError');
  lifecycle.invalidateDataAssetsSession('permission');
  late.resolve(resolution()); await cancelled;
  assert.equal(session.state.data, null); assert.equal(session.state.error.code, 'PERMISSION_DENIED');
  assert.equal(session.state.canContinue, false); session.dispose();
});

test('credential rotation withdraws cached content and prevents stale retention under a new credential', async () => {
  await browser(async () => json(resolution()), async changeToken => {
    const session = data.createMarketReadSession();
    await session.read('same', async () => resolution());
    changeToken('new-credential');
    assert.equal(session.state.data, null);
    await assert.rejects(session.read('same', async () => { throw data.marketError(0, 'NETWORK_ERROR'); }), fail('NETWORK_ERROR'));
    assert.equal(session.state.data, null); assert.equal(session.state.stale, false);
    const late = deferred(), result = session.read('same', async () => late.promise);
    const cancelled = assert.rejects(result, error => error.name === 'AbortError');
    changeToken('another-credential'); late.resolve(resolution()); await cancelled;
    assert.equal(session.state.data, null); session.dispose();
  });
});

test('non-JSON and unknown backend errors expose no SQL, DSN, token or file path', async () => {
  for (const body of ['not JSON postgres://secret SQL /private/path', JSON.stringify({ detail: {
    code: 'SQL_PASSWORD_SECRET', message: 'Bearer confidential SELECT * FROM private', reason: '/secret' } })]) {
    await browser(async () => new Response(body, { status: 500 }), async () => {
      await assert.rejects(data.marketClient.capabilities(), error => {
        assert.equal(error.code, 'MARKET_UNAVAILABLE'); assert.doesNotMatch(error.message, /postgres|SQL|Bearer|private|secret/);
        return true;
      });
    });
  }
  await browser(async () => new Response('not JSON /private/path'), async () => {
    await assert.rejects(data.marketClient.capabilities(), fail('INVALID_RESPONSE'));
  });
});

test('streamed response budget bounds bytes even without Content-Length', async () => {
  const stream = new ReadableStream({ start(controller) { controller.enqueue(new Uint8Array(4 * 1024 * 1024 + 1)); controller.close(); } });
  await browser(async () => new Response(stream), async () => {
    await assert.rejects(data.marketClient.capabilities(), fail('INVALID_RESPONSE'));
  });
});

test('request cancellation, logout and token changes fence fetches that ignore AbortSignal', async () => {
  for (const cancel of ['abort', 'logout', 'token']) {
    const late = deferred(), controller = new AbortController();
    await browser(async () => late.promise, async changeToken => {
      const pending = data.marketClient.resolve('510300.SH', 'etf', { signal: controller.signal });
      const rejection = assert.rejects(pending, error => error.name === 'AbortError');
      if (cancel === 'abort') controller.abort();
      if (cancel === 'logout') lifecycle.invalidateDataAssetsSession();
      if (cancel === 'token') changeToken('rotated');
      late.resolve(json(resolution())); await rejection;
    });
  }
});

test('timeout is bounded and stale retention never enables further pagination', async () => {
  await browser(async () => new Promise(() => {}), async () => {
    await assert.rejects(data.marketClient.capabilities({ timeoutMs: 5 }), fail('QUERY_TIMEOUT'));
  });
  const session = data.createMarketReadSession();
  await session.read('same', async () => resolution());
  await assert.rejects(session.read('same', async () => { throw data.marketError(0, 'NETWORK_ERROR'); }), fail('NETWORK_ERROR'));
  assert.equal(session.state.stale, true); assert.ok(session.state.data); assert.equal(session.state.canContinue, false);
  await assert.rejects(session.read('different', async () => { throw data.marketError(0, 'NETWORK_ERROR'); }), fail('NETWORK_ERROR'));
  assert.equal(session.state.data, null); assert.equal(session.state.stale, false); session.dispose();
});

test('permissions, quality, schema, data/cursor changes and unknown errors withdraw old results', async () => {
  for (const [status, code] of [[403, 'NETWORK_ERROR'], [409, 'DATA_CHANGED'], [409, 'DATA_RESTRICTED'],
    [422, 'INVALID_CURSOR'], [409, 'REBUILD_REQUIRED'], [503, 'DATA_STORE_REBUILDING'], [500, 'UNKNOWN_BACKEND_CODE']]) {
    const session = data.createMarketReadSession();
    await session.read('same', async () => bars());
    await assert.rejects(session.read('same', async () => { throw data.marketError(status, code); }));
    assert.equal(session.state.data, null); assert.equal(session.state.stale, false);
    assert.equal(session.state.canContinue, false); session.dispose();
  }
});

test('late reads, unmount and cancellation cannot republish or persist old page data', async () => {
  const session = data.createMarketReadSession(), first = deferred();
  const pending = session.read('first', async () => first.promise);
  const cancelled = assert.rejects(pending, error => error.name === 'AbortError');
  await session.read('second', async () => resolution());
  await cancelled; first.resolve(bars()); await tick();
  assert.equal(session.state.data.symbol, '510300.SH');
  const delayed = deferred(), result = session.read('third', async () => delayed.promise);
  const disposed = assert.rejects(result, error => error.name === 'AbortError');
  session.dispose(); await disposed; delayed.resolve(bars()); await tick();
  assert.equal(session.state.data, null); assert.equal(session.state.pending, false);
  await assert.rejects(session.read('after', async () => bars()), error => error.name === 'AbortError');
});
