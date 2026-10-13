const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
require.extensions['.ts'] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }, fileName: filename
}).outputText, filename);
const { joinFactorPages, createFactorWindow, defaultFactorStart } = require('../src/features/market-data/basis/window.ts');
const { MarketApiError } = require('../src/features/market-data/data/errors.ts');
const { createMarketClient } = require('../src/features/market-data/data/client.ts');
const { createSeriesWindow } = require('../src/features/market-data/series/window.ts');
const row = (date, factor = '2.123456789123') => ({ trade_date: date, factor });
const page = (items, extra = {}) => ({ items, next_cursor: 'older', basis: { adjustment: 'none', policy: 'observations',
  as_of: '2026-02-06', anchor: null, note: 'no normalization proof' }, meta: { read_token: 'a'.repeat(64), read_at: '2026-02-06T00:00:00Z',
  business_as_of: '2026-02-06', scope: 'synthetic', source_labels: ['synthetic'],
  coverage: { verified: false, state: 'unknown', missing_dates: null, note: 'unknown' }, limitations: [] }, ...extra });
const request = { instrument_key: 'instrument:synthetic', series_key: 'etf-daily-local', start: '2026-01-01', end: '2026-02-06', as_of: '2026-02-06', limit: 512 };

test('factor pages preserve exact text and reject revised bases, conflicting dates or residency overflow', () => {
  const a = page([row('2026-02-06')]), b = page([row('2026-01-02'), row('2026-02-06')]);
  const joined = joinFactorPages(a, b);
  assert.deepEqual(joined.items.map(r => r.trade_date), ['2026-01-02', '2026-02-06']);
  assert.equal(joined.items[0].factor, '2.123456789123');
  for (const changed of [page(b.items, { meta: { ...b.meta, read_token: 'b'.repeat(64) } }),
    page([row('2026-02-06', '3')]), page(b.items, { basis: { ...b.basis, as_of: '2026-02-05' } })])
    assert.throws(() => joinFactorPages(a, changed), e => e.code === 'DATA_CHANGED');
  const many = Array.from({ length: 10001 }, (_, i) => row(String(i).padStart(5, '0')));
  assert.throws(() => joinFactorPages(page([]), page(many)), e => e.code === 'QUERY_BUDGET_EXCEEDED');
});

test('factor session suppresses repeated cursors, marks network stale and clears on changed data', async () => {
  global.window = { sessionStorage: { getItem: () => 'synthetic-owner' } };
  let reads = 0, error;
  const client = { factors: async req => { reads++; assert.ok(req.signal); if (error) throw error;
    return page([row(req.cursor ? '2026-01-02' : '2026-02-06')]); } };
  const view = createFactorWindow(client);
  await view.load(request, 'none'); await view.earlier(); await view.earlier(); assert.equal(reads, 2);
  error = new MarketApiError(503, 'NETWORK_ERROR', 'network');
  await assert.rejects(view.load(request, 'none')); assert.equal(view.session.state.stale, true);
  assert.equal(view.session.state.canContinue, false); await view.earlier(); assert.equal(reads, 3);
  error = new MarketApiError(409, 'DATA_CHANGED', 'changed');
  await assert.rejects(view.load(request, 'none')); assert.equal(view.session.state.data, null); view.dispose();
});

test('cutoff or basis change removes old factors before a delayed response; UTC default bound is stable', async () => {
  global.window = { sessionStorage: { getItem: () => 'synthetic-owner' } };
  let resolve;
  const view = createFactorWindow({ factors: async () => page([row('2026-02-06')]) });
  await view.load(request, 'none');
  const other = createFactorWindow({ factors: () => new Promise(r => { resolve = r; }) });
  const pending = other.load(request, 'pre'); assert.equal(other.session.state.data, null);
  resolve(page([row('2026-02-06')])); await pending; other.dispose(); view.dispose();
  assert.equal(defaultFactorStart('2026-02-06'), '2025-02-06');
});

test('typed factor client binds fixed as_of and rejects response/request cutoff mismatch', async () => {
  let path;
  const client = createMarketClient(async url => { path = url; return page([row('2026-02-06')]); });
  await client.factors(request); assert.match(path, /as_of=2026-02-06/);
  await assert.rejects(client.factors({ ...request, as_of: '2026-02-05' }), e => e.code === 'INVALID_REQUEST');
  const changed = createMarketClient(async () => page([], { basis: { ...page([]).basis, as_of: '2026-02-07' } }));
  await assert.rejects(changed.factors(request), e => e.code === 'INVALID_RESPONSE');
});

test('series capability request binds the current display cutoff without altering identity', async () => {
  let path;
  const client = createMarketClient(async url => { path = url; return []; });
  await client.series(request.instrument_key, { as_of: '2026-02-06' });
  assert.match(path, /series\?as_of=2026-02-06$/);
  await assert.rejects(client.series(request.instrument_key, { as_of: 'yesterday' }), e => e.code === 'INVALID_REQUEST');
});

test('factor failure withdraws dependent adjusted windows and stops stale paging; raw stays independent', async () => {
  global.window = { sessionStorage: { getItem: () => 'synthetic-owner' } };
  const bars = { items: [], next_cursor: 'older' };
  const view = createSeriesWindow({ bars: async () => bars });
  await view.load({ ...request, period: 'day', adjustment: 'pre' });
  await view.factorFailed(new MarketApiError(503, 'NETWORK_ERROR', 'network'));
  assert.equal(view.session.state.data, bars);
  assert.equal(view.session.state.stale, true);
  assert.equal(view.session.state.canContinue, false);
  const withdrawn = view.factorFailed(new MarketApiError(409, 'DATA_CHANGED', 'changed'));
  assert.equal(view.session.state.data, null); // No delayed-response frame with old prices.
  await withdrawn;
  assert.equal(view.session.state.error.code, 'DATA_CHANGED');
  await view.load({ ...request, period: 'day', adjustment: 'none' });
  await view.factorFailed(new MarketApiError(409, 'DATA_RESTRICTED', 'quality'));
  assert.equal(view.session.state.data, bars);
  assert.equal(view.session.state.stale, false);
  view.dispose();
});
