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
  assert.deepEqual(restoreCatalogState({ query:' price ', api:'get_price', type:'data', tab:'example', example:0 }),
    { query:' price ', api:'get_price', type:'data', tab:'example', example:0 });
  for (const example of [-1, 2, Infinity, '1', null]) assert.equal(restoreCatalogState({api:'get_price',example}).example,0);
});
test('search and type filters intersect without changing the selected document', () => {
  assert.deepEqual(filterCatalog('  GET_PRICE  ','all').map(item=>item.id),['get_price']);
  assert.deepEqual(filterCatalog('context.clock','data'),[]);
  assert.deepEqual(filterCatalog('initialize','clock').map(item=>item.id),['callbacks','scheduling']);
  assert.deepEqual(filterCatalog('','context').map(item=>item.id),['context','record']);
  assert.equal(filterCatalog('','all').length,10);
});
test('documents describe strict runtime limits rather than prototype-only promises', () => {
  const price=API_ENTRIES.find(item=>item.id==='get_price'), orders=API_ENTRIES.find(item=>item.id==='orders');
  assert.match(JSON.stringify(price.params),/恰好选择一个/);
  assert.match(JSON.stringify(price.boundary),/LOOKAHEAD_FORBIDDEN/);
  assert.match(JSON.stringify(price.returns),/Decimal/);
  assert.match(JSON.stringify(orders.notes),/先等卖出成交/);
  assert.match(JSON.stringify(orders.returns),/effective_quantity/);
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
  assert.match(html,/get_price\(securities/);
  assert.match(html,/quantfoundry\.api/);
  assert.doesNotMatch(html,/context\.data\.daily_bars\(\)/);
});
