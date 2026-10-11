/* D06 contract tests use synthetic responses, never production acceptance. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
for (const extension of ['.ts', '.tsx']) require.extensions[extension] = (module, filename) => {
  const compiled = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }, fileName: filename
  });
  module._compile(compiled.outputText, filename);
};
require.extensions['.css'] = () => {};
global.__QF_VERSION__ = fs.readFileSync(require('node:path').resolve(__dirname, '../../VERSION'), 'utf8').trim();
const { createElement } = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { UpdateSummary, IssueRecord, issueExplanation } = require('../src/features/data-assets/UpdateIssuesPanel.tsx');
const { createUpdateIssuesSession } = require('../src/features/data-assets/UpdateIssuesSession.ts');
const { apiError } = require('../src/features/data-assets/data/errors.ts');
const { invalidateDataAssetsSession } = require('../src/features/data-assets/data/lifecycle.ts');
const dataset = overrides => ({ dataset: 'fixture.daily', name: '隔离示例', status: 'available',
  updated_at: '2026-10-10T08:00:00.123456789Z', legacy_restrictions: 1, limitations: [],
  last_update: { state: 'failed', complete: false, qualified: false, reason: 'SOURCE_REFRESH_FAILED',
    source_rows: null, committed_partitions: null, updated_at: '2026-10-10T07:59:00Z' }, ...overrides });
const issue = overrides => ({ dataset: 'fixture.daily', kind: 'current', reason: 'REPORT_INCOMPLETE',
  scope_key: 'fixture-report/member', affected_objects: null, updated_at: null, ...overrides });
const inventory = (offset = 0, total = 43, overrides = {}) => ({
  items: Array.from({ length: Math.min(20, Math.max(0, total - offset)) }, (_, i) => issue({ scope_key: `fixture-${offset + i}` })),
  total, affected_objects: 91, next_offset: offset + 20 < total ? offset + 20 : null, ...overrides });
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
const cancellation = error => error.name === 'AbortError';
const render = (Component, props) => renderToStaticMarkup(createElement(Component, props));

test('current availability, failed update, quality, coverage and timestamp keep separate meanings', () => {
  const html = render(UpdateSummary, { dataset: dataset() });
  for (const expected of ['当前可用（按服务端声明）', '本次更新失败', '未获合格确认',
    '业务日期覆盖</dt><dd>未声明', '业务数据截至日</dt><dd>未声明',
    '以服务端对实际对象与范围的查询判断为准', '2026-10-10T08:00:00.123456789Z', '旧数据是否可读仍以当前状态和实际查询为准']) assert.ok(html.includes(expected));
  assert.match(html, /来源记录数<\/dt><dd[^>]*>未声明/);
  assert.match(html, /已提交分区数<\/dt><dd[^>]*>未声明/);
  assert.doesNotMatch(html, /\d+%|全域可用|R01完成|供应商数据错误/);
});

test('complete does not certify quality or coverage and missing update does not invent stages', () => {
  const current = dataset(); current.last_update = { ...current.last_update, state: 'processed', complete: true, reason: null };
  const html = render(UpdateSummary, { dataset: current });
  assert.match(html, /已完成（服务端声明）/); assert.match(html, /未获合格确认/);
  assert.match(html, /业务日期覆盖<\/dt><dd>未声明/); assert.doesNotMatch(html, /已声明合格/);
  const absent = render(UpdateSummary, { dataset: dataset({ status: 'future_state', last_update: null }) });
  assert.match(absent, /状态待确认/); assert.match(absent, /暂无更新记录/); assert.doesNotMatch(absent, /等待重试|处理中|处理完成/);
});

test('known reason explanations and unknown safe codes preserve scope without inventing facts', () => {
  assert.match(issueExplanation('FULL_RANGE_UNPROVEN').next, /不表示全部数值错误/);
  assert.match(issueExplanation('SOURCE_REFRESH_FAILED').next, /旧数据是否可读/);
  assert.match(issueExplanation('REPORT_INCOMPLETE').next, /单页成员不代表完整报告/);
  const unknown = issueExplanation('FUTURE_VALID_CODE');
  assert.equal(unknown.label, '原因待确认'); assert.equal(unknown.code, 'FUTURE_VALID_CODE');
  assert.equal(issueExplanation('BEARER_SECRET').code, null);
  const html = render(IssueRecord, { ordinal: 21, issue: issue({ dataset: null, kind: 'legacy', reason: 'FUTURE_VALID_CODE' }) });
  assert.match(html, /既有数据限制/); assert.match(html, /本条受影响成员数：未声明/);
  assert.match(html, /未指定数据集，由服务端纳入本次筛选/);
  assert.match(html, /<details><summary>问题诊断信息（第 21 条）/);
  assert.match(html, /FUTURE_VALID_CODE/); assert.match(html, /fixture-report\/member/);
  assert.doesNotMatch(html, /<details open|错误率：|供应商/);
});

test('manual server pagination reaches every page, supports previous pages and preserves equal records', async () => {
  const calls = [];
  const session = createUpdateIssuesSession('fixture.daily', { async listIssues(id, options) {
    calls.push({ id, offset: options.offset, limit: options.limit });
    return inventory(options.offset, 43, { items: inventory(options.offset).items.map(() => issue()) });
  } });
  try {
    await session.read(); assert.equal(calls.length, 1); assert.equal(session.getSnapshot().result.items.length, 20);
    await session.read(1); assert.equal(session.getSnapshot().offset, 20);
    // Tail uses its actual 3 records, still without label-based deduplication.
    await session.read(2); assert.equal(session.getSnapshot().result.items.length, 3);
    assert.equal(session.getSnapshot().result.next_offset, null);
    await session.read(1); assert.equal(session.getSnapshot().page, 1);
    assert.deepEqual(calls.map(v => v.offset), [0, 20, 40, 20]);
    assert.ok(calls.every(v => v.id === 'fixture.daily' && v.limit === 20));
  } finally { session.dispose(); }
});

test('a legal empty inventory is distinct from a failed first read', async () => {
  const empty = createUpdateIssuesSession('fixture.daily', { listIssues: async () => inventory(0, 0, { affected_objects: 0 }) });
  const failed = createUpdateIssuesSession('fixture.daily', { listIssues: async () => { throw apiError(503, 'DATA_STORE_UNAVAILABLE'); } });
  try {
    await empty.read(); assert.equal(empty.getSnapshot().result.total, 0);
    await assert.rejects(failed.read()); assert.equal(failed.getSnapshot().result, null);
    assert.equal(failed.getSnapshot().readAt, null);
  } finally { empty.dispose(); failed.dispose(); }
});

test('refresh resets page navigation but preserves original page, offset and read time on ordinary failure', async () => {
  let failing = false;
  const session = createUpdateIssuesSession('fixture.daily', { async listIssues(id, options) {
    if (failing) throw apiError(503, 'DATA_STORE_UNAVAILABLE'); return inventory(options.offset);
  } });
  try {
    await session.read(); await session.read(1); const before = session.getSnapshot(); failing = true;
    await assert.rejects(session.read(0, true)); const after = session.getSnapshot();
    assert.equal(after.offset, 20); assert.equal(after.page, 1); assert.equal(after.readAt, before.readAt);
    assert.deepEqual(after.result, before.result); assert.equal(after.stale, true);
    await assert.rejects(session.read(1), error => error.code === 'INVALID_REQUEST');
    failing = false; await session.read(0, true); assert.equal(session.getSnapshot().page, 0); assert.equal(session.getSnapshot().stale, false);
  } finally { session.dispose(); }
});

test('permission, restriction and data change clear every cached page and offset', async () => {
  for (const [status, code] of [[401, 'AUTH_REQUIRED'], [403, 'PERMISSION_DENIED'], [409, 'DATA_RESTRICTED'], [409, 'DATA_CHANGED']]) {
    let failing = false;
    const session = createUpdateIssuesSession('fixture.daily', { async listIssues(id, options) {
      if (failing) throw apiError(status, code); return inventory(options.offset);
    } });
    try {
      await session.read(); failing = true; await assert.rejects(session.read(1));
      assert.equal(session.getSnapshot().result, null); assert.equal(session.getSnapshot().readAt, null);
      await assert.rejects(session.read(1), error => error.code === 'INVALID_REQUEST');
    } finally { session.dispose(); }
  }
});

test('a changed declared inventory and malformed offsets cannot continue an older page sequence', async () => {
  let value = inventory();
  const session = createUpdateIssuesSession('fixture.daily', { listIssues: async () => value });
  try {
    await session.read(); value = inventory(20, 44);
    await assert.rejects(session.read(1), error => error.code === 'DATA_CHANGED');
    assert.equal(session.getSnapshot().result, null);
    for (const next_offset of [0, 19, 21, 43]) {
      value = inventory(0, 43, { next_offset });
      await assert.rejects(session.read(0, true), error => error.code === 'INVALID_RESPONSE');
      assert.equal(session.getSnapshot().result, null);
    }
  } finally { session.dispose(); }
});

test('new refresh, object disposal and permission invalidation reject transports that ignore abort', async () => {
  const old = deferred(); let calls = 0;
  const session = createUpdateIssuesSession('fixture.daily', { listIssues: async () => ++calls === 1 ? old.promise : inventory() });
  try {
    const first = session.read().then(() => 'accepted', error => error);
    await Promise.resolve(); await session.read(0, true); old.resolve(inventory(0, 0));
    assert.ok(cancellation(await first)); assert.equal(session.getSnapshot().result.total, 43);
    for (const action of ['permission', 'dispose']) {
      const late = deferred();
      const next = createUpdateIssuesSession('fixture.report', { listIssues: () => late.promise });
      const pending = next.read().then(() => 'accepted', error => error);
      await Promise.resolve();
      if (action === 'permission') invalidateDataAssetsSession('permission'); else next.dispose();
      late.resolve(inventory()); assert.ok(cancellation(await pending)); assert.equal(next.getSnapshot().result, null);
      next.dispose();
    }
  } finally { session.dispose(); }
});

test('snapshot copies cannot mutate cached records, and changed session tokens remove cached content', async () => {
  let token = 'isolated-fixture-a'; global.window = { sessionStorage: { getItem: () => token } };
  const session = createUpdateIssuesSession('fixture.daily', { listIssues: async () => inventory() });
  try {
    await session.read(); const copy = session.getSnapshot(); copy.result.items[0].scope_key = 'altered';
    assert.notEqual(session.getSnapshot().result.items[0].scope_key, 'altered');
    token = 'isolated-fixture-b'; assert.equal(session.getSnapshot().result, null);
  } finally { session.dispose(); delete global.window; }
});
