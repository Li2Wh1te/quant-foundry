/* D03 tests use synthetic metadata and the real D01 client, without external IO. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
for (const extension of ['.ts', '.tsx']) {
  require.extensions[extension] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }, fileName: filename
  }).outputText, filename);
}
require.extensions['.css'] = () => {};
global.__QF_VERSION__ = '0.3.0';
const { createElement } = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { MemoryRouter } = require('react-router-dom');
const { catalogName, catalogPage, catalogItems, catalogOptions, catalogMetrics, retainCatalog, CatalogRow } = require('../src/features/data-assets/CatalogView.tsx');
const { createDataAssetsClient, DataStoreApiError } = require('../src/features/data-assets/data/index.ts');

function dataset(index = 1, changes = {}) {
  return { dataset: `fixture.${index}`, name: `隔离示例：日频数据 ${index}`, domain: 'fixture', source: 'fixture-local', frequency: 'daily',
    representation: 'typed-object-nodes-v2', schema_id: 'fixture-schema', rule: 'fixture-rule', fields: [], limitations: [], business_key: [],
    status: 'available', row_count: 5, generation: 1, updated_at: null, issues: 0, last_update: null, partitions: [], partitions_truncated: false,
    partition_range: { from: null, to: null, precision: 'partition' }, preview_key: null, ...changes };
}
const snapshot = (items, changes = {}) => ({ items, total: items.length, phase: 'ready', next_offset: null, complete: true,
  counts: { total: items.length, byStatus: Object.create(null) }, incompleteReason: null, readAt: '2026-10-10T10:00:00Z', error: null, ...changes });

for (const size of [0, 1, 60, 121]) {
  test(`${size} registered datasets: complete loading and sorted/filterable catalog has no fixed denominator`, async () => {
    const registered = Array.from({ length: size }, (_, i) => dataset(size - i));
    const calls = [];
    const client = createDataAssetsClient(async path => {
      const url = new URL(path, 'http://fixture.invalid');
      const offset = Number(url.searchParams.get('offset')), limit = Number(url.searchParams.get('limit'));
      calls.push({ path: url.pathname, offset, limit });
      return { items: registered.slice(offset, offset + limit), total: size, phase: 'ready', next_offset: offset + limit < size ? offset + limit : null };
    });
    const loaded = await client.loadCatalog();
    const sorted = catalogItems(loaded.items, new URLSearchParams());
    assert.equal(loaded.complete, true);
    assert.equal(sorted.length, size);
    assert.equal(new Set(sorted.map(item => item.dataset)).size, size);
    if (size) assert.equal(sorted[0].dataset, 'fixture.1');
    assert.deepEqual(catalogMetrics(loaded), [String(size), String(size), '0', '0']);
    assert.deepEqual(calls.map(call => call.offset), size > 100 ? [0, 100] : [0]);
    assert.ok(calls.every(call => call.path === '/datasets' && call.limit <= 100));
  });
}

test('Chinese aliases, original names, identifiers and full-width search share the same data identity', () => {
  const item = dataset(1, { dataset: 'market.stock_daily.reported', name: '市场 · stock_daily' });
  assert.equal(catalogName(item), '市场 · 股票日频');
  for (const search of ['股票日频', 'stock_daily', 'MARKET.STOCK_DAILY', ' ＳＴＯＣＫ＿ＤＡＩＬＹ ']) {
    assert.deepEqual(catalogItems([item], new URLSearchParams({ search })), [item]);
  }
  assert.equal(catalogName(dataset(1, { name: '用户命名的股票行情' })), '用户命名的股票行情');
  assert.equal(catalogName(dataset(1, { name: '市场 · future_native' })), '市场 · future_native');
  assert.equal(catalogName(dataset(1, { name: '市场 · constructor' })), '市场 · constructor');
});

test('stable display-name sorting uses identifiers to distinguish duplicate names without mutating input', () => {
  const items = [dataset(10, { name: '同名' }), dataset(2, { name: '同名' }), dataset(1, { name: '同名' })];
  assert.deepEqual(catalogItems(items, new URLSearchParams()).map(item => item.dataset), ['fixture.1', 'fixture.2', 'fixture.10']);
  assert.deepEqual(items.map(item => item.dataset), ['fixture.10', 'fixture.2', 'fixture.1']);
});

test('status, source and frequency intersect; unknown state does not become available', () => {
  const items = [dataset(1), dataset(2, { source: 'tushare', frequency: 'minute' }),
    dataset(3, { source: 'tushare', frequency: 'minute', status: 'future_state' }), dataset(4, { status: 'unknown' })];
  assert.deepEqual(catalogItems(items, new URLSearchParams({ status: 'available', source: 'tushare', frequency: 'minute' })).map(item => item.dataset), ['fixture.2']);
  assert.deepEqual(catalogItems(items, new URLSearchParams({ status: 'unknown' })).map(item => item.dataset), ['fixture.3', 'fixture.4']);
  assert.deepEqual(catalogItems(items, new URLSearchParams({ source: 'absent-source' })), []);
  assert.deepEqual(catalogItems(items, new URLSearchParams({ frequency: 'future_frequency' })), []);
});

test('frequency/source choices retain unknown types and disappeared URL selections', () => {
  const items = [dataset(1), dataset(2, { frequency: 'future_frequency' }), dataset(3, { frequency: 'unknown', source: 'constructor' })];
  const frequencies = catalogOptions(items, 'frequency', 'removed_frequency');
  assert.ok(frequencies.some(option => option.value === 'unknown' && option.label === '未声明'));
  assert.ok(frequencies.some(option => option.value === 'future_frequency' && option.label === '未声明（future_frequency）'));
  assert.ok(frequencies.some(option => option.value === 'removed_frequency'));
  const sources = catalogOptions(items, 'source', 'removed_source');
  assert.ok(sources.some(option => option.value === 'constructor' && option.label === 'constructor'));
  assert.ok(sources.some(option => option.value === 'removed_source'));
});

test('missing or partial metadata cannot produce whole-directory metrics', () => {
  for (const loaded of [null, snapshot([dataset()], { complete: false, counts: null, total: 200 }), snapshot([dataset()], { counts: null })]) {
    assert.deepEqual(catalogMetrics(loaded), ['待加载', '待加载', '待加载', '待加载']);
  }
  assert.deepEqual(catalogMetrics(snapshot([], { counts: { total: 9, byStatus: { available: 2, restricted: 1, rebuild_required: 4, not_checked: 2 } } })), ['9', '2', '1', '2']);
});

test('mid-load error and metadata budget preserve partial rows with pending global counts', async () => {
  for (const budget of [false, true]) {
    const client = createDataAssetsClient(async path => {
      const offset = Number(new URL(path, 'http://fixture.invalid').searchParams.get('offset'));
      if (!budget && offset) throw new DataStoreApiError(503, 'DATA_STORE_UNAVAILABLE', 'fixture failure');
      return { items: Array.from({ length: 100 }, (_, i) => dataset(offset + i)), total: 1100, phase: 'ready', next_offset: offset + 100 };
    });
    const loaded = await client.loadCatalog();
    assert.equal(loaded.complete, false);
    assert.equal(loaded.incompleteReason, budget ? 'budget' : 'failed');
    assert.equal(loaded.items.length, budget ? 1000 : 100);
    assert.deepEqual(catalogMetrics(loaded), ['待加载', '待加载', '待加载', '待加载']);
  }
});

test('failed refresh keeps old observations while first partial load remains usable', () => {
  const previous = snapshot([dataset()]);
  for (const reason of ['failed', 'changed', 'pagination']) {
    const next = snapshot([], { complete: false, counts: null, incompleteReason: reason });
    assert.equal(retainCatalog(previous, next), true);
    assert.equal(retainCatalog(null, next), false);
  }
  assert.equal(retainCatalog(previous, snapshot([], { error: new Error('fixture failure') })), true);
  assert.equal(retainCatalog(previous, snapshot([], { complete: false, counts: null, incompleteReason: 'budget' })), false);
  assert.equal(retainCatalog(previous, snapshot([])), false);
});

test('page parsing rejects unsafe/invalid values without rounding them into a real page', () => {
  for (const value of [null, '', '-1', '0', '1.5', '02', 'Infinity', '1e3', '9999999999999999999999']) assert.equal(catalogPage(value), 1);
  assert.equal(catalogPage('3'), 3);
});

test('a usable current catalog row keeps a failed update in its own cell and technical metadata collapsed', () => {
  const item = dataset(1, { last_update: { state: 'failed', updated_at: null } });
  const html = renderToStaticMarkup(createElement(MemoryRouter, null, createElement('table', null,
    createElement('tbody', null, createElement(CatalogRow, { item, params: new URLSearchParams('source=fixture-local&page=2') })))));
  assert.match(html, /当前可用（按服务端声明）/);
  assert.match(html, /本次更新失败/);
  assert.match(html, /tone-success/); assert.match(html, /tone-danger/);
  assert.match(html, /<details class="qf-catalog-technical">/);
  assert.doesNotMatch(html, /<details[^>]* open/);
  assert.match(html, /fixture\.1\?source=fixture-local&amp;page=2/);
  assert.match(html, /用途未声明/); assert.match(html, /记录时间：暂无记录/);
});

test('missing values stay unknown instead of storage zero or a fabricated update record', () => {
  const item = dataset(1, { status: 'future_state', frequency: 'future_frequency', representation: 'future_layout', row_count: null });
  const html = renderToStaticMarkup(createElement(MemoryRouter, null, createElement('table', null,
    createElement('tbody', null, createElement(CatalogRow, { item, params: new URLSearchParams() })))));
  assert.match(html, /状态待确认/); assert.match(html, /暂无更新记录/);
  assert.match(html, /future_state/); assert.match(html, /future_frequency/); assert.match(html, /future_layout/);
  assert.match(html, /qf-catalog-secondary">未声明/);
  assert.doesNotMatch(html, /当前可用|本次更新失败|记录时间：/);
});
