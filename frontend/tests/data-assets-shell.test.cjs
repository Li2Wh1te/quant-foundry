/* Exercise user navigation context and rendered semantics, without API fixtures
 * entering the application bundle or implying any production read permission. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
for (const extension of ['.ts', '.tsx']) {
  require.extensions[extension] = (module, filename) => {
    const compiled = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
      compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }, fileName: filename
    });
    module._compile(compiled.outputText, filename);
  };
}
require.extensions['.css'] = () => {};
const { createElement } = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { MemoryRouter } = require('react-router-dom');
const navigation = require('../src/features/data-assets/navigation.ts');
const { DatasetTabs, LocalNotice, DataSheet, WorkspaceHeader } = require('../src/features/data-assets/components/index.ts');

test('list context survives encoded dataset links and every detail view', () => {
  const source = new URLSearchParams('search=日频&status=restricted&source=local&frequency=daily&page=3&view=preview');
  const link = navigation.datasetHref('source/report + subject', source);
  assert.ok(link.startsWith('/admin/data-assets/source%2Freport%20%2B%20subject?'));
  for (const view of ['overview', 'preview', 'updates']) {
    const detail = navigation.viewParams(source, view);
    assert.equal(detail.get('view'), view);
    assert.equal(navigation.catalogHref(detail), navigation.catalogHref(source));
    assert.equal(detail.get('page'), '3');
    assert.equal(detail.get('source'), 'local');
    assert.equal(detail.get('frequency'), 'daily');
  }
  assert.equal(navigation.dataAssetsView('unknown'), 'overview');
  assert.equal(navigation.dataAssetsView(null), 'overview');
});

test('navigation excludes tokens, cursors, business values and unrelated parameters', () => {
  const params = new URLSearchParams('search=x&cursor=private-cursor&token=secret&subject=private-object&view=updates&data=raw');
  assert.equal(navigation.catalogHref(params), '/admin/data-assets?search=x');
  assert.equal(navigation.datasetHref('a', params), '/admin/data-assets/a?search=x');
  assert.equal(navigation.viewParams(params, 'overview').toString(), 'search=x&view=overview');
});

test('return scroll is scoped to list filters and unavailable storage remains optional', () => {
  const entries = new Map();
  const main = { scrollTop: 384 };
  global.document = { querySelector: () => main };
  global.sessionStorage = { setItem: (key, value) => entries.set(key, value), getItem: key => entries.get(key) ?? null };
  try {
    const first = new URLSearchParams('search=first&page=2');
    const second = new URLSearchParams('search=second&page=1');
    navigation.saveCatalogScroll(first);
    main.scrollTop = 0;
    navigation.restoreCatalogScroll(first);
    assert.equal(main.scrollTop, 384);
    navigation.restoreCatalogScroll(second);
    assert.equal(main.scrollTop, 0);
    global.sessionStorage = { setItem() { throw new Error('disabled'); }, getItem() { throw new Error('disabled'); } };
    assert.doesNotThrow(() => navigation.saveCatalogScroll(first));
    assert.doesNotThrow(() => navigation.restoreCatalogScroll(first));
  } finally { delete global.document; delete global.sessionStorage; }
});

test('same-object tabs expose a single keyboard entry and associated panels', () => {
  for (const view of ['overview', 'preview', 'updates']) {
    const html = renderToStaticMarkup(createElement(DatasetTabs, { value: view, onChange() {} }));
    assert.match(html, /role="tablist" aria-label="数据集视图"/);
    assert.equal((html.match(/aria-selected="true"/g) ?? []).length, 1);
    assert.equal((html.match(/tabindex="0"/g) ?? []).length, 1);
    assert.match(html, new RegExp(`id="qf-assets-tab-${view}" aria-controls="qf-assets-panel-${view}" aria-selected="true"`));
  }
});

test('local notices, title and continuous surface retain explicit accessible wording', () => {
  const error = renderToStaticMarkup(createElement(LocalNotice, { tone: 'error' }, '读取失败，请刷新页面。'));
  assert.match(error, /role="alert"/);
  assert.match(error, /读取失败，请刷新页面/);
  assert.doesNotMatch(error, /当前可用|暂无数据/);
  const warning = renderToStaticMarkup(createElement(LocalNotice, { tone: 'warning' }, '状态待确认'));
  assert.match(warning, /role="status"/);
  const html = renderToStaticMarkup(createElement(WorkspaceHeader, {
    title: '当前对象', description: '用途未声明',
    actions: createElement('button', { type: 'button' }, '刷新页面')
  }));
  assert.match(html, /<h1>当前对象<\/h1>/);
  assert.match(html, /用途未声明/);
  assert.match(renderToStaticMarkup(createElement(DataSheet, { title: '字段' }, '字段内容')), /<h2>字段<\/h2>字段内容/);
});

test('only the data-assets navigation remains active on its persistent detail route', () => {
  global.__QF_VERSION__ = '0.3.0';
  global.window = { innerWidth: 1440 };
  const { AuthProvider } = require('../src/auth/AuthContext.tsx');
  const { OverviewShell } = require('../src/components/OverviewShell.tsx');
  const render = route => renderToStaticMarkup(createElement(MemoryRouter, { initialEntries: [route] },
    createElement(AuthProvider, null, createElement(OverviewShell, null, 'content'))));
  try {
    const detail = render('/admin/data-assets/encoded.dataset?view=preview');
    assert.match(detail, /aria-label="数据资产"[^>]*aria-current="page"/);
    assert.equal((detail.match(/aria-current="page"/g) ?? []).length, 1);
    const overview = render('/admin');
    assert.match(overview, /aria-label="总览"[^>]*aria-current="page"/);
    assert.doesNotMatch(overview, /aria-label="数据资产"[^>]*aria-current="page"/);
  } finally { delete global.window; delete global.__QF_VERSION__; }
});
