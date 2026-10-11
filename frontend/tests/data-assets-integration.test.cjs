/* Cross-module request integration against an owned HTTP fixture, not backend acceptance. */
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const { test, before, after } = require('node:test');
const ts = require('typescript');
for (const extension of ['.ts', '.tsx']) require.extensions[extension] = (module, filename) => {
  const output = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    fileName: filename, compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }
  });
  module._compile(output.outputText, filename);
};
require.extensions['.css'] = () => {};
global.__QF_VERSION__ = fs.readFileSync(require('node:path').resolve(__dirname, '../../VERSION'), 'utf8').trim();
const { dataset } = require('../src/features/data-assets/data/tests/fixtures.cjs');
const { createDataAssetsClient, createPreviewSession, buildPreviewRequest, DataStoreApiError } = require('../src/features/data-assets/data/index.ts');
const { defaultPreviewColumns, previewScopeKey, previewCellValue } = require('../src/features/data-assets/PreviewPanel.tsx');
const { catalogItems, catalogMetrics } = require('../src/features/data-assets/CatalogView.tsx');
const { createUpdateIssuesSession } = require('../src/features/data-assets/UpdateIssuesSession.ts');
const { datasetHref, viewParams, catalogHref } = require('../src/features/data-assets/navigation.ts');
const nativeFetch = global.fetch;
let server, base, mode = 'normal', size = 25;
const requests = [];
const item = index => dataset({ dataset: `fixture/source + item/${index}`, name: `隔离当前数据 ${String(index).padStart(4, '0')}`,
  frequency: 'daily', fields: [
    { column: 'object_key', type: 'string', meaning: 'current_identity' },
    { column: 'member_key', type: 'string', meaning: 'current_identity' },
    { column: 'decimal', type: 'string', meaning: 'Fixture.decimal' },
    { column: 'integer', type: 'int64', meaning: 'Fixture.integer' },
    { column: 'ns', type: 'int64', meaning: 'Fixture.nanosecond' },
    { column: 'missing', type: 'string', meaning: 'Fixture.missing' },
    { column: 'flag', type: 'bool', meaning: 'Fixture.flag' },
    { column: 'zero', type: 'int64', meaning: 'Fixture.zero' }
  ].map(field => ({ ...field, logical_type: 'typed_scalar', arithmetic: 'unsupported' })),
  last_update: { state: 'failed', complete: false, qualified: false, updated_at: null } });
function send(res, status, body) {
  res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
  res.end(JSON.stringify(body).replaceAll('"RAW_INT"', '900719925474099312345').replaceAll('"RAW_NS"', '1791619200123456789'));
}
before(async () => {
  global.window = { sessionStorage: { getItem: () => 's2-d07-public-http-fixture' } };
  server = http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://127.0.0.1');
    let raw = ''; for await (const chunk of req) raw += chunk;
    const body = raw ? JSON.parse(raw) : null;
    requests.push({ path: url.pathname, query: url.search, method: req.method, body, cache: req.headers['cache-control'] });
    if (url.pathname.endsWith('/datasets')) {
      const offset = Number(url.searchParams.get('offset')), limit = Number(url.searchParams.get('limit'));
      return send(res, 200, { items: Array.from({ length: Math.min(limit, size - offset) }, (_, i) => item(offset + i)),
        total: size, phase: 'ready', next_offset: offset + limit < size ? offset + limit : null });
    }
    if (url.pathname.includes('/datasets/')) {
      const id = decodeURIComponent(url.pathname.split('/datasets/')[1]);
      return send(res, 200, item(Number(id.split('/').at(-1))));
    }
    if (url.pathname.endsWith('/issues')) {
      if (mode === 'permission') { res.writeHead(403); res.flushHeaders(); return; }
      if (mode.startsWith('issues')) {
        const offset = Number(url.searchParams.get('offset')), limit = Number(url.searchParams.get('limit'));
        const total = mode === 'issues-changed' && offset > 0 ? 46 : 45;
        const items = Array.from({ length: Math.min(limit, total - offset) }, (_, index) => ({ dataset: url.searchParams.get('dataset'),
          scope_key: `scope-${index + offset}`, reason: 'FULL_RANGE_UNPROVEN', kind: 'current', updated_at: null, affected_objects: 2 }));
        return send(res, 200, { items, total, affected_objects: total * 2, next_offset: offset + items.length < total ? offset + items.length : null });
      }
      return send(res, 200, { items: [], total: 0, affected_objects: 0, next_offset: null });
    }
    if (url.pathname.endsWith('/query')) {
      const answer = () => send(res, 200, { dataset: body.dataset, status: 'available',
        rows: [Object.fromEntries(body.columns.map(column => [column, ({ object_key: body.from_key, member_key: body.cursor ? 'member-2' : 'member-1',
          decimal: '12345678901234567890.123456789012345678', integer: 'RAW_INT', ns: 'RAW_NS', missing: null, flag: false, zero: 0 })[column]]))],
        next_cursor: body.cursor ? null : 'fixture-only-cursor', generation: mode === 'changed' ? 2 : 1,
        request_satisfied: false, business_date_coverage_verified: false, partial_requested: false,
        actual_range: { from: body.from_key, to: body.to_key }, selected_partitions: [], limitations: [] });
      if (body.subject === 'slow-fixture') {
        const timer = setTimeout(answer, 200);
        res.once('close', () => clearTimeout(timer));
        return;
      }
      return answer();
    }
    send(res, 404, {});
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  base = `http://127.0.0.1:${server.address().port}`;
  global.fetch = (resource, options) => nativeFetch(new URL(resource, base), options);
});
after(async () => { global.fetch = nativeFetch; delete global.window; server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); });

