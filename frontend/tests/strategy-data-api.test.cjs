const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }, fileName: filename
}).outputText, filename);
require.extensions['.css'] = () => {};
const { API_ENTRIES, DEFAULT_STATE, filterCatalog, restoreCatalogState } = require('../src/pages/strategyData/catalog.ts');
const { StrategyDataApiPage } = require('../src/pages/StrategyDataApiPage.tsx');
const { createElement } = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { MemoryRouter } = require('react-router-dom');

// Restoring untrusted or old browser state must never select a nonexistent
// method, invalid tab or missing example after the catalog evolves.
test('catalog recovers from corrupt and obsolete workspace state', () => {
  for (const value of [null, 2, 'old', [], { api:'daily_bars', tab:'removed', type:'invalid', example:99 }]) {
    assert.deepEqual(restoreCatalogState(value), DEFAULT_STATE);
  }
  assert.deepEqual(restoreCatalogState({ query:' bars ', api:'bars', type:'data', tab:'example', example:1 }),
    { query:' bars ', api:'bars', type:'data', tab:'example', example:1 });
  for (const example of [-1, 2, Infinity, '1', null]) assert.equal(restoreCatalogState({api:'bars',example}).example,0);
});
test('search and type filters intersect without changing the selected document', () => {
  assert.deepEqual(filterCatalog('  BARS  ','all').map(item=>item.id),['bars']);
  assert.deepEqual(filterCatalog('context.clock','data'),[]);
  assert.deepEqual(filterCatalog('时间','clock').map(item=>item.id),['now']);
  assert.deepEqual(filterCatalog('','context').map(item=>item.id),['session_date','data_cutoff']);
  assert.equal(filterCatalog('','all').length,7);
});
test('documents describe strict runtime limits rather than prototype-only promises', () => {
  const bars=API_ENTRIES.find(item=>item.id==='bars'), factors=API_ENTRIES.find(item=>item.id==='adjusted_series');
  assert.match(JSON.stringify(bars.params),/两个 date 必须同时提供/);
  assert.match(JSON.stringify(bars.boundary),/history_incomplete/);
  assert.match(JSON.stringify(bars.returns),/volume/);
  assert.match(JSON.stringify(factors.notes),/AdjustmentNotActiveError/);
  assert.match(JSON.stringify(factors.params),/不支持 raw/);
  for(const entry of API_ENTRIES) for(const example of entry.examples) {
    assert.doesNotMatch(example.code,/uuid-\d|daily_bars\(|adjustment_factors\(|eligible_etfs\(|clock\.sessions\(/);
  }
});
test('default document is accessible even when browser storage is unavailable', () => {
  const html=renderToStaticMarkup(createElement(MemoryRouter,null,createElement(StrategyDataApiPage)));
  assert.match(html,/aria-label="搜索接口"/);
  assert.match(html,/role="tablist"/);
  assert.match(html,/role="tabpanel"/);
  assert.match(html,/href="\/admin\/strategies"/);
  assert.match(html,/context\.data\.bars\(\)/);
  assert.doesNotMatch(html,/context\.data\.daily_bars\(\)/);
});
