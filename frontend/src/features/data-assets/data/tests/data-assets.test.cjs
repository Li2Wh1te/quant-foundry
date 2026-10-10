/* Run explicitly with node --test; file ownership keeps these tests inside D01. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
require.extensions['.ts'] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }, fileName: filename
}).outputText, filename);
const data = require('../index.ts');
const validation = require('../validation.ts');
const { dataStoreApi } = require('../../../../api/dataStore.ts');
const { dataset, catalog, preview, scenarios } = require('./fixtures.cjs');
const response = (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } });
const request = () => data.buildPreviewRequest(dataset());
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
async function withHttp(fetch, work) {
  const previousFetch = global.fetch, previousWindow = global.window;
  let token = 'isolated-auth';
  global.fetch = fetch;
  global.window = { sessionStorage: { getItem: () => token } };
  try { return await work({ setToken: value => { token = value; } }); }
  finally { data.invalidateDataAssetsSession(); global.fetch = previousFetch; global.window = previousWindow; }
}
const invalid = field => error => error instanceof data.DataStoreApiError && error.code === 'INVALID_RESPONSE' && (!field || error.field === field);

test('all fixture current states remain distinct from the most recent update result', () => {
  for (const [name, label] of [['normal', '当前可用（按服务端声明）'], ['empty', '当前为空'], ['notChecked', '尚未检查'],
    ['restricted', '存在限制'], ['maintenance', '暂不可读取／处理中'], ['unknown', '状态待确认']]) {
    assert.equal(data.currentStatus(validation.parseDataset(scenarios[name]).status).label, label);
  }
  const display = data.datasetPresentation(scenarios.updateFailedWithCurrent);
  assert.equal(display.current.label, '当前可用（按服务端声明）');
  assert.equal(display.update.label, '本次更新失败');
  assert.equal(data.updateStatus(null).label, '暂无更新记录');
  for (const state of ['backoff', 'deferred', 'running', 'incomplete', 'processed']) {
    assert.equal(data.updateStatus({ ...scenarios.updateFailedWithCurrent.last_update, state }).known, true);
  }
});

test('unknown/missing/null/prototype-named states never become available or zero', () => {
  for (const status of [null, undefined, '', 'unexpected', '__proto__', 'constructor']) {
    const item = validation.parseDataset(dataset({ status, row_count: null }));
    assert.equal(data.currentStatus(item.status).label, '状态待确认');
    assert.equal(data.currentStatus(item.status).known, false);
    assert.equal(item.row_count, null);
    assert.equal(data.formatCount(item.row_count), '未声明');
  }
  assert.equal(data.frequencyLabel('__proto__'), '未声明');
});

test('malformed identifiers, arrays, nullable fields and counts have safe field locators', () => {
  for (const row_count of [-1, 1.5, false, '5', NaN, Infinity, Number.MAX_SAFE_INTEGER + 1]) {
    assert.throws(() => validation.parseDataset(dataset({ row_count })), invalid('dataset.row_count'));
  }
  assert.throws(() => validation.parseDataset(dataset({ dataset: '' })), invalid('dataset.dataset'));
  assert.throws(() => validation.parseDataset(dataset({ fields: {} })), invalid('dataset.fields'));
  assert.throws(() => validation.parseDataset(dataset({ updated_at: 123 })), invalid('dataset.updated_at'));
  assert.throws(() => validation.parseDatasetList(catalog([], { total: null })), invalid('datasets.total'));
  assert.throws(() => validation.parseDatasetList(catalog([], { next_offset: -1 })), invalid('datasets.next_offset'));
  assert.throws(() => validation.parsePreview(preview({ next_cursor: {} })), invalid('preview.next_cursor'));
  assert.equal(validation.parseDataset(dataset({ additive_server_field: 'accepted' })).dataset, dataset().dataset);
});

test('issues retain total records and affected members independently; missing members stay unknown', () => {
  const input = { items: [{ dataset: dataset().dataset, scope_key: null, reason: 'REPORT_INCOMPLETE', kind: 'current', updated_at: null, affected_objects: 7 }],
    total: 2, affected_objects: 12, next_offset: 1 };
  const result = validation.parseIssueList(input);
  assert.equal(result.total, 2); assert.equal(result.affected_objects, 12); assert.equal(result.items[0].affected_objects, 7);
  assert.equal(validation.parseIssueList({ ...input, affected_objects: undefined }).affected_objects, null);
  assert.throws(() => validation.parseIssueList({ ...input, affected_objects: -1 }), invalid('issues.affected_objects'));
});

test('precise strings and raw JSON numeric tokens survive without Number/Date rounding', () => {
  const exact = validation.parsePreview(preview()).rows[0];
  for (const column of ['exact_decimal', 'large_integer', 'timestamp_ns']) assert.equal(data.formatValue(exact[column]), exact[column]);
  assert.equal(data.formatTimestamp(dataset().updated_at), '2026-10-10T07:00:00.123456789Z');
  assert.equal(data.formatTimestamp(exact.timestamp_ns), exact.timestamp_ns);
  assert.equal(data.formatCount('9223372036854775807'), '9,223,372,036,854,775,807');
  assert.equal(data.formatCount(0), '0'); assert.equal(data.formatValue(null), '未声明');
  const raw = validation.parseExactJson('{"n":9223372036854775807,"d":0.1234567890123456789,"e":1e100,"safe":100,"quoted":"\\\"9223372036854775807\\\""}');
  assert.deepEqual(raw, { n: '9223372036854775807', d: '0.1234567890123456789', e: '1e100', safe: 100, quoted: '"9223372036854775807"' });
  assert.throws(() => validation.parsePreview(preview({ rows: [{ bad: Number.MAX_SAFE_INTEGER + 1 }] })), invalid('preview.rows[0].bad'));
});

test('display fields never infer units, coverage, object totals or history from storage metadata', () => {
  const display = data.datasetPresentation(dataset());
  assert.equal(display.physicalRowLabel, '当前物理存储行'); assert.equal(display.partitionLabel, '存储分区范围');
  for (const key of ['units', 'priceBasis', 'businessDateCoverage', 'historicalCapability']) assert.equal(display[key], '未声明');
  assert.equal(display.name, dataset().name); assert.equal(display.source, dataset().source);
  assert.equal(data.issueReason('REPORT_INCOMPLETE').label, '报告内容不完整');
  assert.equal(data.issueReason('FUTURE_REASON').label, '原因待确认');
  assert.equal(data.issueReason('BEARER_SECRET').diagnosticCode, null);
});

test('preview builder uses the actual source representation and bounded safe read controls', () => {
  const built = request();
  assert.equal(built.representation, dataset().preview_key.representation);
  assert.notEqual(built.representation, dataset().representation);
  assert.equal(built.from_key, dataset().preview_key.object_key); assert.equal(built.to_key, built.from_key);
  assert.equal(built.allow_partial, false); assert.equal(built.cursor, null);
  assert.throws(() => data.buildPreviewRequest(dataset({ preview_key: null })), error => error.code === 'PREVIEW_UNDECLARED');
  for (const change of [{ page_size: 101 }, { columns: Array.from({ length: 33 }, (_, i) => 'field' + i) },
    { allow_partial: true }, { from_key: 'z', to_key: 'a' }, { cursor: '' }]) {
    assert.throws(() => validation.validatePreviewRequest({ ...built, ...change }), error => error.code === 'INVALID_REQUEST');
  }
});

test('typed requests encode dataset paths, preserve auth, avoid caching and validate every endpoint', async () => {
  const calls = [];
  await withHttp(async (url, options) => {
    calls.push({ url, options });
    if (url.includes('/datasets?')) return response(catalog());
    if (url.includes('/datasets/')) return response(dataset());
    if (url.includes('/status?')) return response({ items: [], total: 0, phase: 'ready', code: 'READY', fresh_install: false, next_offset: null });
    if (url.includes('/issues?')) return response({ items: [], total: 0, affected_objects: 0, next_offset: null });
    return response(preview());
  }, async () => {
    const client = data.dataAssetsClient;
    await client.listDatasets({ limit: 100, offset: 0 }); await client.getDataset(dataset().dataset);
    await client.listStatus({ state: 'failed' }); await client.listIssues(dataset().dataset, { limit: 20, offset: 20 });
    const result = await client.queryPreview(request());
    assert.equal(result.request_satisfied, false); assert.equal(result.business_date_coverage_verified, false);
    assert.equal(calls[1].url, '/api/admin/data-store/datasets/isolated%2Fsource%2Fexample');
    const query = new URL(calls[3].url, 'https://isolated.invalid').searchParams;
    assert.equal(query.get('dataset'), dataset().dataset); assert.equal(query.get('offset'), '20');
    assert.equal(new URL(calls[2].url, 'https://isolated.invalid').searchParams.get('state'), 'failed');
    for (const call of calls) { assert.equal(call.options.cache, 'no-store'); assert.equal(call.options.headers.Authorization, 'Bearer isolated-auth'); }
    assert.equal(JSON.parse(calls[4].options.body).allow_partial, false);
  });
});

test('wrong response identity and malformed HTTP 200 are rejected', async () => {
  await withHttp(async () => response(dataset({ dataset: 'another-dataset' })), async () => {
    await assert.rejects(data.dataAssetsClient.getDataset(dataset().dataset), invalid('dataset.dataset'));
  });
  await withHttp(async () => response({ items: null, total: 0 }), async () => {
    await assert.rejects(data.dataAssetsClient.listDatasets(), invalid('datasets.items'));
  });
  await withHttp(async () => new Response('not-json'), async () => {
    await assert.rejects(data.dataAssetsClient.listDatasets(), error => error.code === 'INVALID_RESPONSE');
  });
});

test('catalog follows next_offset beyond 100 items and only then exposes full counts', async () => {
  const items = Array.from({ length: 205 }, (_, i) => dataset({ dataset: 'isolated/item-' + i, status: i % 2 ? 'not_checked' : 'available' }));
  const calls = [];
  const client = data.createDataAssetsClient(async path => {
    const params = new URL(path, 'https://isolated.invalid').searchParams;
    const offset = Number(params.get('offset')), limit = Number(params.get('limit')); calls.push(offset);
    return catalog(items.slice(offset, offset + limit), { total: items.length, next_offset: offset + limit < items.length ? offset + limit : null });
  });
  const result = await client.loadCatalog();
  assert.deepEqual(calls, [0, 100, 200]); assert.equal(result.complete, true); assert.equal(result.items.length, 205);
  assert.equal(result.counts.total, 205); assert.equal(result.counts.byStatus.available, 103); assert.equal(result.counts.byStatus.not_checked, 102);
  assert.equal(result.error, null); assert.ok(result.readAt);
});

test('first-page failures are errors; mid-catalog failures retain explicit incomplete metadata', async () => {
  const failure = new data.DataStoreApiError(503, 'CATALOG_UNAVAILABLE', 'safe fixture failure');
  const first = data.createDataAssetsClient(async () => { throw failure; });
  await assert.rejects(first.loadCatalog(), error => error === failure);
  let calls = 0;
  const partial = data.createDataAssetsClient(async () => { if (calls++) throw failure; return scenarios.incompleteCatalog; });
  const result = await partial.loadCatalog();
  assert.equal(result.complete, false); assert.equal(result.incompleteReason, 'failed'); assert.equal(result.items.length, 1);
  assert.equal(result.total, 101); assert.equal(result.counts, null); assert.equal(result.error, failure); assert.equal(result.next_offset, 1);
});

test('repeated offsets, duplicate identities, premature last page and changing totals stop safely', async () => {
  for (const next of [0, 2, null]) {
    let calls = 0;
    const client = data.createDataAssetsClient(async () => { calls++; return catalog([dataset()], { total: 4, next_offset: next }); });
    const result = await client.loadCatalog();
    assert.equal(calls, 1); assert.equal(result.complete, false); assert.equal(result.incompleteReason, 'pagination'); assert.equal(result.counts, null);
  }
  let calls = 0;
  const duplicate = data.createDataAssetsClient(async () => catalog([dataset()], { total: 4, next_offset: ++calls }));
  const duplicateResult = await duplicate.loadCatalog();
  assert.equal(calls, 2); assert.equal(duplicateResult.items.length, 1); assert.equal(duplicateResult.counts, null);
  calls = 0;
  const changed = data.createDataAssetsClient(async () => catalog([dataset({ dataset: 'isolated/item-' + calls })], { total: ++calls === 1 ? 4 : 5, next_offset: calls }));
  const changedResult = await changed.loadCatalog();
  assert.equal(changedResult.incompleteReason, 'changed'); assert.equal(changedResult.items.length, 1);
});

test('metadata loading never exceeds ten pages / one thousand items; final exact budget can be complete', async () => {
  for (const total of [1000, 1001]) {
    let calls = 0;
    const client = data.createDataAssetsClient(async path => {
      const offset = Number(new URL(path, 'https://isolated.invalid').searchParams.get('offset')); calls++;
      return catalog(Array.from({ length: 100 }, (_, i) => dataset({ dataset: 'isolated/item-' + (offset + i) })),
        { total, next_offset: offset + 100 < total ? offset + 100 : null });
    });
    const result = await client.loadCatalog();
    assert.equal(calls, 10); assert.equal(result.items.length, 1000); assert.equal(result.complete, total === 1000);
    assert.equal(result.incompleteReason, total === 1000 ? null : 'budget');
    assert.equal(result.counts === null, total === 1001);
  }
});

test('status associations use dataset identity and exclude non-business entry counts', () => {
  const items = [{ entry_id: 'ops', dataset: null }, { entry_id: 'second', dataset: 'b' }, { entry_id: 'first', dataset: 'a' }, { entry_id: 'third', dataset: 'a' }];
  const joined = data.statusByDataset(items);
  assert.deepEqual(joined.get('a').map(item => item.entry_id), ['first', 'third']); assert.equal(joined.size, 2);
});

test('401/403 surface safe distinct errors and clear all preview session caches', async () => {
  for (const status of [401, 403]) {
    let fail = false;
    await withHttp(async () => fail ? response({ detail: { code: 'PRIVATE_SECRET', message: 'Bearer fixture-secret SQL private' } }, status) : response(preview()), async () => {
      const session = data.createPreviewSession();
      try {
        await session.read(request()); assert.ok(session.getSnapshot().result); fail = true;
        await assert.rejects(data.dataAssetsClient.listDatasets(), error => error.status === status && error.code === (status === 401 ? 'AUTH_REQUIRED' : 'PERMISSION_DENIED')
          && !/fixture-secret|SQL|Bearer/.test(error.message));
        assert.equal(session.getSnapshot().result, null); assert.deepEqual(session.getSnapshot().cursors, [null]);
      } finally { session.dispose(); }
    });
  }
});

test('unknown errors never expose server messages, SQL, credentials or raw network details', async () => {
  await withHttp(async () => response({ detail: { code: 'NEW_SAFE_REASON', message: 'secret DSN SQL' } }, 409), async () => {
    await assert.rejects(data.dataAssetsClient.listDatasets(), error => error.code === 'NEW_SAFE_REASON' && !/secret|DSN|SQL/.test(error.message));
  });
  await withHttp(async () => { throw new TypeError('Bearer private network secret'); }, async () => {
    await assert.rejects(data.dataAssetsClient.listDatasets(), error => error.code === 'NETWORK_ERROR' && !/private|secret|Bearer/.test(error.message));
  });
});

test('timeout and caller cancellation settle even when fetch ignores AbortSignal', async () => {
  await withHttp(async () => new Promise(() => {}), async () => {
    await assert.rejects(data.dataAssetsClient.listDatasets({ timeoutMs: 5 }), error => error.code === 'QUERY_TIMEOUT');
    const controller = new AbortController();
    const promise = data.dataAssetsClient.listDatasets({ signal: controller.signal });
    const checked = assert.rejects(promise, { name: 'AbortError' }); controller.abort(); await checked;
    await assert.rejects(data.dataAssetsClient.listDatasets({ signal: controller.signal }), { name: 'AbortError' });
  });
});

test('late responses after abort or token changes are rejected rather than accepted', async () => {
  for (const mode of ['abort', 'token']) {
    const late = deferred();
    await withHttp(async () => late.promise, async ({ setToken }) => {
      const controller = new AbortController();
      const promise = data.dataAssetsClient.listDatasets({ signal: controller.signal });
      const checked = assert.rejects(promise, { name: 'AbortError' });
      if (mode === 'abort') controller.abort(); else setToken('another-isolated-session');
      late.resolve(response(catalog())); await checked;
    });
  }
});

test('latest request scopes reject late results, caller aborts and disposed scopes', async () => {
  const scope = data.createRequestScope(), late = deferred();
  const first = scope.run(async () => late.promise), checked = assert.rejects(first, { name: 'AbortError' });
  assert.equal(await scope.run(async () => 'new'), 'new'); late.resolve('old'); await checked;
  const controller = new AbortController(); controller.abort();
  await assert.rejects(scope.run(async () => 'never', { signal: controller.signal }), { name: 'AbortError' });
  scope.dispose(); await assert.rejects(scope.run(async () => 'never'), { name: 'AbortError' });
});

test('preview paging uses only obtained cursors and never concatenates pages or asserts coverage', async () => {
  const calls = [];
  await withHttp(async (_url, options) => {
    const body = JSON.parse(options.body); calls.push(body);
    return response(preview({ rows: [{ member: body.cursor ? 'second' : 'first' }], next_cursor: body.cursor ? null : 'isolated-cursor' }));
  }, async () => {
    const session = data.createPreviewSession();
    try {
      await session.read(request()); await session.read(request(), { page: 1 });
      const current = session.getSnapshot();
      assert.equal(calls[1].cursor, 'isolated-cursor'); assert.deepEqual(current.result.rows, [{ member: 'second' }]);
      assert.equal(current.result.request_satisfied, false); assert.equal(current.result.business_date_coverage_verified, false);
      await assert.rejects(session.read(request(), { page: 2 }), error => error.code === 'INVALID_REQUEST');
      current.cursors.push('untrusted'); current.result.rows[0].member = 'mutated';
      assert.equal(session.getSnapshot().result.rows[0].member, 'second');
    } finally { session.dispose(); }
  });
});

test('DATA_CHANGED errors and silent generation changes clear old rows and cursor stacks', async () => {
  for (const change of ['error', 'generation']) {
    let calls = 0;
    await withHttp(async () => {
      if (++calls === 1) return response(preview({ next_cursor: 'isolated-cursor' }));
      return change === 'error' ? response(scenarios.dataChanged, 409) : response(preview({ generation: 2 }));
    }, async () => {
      const session = data.createPreviewSession();
      try {
        await session.read(request());
        await assert.rejects(session.read(request(), { page: 1 }), error => error.code === 'DATA_CHANGED');
        assert.equal(session.getSnapshot().result, null); assert.deepEqual(session.getSnapshot().cursors, [null]);
      } finally { session.dispose(); }
    });
  }
});

test('permission failure from a preview remains a 403 for the page handler', async () => {
  await withHttp(async () => response({ detail: {} }, 403), async () => {
    const session = data.createPreviewSession();
    try { await assert.rejects(session.read(request()), error => error.status === 403); assert.equal(session.getSnapshot().result, null); }
    finally { session.dispose(); }
  });
});

test('restricted/maintenance response pages and repeated cursors revoke the previous preview', async () => {
  for (const status of ['restricted', 'rebuilding', 'rebuild_required']) {
    let calls = 0;
    await withHttp(async () => response(preview({ status: ++calls === 1 ? 'available' : status, next_cursor: 'isolated-cursor' })), async () => {
      const session = data.createPreviewSession();
      try {
        await session.read(request()); await assert.rejects(session.read(request(), { page: 1 }), error => data.clearsPreview(error));
        assert.equal(session.getSnapshot().result, null);
      } finally { session.dispose(); }
    });
  }
  await withHttp(async () => response(preview({ next_cursor: 'same-cursor' })), async () => {
    const session = data.createPreviewSession();
    try {
      await session.read(request()); await assert.rejects(session.read(request(), { page: 1 }), error => error.code === 'INVALID_CURSOR');
      assert.deepEqual(session.getSnapshot().cursors, [null]);
    } finally { session.dispose(); }
  });
});

test('ordinary refresh failure retains the permitted page and original read time', async () => {
  let fail = false;
  await withHttp(async () => fail ? response({ detail: { code: 'CATALOG_UNAVAILABLE' } }, 503) : response(preview()), async () => {
    const session = data.createPreviewSession();
    try {
      await session.read(request()); const before = session.getSnapshot(); fail = true;
      await assert.rejects(session.read(request()), error => error.status === 503);
      assert.deepEqual(session.getSnapshot(), before);
    } finally { session.dispose(); }
  });
});

test('object changes immediately clear the page and reject old results without erasing the new page', async () => {
  const old = deferred(), newer = deferred(); let calls = 0;
  const client = { queryPreview: async () => ++calls === 1 ? old.promise : newer.promise };
  const session = data.createPreviewSession(client);
  try {
    const first = session.read(request()), checked = assert.rejects(first, { name: 'AbortError' });
    await Promise.resolve();
    const second = session.read({ ...request(), subject: 'another-subject' });
    assert.equal(session.getSnapshot().result, null);
    newer.resolve(preview({ rows: [{ member: 'new' }] })); await second;
    old.resolve(preview({ rows: [{ member: 'old' }] })); await checked;
    assert.equal(session.getSnapshot().result.rows[0].member, 'new');
  } finally { session.dispose(); }
});

test('logout invalidation and token changes clear memory without persisting cursors or business values', async () => {
  await withHttp(async () => response(preview({ next_cursor: 'isolated-cursor' })), async ({ setToken }) => {
    const session = data.createPreviewSession();
    try {
      await session.read(request()); data.invalidateDataAssetsSession();
      assert.equal(session.getSnapshot().result, null);
      await session.read(request()); setToken(null);
      assert.equal(session.getSnapshot().result, null); assert.deepEqual(session.getSnapshot().cursors, [null]);
    } finally { session.dispose(); }
  });
});

test('legacy import compatibility validates responses and blocks partial/current-history controls', async () => {
  await withHttp(async () => response(catalog()), async () => {
    assert.equal((await dataStoreApi('/datasets?limit=100')).items[0].dataset, dataset().dataset);
    await assert.rejects(dataStoreApi('/query?release=old', undefined, request()), error => error.code === 'INVALID_REQUEST');
    await assert.rejects(dataStoreApi('/query', undefined, { ...request(), allow_partial: true }), error => error.code === 'INVALID_REQUEST');
  });
});