test('catalog selection, encoded detail, D05 defaults and exact pages use one bounded current client', async () => {
  mode = 'normal'; size = 25; requests.length = 0;
  const client = createDataAssetsClient();
  const snapshot = await client.loadCatalog();
  assert.deepEqual(catalogMetrics(snapshot), ['25', '25', '0', '0']);
  const params = new URLSearchParams('search=0003&status=available&source=all&frequency=daily&page=1&cursor=bad&token=bad');
  const [selected] = catalogItems(snapshot.items, params);
  const href = datasetHref(selected.dataset, params);
  assert.match(href, /fixture%2Fsource%20%2B%20item%2F3/);
  assert.equal(new URL(href, base).searchParams.has('cursor'), false);
  assert.equal(catalogHref(viewParams(params, 'preview')), catalogHref(params));
  const detail = await client.getDataset(selected.dataset);
  assert.equal(detail.status, 'available'); assert.equal(detail.last_update.state, 'failed');
  const body = buildPreviewRequest(detail, { columns: defaultPreviewColumns(detail), pageSize: 20 });
  assert.equal(body.representation, detail.preview_key.representation);
  assert.notEqual(body.representation, detail.representation);
  const session = createPreviewSession(client);
  try {
    const first = await session.read(body);
    assert.equal(previewCellValue(first.rows[0].decimal), '12345678901234567890.123456789012345678');
    assert.equal(previewCellValue(first.rows[0].integer), '900719925474099312345');
    assert.equal(previewCellValue(first.rows[0].ns), '1791619200123456789');
    assert.equal(previewCellValue(first.rows[0].missing), '—');
    assert.equal(previewCellValue(first.rows[0].flag), 'false'); assert.equal(previewCellValue(first.rows[0].zero), '0');
    const second = await session.read(body, { page: 1 });
    assert.equal(second.rows[0].member_key, 'member-2');
    assert.equal(session.getSnapshot().result.rows.length, 1);
    assert.equal(second.request_satisfied, false); assert.equal(second.business_date_coverage_verified, false);
    assert.equal(requests.filter(r => r.method === 'POST').length, 2);
    for (const request of requests.filter(r => r.body)) { assert.equal(request.body.allow_partial, false); assert.ok(request.body.page_size <= 100); }
    for (const request of requests) assert.match(request.cache, /no-cache/);
  } finally { session.dispose(); }
});

test('later-page generation change withdraws the D05 result and cursor stack', async () => {
  mode = 'normal'; const session = createPreviewSession(), detail = item(0);
  const body = buildPreviewRequest(detail, { columns: defaultPreviewColumns(detail) });
  try {
    await session.read(body); mode = 'changed';
    await assert.rejects(session.read(body, { page: 1 }), e => e instanceof DataStoreApiError && e.code === 'DATA_CHANGED');
    assert.equal(session.getSnapshot().result, null); assert.deepEqual(session.getSnapshot().cursors, [null]);
  } finally { session.dispose(); mode = 'normal'; }
});

