const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
require.extensions['.ts'] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }, fileName: filename
}).outputText, filename);
const { checkedPlotNumber, plotBars, exactBarValue } = require('../src/features/market-data/series/plotting.ts');
const { joinBarPages, createSeriesWindow, shanghaiBusinessDate } = require('../src/features/market-data/series/window.ts');
const { MarketApiError } = require('../src/features/market-data/data/errors.ts');
const makeBar = (day, extra = {}) => ({ bucket_start: day, bucket_end: day, first_trade_date: day, last_trade_date: day,
  open: '4.123456', high: '5.000001', low: '3.000001', close: '4.999999', volume: '100.1234', amount: '1000.1234', completion: 'unknown', ...extra });
const makePage = (items, extra = {}) => ({ items, series_key: 'etf-daily-local', instrument_key: 'instrument:synthetic', period: 'day',
  next_cursor: 'earlier-token', units: { price: '元', volume: '手', amount: '千元' }, window: { start: null, end: '2026-02-08', as_of: '2026-02-08' },
  basis: { adjustment: 'none', policy: 'formal', as_of: '2026-02-08', anchor: null, note: 'current revisions' },
  meta: { read_token: 'read-one', read_at: '2026-02-08T00:00:00Z', business_as_of: '2026-02-06', source_labels: ['formal'],
    scope: 'synthetic', coverage: { verified: false, state: 'unknown', missing_dates: null, note: 'unknown' }, limitations: [] }, ...extra });

const browser = () => { global.window = { sessionStorage: { getItem: () => 'test-owner' } }; };