test('an issue HTTP denial with no completed body revokes an in-flight preview across panels', async () => {
  mode = 'normal'; const session = createPreviewSession(), detail = item(0), client = createDataAssetsClient();
  const body = buildPreviewRequest(detail, { columns: defaultPreviewColumns(detail) });
  try {
    await session.read(body);
    const pending = session.read({ ...body, subject: 'slow-fixture' });
    const settled = Promise.allSettled([pending]);
    mode = 'permission';
    await assert.rejects(client.listIssues(detail.dataset), e => e.status === 403);
    assert.equal((await settled)[0].status, 'rejected');
    assert.equal(session.getSnapshot().result, null); assert.deepEqual(session.getSnapshot().cursors, [null]);
  } finally { session.dispose(); mode = 'normal'; }
});

test('changing conditions cancels the old HTTP read before a new object can populate its session', async () => {
  mode = 'normal'; const session = createPreviewSession(), detail = item(0);
  const body = buildPreviewRequest(detail, { columns: defaultPreviewColumns(detail) });
  try {
    const pending = session.read({ ...body, subject: 'slow-fixture' });
    const settled = Promise.allSettled([pending]);
    const next = await session.read({ ...body, subject: 'new-fixture' });
    assert.equal((await settled)[0].status, 'rejected');
    assert.equal(session.getSnapshot().result.rows[0].member_key, next.rows[0].member_key);
    assert.equal(session.getSnapshot().page, 0);
    assert.notEqual(previewScopeKey(detail), previewScopeKey({ ...detail, schema_id: 'next' }));
  } finally { session.dispose(); }
});

test('D01 metadata budget remains visibly incomplete in D03 and does not claim global counts', async () => {
  mode = 'normal'; size = 1101; requests.length = 0;
  try {
    const snapshot = await createDataAssetsClient().loadCatalog();
    assert.equal(snapshot.complete, false); assert.equal(snapshot.incompleteReason, 'budget');
    assert.equal(snapshot.items.length, 1000); assert.equal(snapshot.total, 1101); assert.equal(snapshot.counts, null);
    assert.deepEqual(catalogMetrics(snapshot), ['待加载', '待加载', '待加载', '待加载']);
    assert.equal(requests.length, 10);
  } finally { size = 25; }
});

test('D06 and D05 independent page stacks coexist; an issue inventory change clears only that stack', async () => {
  mode = 'issues'; const detail = item(0), issues = createUpdateIssuesSession(detail.dataset), preview = createPreviewSession();
  try {
    await preview.read(buildPreviewRequest(detail, { columns: defaultPreviewColumns(detail) }));
    await issues.read(); await issues.read(1);
    assert.equal(issues.getSnapshot().page, 1); assert.equal(issues.getSnapshot().result.items[0].scope_key, 'scope-20');
    assert.equal(issues.getSnapshot().result.total, 45); assert.equal(issues.getSnapshot().result.affected_objects, 90);
    mode = 'issues-changed'; await assert.rejects(issues.read(2), e => e.code === 'DATA_CHANGED');
    assert.equal(issues.getSnapshot().result, null); assert.ok(preview.getSnapshot().result);
  } finally { issues.dispose(); preview.dispose(); mode = 'normal'; }
});

test('a single HTTP denial revokes both D05 preview and D06 problem sessions', async () => {
  mode = 'issues'; const detail = item(0), issues = createUpdateIssuesSession(detail.dataset), preview = createPreviewSession();
  try {
    await issues.read(); await preview.read(buildPreviewRequest(detail, { columns: defaultPreviewColumns(detail) }));
    mode = 'permission'; await assert.rejects(createDataAssetsClient().listIssues(detail.dataset), e => e.status === 403);
    assert.equal(issues.getSnapshot().result, null); assert.equal(preview.getSnapshot().result, null);
    assert.equal(issues.getSnapshot().page, 0); assert.deepEqual(preview.getSnapshot().cursors, [null]);
  } finally { issues.dispose(); preview.dispose(); mode = 'normal'; }
});