test('exact Decimal stays available after checked binary64 plotting, including quantity limits', () => {
  const page = makePage([makeBar('2026-02-06', { volume: '9007199254740993.1234', amount: null })]);
  const result = plotBars(page);
  assert.equal(result.error, ''); assert.equal(result.bars[0].open, 4.123456);
  assert.equal(exactBarValue(result.bars[0], 'open'), '4.123456');
  assert.equal(exactBarValue(result.bars[0], 'volume'), '9007199254740993.1234');
  assert.equal(result.bars[0].volume, undefined); assert.equal(result.volumeMissing, true);
  assert.equal(result.bars[0].turnover, undefined);
  assert.equal(checkedPlotNumber(null, 3), undefined);
  for (const value of ['1' + '0'.repeat(255), '-1', 'NaN', 'Infinity', '10000000000000.000999'])
    assert.throws(() => checkedPlotNumber(value, 3));
});
test('null or incoherent OHLC blocks the entire plot, without dropping dates or adding zero', () => {
  for (const extra of [{ open: null }, { low: '-1' }, { high: '1' }]) {
    const result = plotBars(makePage([makeBar('2026-02-05'), makeBar('2026-02-06', extra)]));
    assert.equal(result.bars.length, 0); assert.notEqual(result.error, '');
  }
  assert.equal(plotBars(makePage([makeBar('2026-02-06', { volume: '0' })])).volumeMissing, false);
});
test('business day timestamp is UTC-based in every browser timezone', () => {
  const prior = process.env.TZ;
  for (const zone of ['UTC', 'America/Los_Angeles', 'Asia/Shanghai']) {
    process.env.TZ = zone;
    assert.equal(plotBars(makePage([makeBar('2026-02-06')])).bars[0].timestamp, Date.parse('2026-02-06T00:00:00Z'));
    assert.equal(shanghaiBusinessDate(new Date('2026-02-05T23:30:00Z')), '2026-02-06');
  }
  process.env.TZ = prior;
});
test('three pages sort/deduplicate whole buckets and reject changed reads, units or fixed windows', () => {
  const initial = makePage([makeBar('2026-02-06')]);
  const second = makePage([makeBar('2026-02-05'), makeBar('2026-02-06')]);
  const third = makePage([makeBar('2026-02-04')], { next_cursor: null });
  assert.deepEqual(joinBarPages(joinBarPages(initial, second), third).items.map(x => x.last_trade_date), ['2026-02-04', '2026-02-05', '2026-02-06']);
  for (const changed of [{ meta: { ...initial.meta, read_token: 'new' } }, { period: 'week' },
    { window: { ...initial.window, end: '2026-02-09' } }, { units: { price: '美元', volume: '手', amount: '千元' } }])
    assert.throws(() => joinBarPages(initial, { ...second, ...changed }), error => error.code === 'DATA_CHANGED');
});
test('browser resident limit refuses excessive history instead of silently trimming/losing warmup', () => {
  const page = makePage(Array.from({ length: 10_000 }, (_, i) => makeBar(String(i).padStart(5, '0'))));
  assert.throws(() => joinBarPages(page, makePage([makeBar('older-date')])), error => error.code === 'QUERY_BUDGET_EXCEEDED');
});
test('fixed-condition history handles stale network data, prevents repeats, and clears quality or auth denial', async () => {
  browser(); let next = makePage([makeBar('2026-02-06')]); const calls = [];
  const window = createSeriesWindow({ bars: async (request, options) => { calls.push({ request, signal: options.signal }); if (next instanceof Error) throw next; return next; } });
  const request = { instrument_key: 'instrument:synthetic', series_key: 'etf-daily-local', period: 'day', adjustment: 'none', end: '2026-02-08', as_of: '2026-02-08', limit: 512 };
  await window.load(request);
  next = makePage([makeBar('2026-02-05')]); await window.earlier(); await window.earlier();
  assert.equal(calls.length, 2); assert.equal(calls[1].request.end, request.end); assert.ok(calls[1].signal);
  next = new MarketApiError(0, 'NETWORK_ERROR', 'network');
  await assert.rejects(window.load(request)); assert.equal(window.session.state.stale, true); assert.equal(window.session.state.canContinue, false);
  next = new MarketApiError(409, 'DATA_RESTRICTED', 'quality');
  await assert.rejects(window.load(request)); assert.equal(window.session.state.data, null);
  next = makePage([makeBar('2026-02-06')]); await window.load(request);
  next = new MarketApiError(403, 'PERMISSION_DENIED', 'denied');
  await assert.rejects(window.load(request)); assert.equal(window.session.state.data, null); window.dispose();
});
test('condition changes withdraw prior history while pending and late replies cannot win', async () => {
  browser(); let release;
  const window = createSeriesWindow({ bars: async request => request.period === 'day' ? makePage([makeBar('2026-02-06')]) : new Promise(resolve => { release = resolve; }) });
  const request = { instrument_key: 'instrument:synthetic', series_key: 'etf-daily-local', period: 'day', adjustment: 'none', end: '2026-02-08', as_of: '2026-02-08' };
  await window.load(request);
  const pending = window.load({ ...request, period: 'month' }); assert.equal(window.session.state.data, null);
  const result = window.load(request); release(makePage([makeBar('2026-01-30')], { period: 'month' }));
  await assert.rejects(pending); await result; assert.equal(window.session.state.data.items[0].last_trade_date, '2026-02-06'); window.dispose();
});

test('render cutoff gate rejects old future data before the next effect/request starts', () => {
  const { matchesBarWindow } = require('../src/features/market-data/series/window.ts');
  const page = makePage([makeBar('2026-02-06')]);
  const request = { instrument_key: page.instrument_key, series_key: page.series_key, period: 'day', adjustment: 'none', end: '2026-02-08', as_of: '2026-02-08' };
  assert.equal(matchesBarWindow(page, request), true);
  for (const change of [{ end: '2026-02-05', as_of: '2026-02-05' }, { series_key: 'other-series' }, { adjustment: 'pre' }, { period: 'week' }, { instrument_key: 'instrument:other' }, { start: '2026-02-01' }])
    assert.equal(matchesBarWindow(page, { ...request, ...change }), false);
});
