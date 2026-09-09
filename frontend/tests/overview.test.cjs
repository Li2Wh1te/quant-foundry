/* Exercise snapshot failures and actual UI output. Test fixtures never enter the
 * production bundle and cannot replace authenticated responses in the app. */
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
const { createElement } = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { MemoryRouter } = require('react-router-dom');
const { OverviewContent } = require('../src/components/OverviewContent.tsx');
const { loadOverviewSnapshot, OverviewApiError } = require('../src/api/overview.ts');
const { overviewTime, overviewDuration, RUN_LABELS } = require('../src/components/overviewPresentation.ts');
const empty = { operations: null, etfs: null, calendar: null };
const run = {
  id: 'run-1', task_id: 'task-1', task_name: '日历同步', task_type: 'internal_task_key',
  task_type_name: '交易日历同步', task_type_english_name: 'Trading Calendar Sync', source_key: 'tushare',
  status: 'failed', created_at: '2026-09-08T15:59:00Z', started_at: '2026-09-08T16:00:00Z',
  finished_at: '2026-09-08T16:00:03Z', duration_seconds: 3
};
const operations = {
  generated_at: '2026-09-09T01:00:00Z', timezone: 'Asia/Shanghai',
  day_start: '2026-09-08T16:00:00Z', day_end: '2026-09-09T16:00:00Z',
  metrics: { configured_sources: 1, total_sources: 1, active_tasks: 6, queued_runs: 2, running_runs: 1, today_runs: 123, today_succeeded: 120, attention_tasks: 14 },
  sources: [{ key: 'tushare', name: 'Tushare', configured: true, connection_status: 'not_checked', active_tasks: 6, last_success_at: null }],
  recent_runs: [run], attention: [{ run, retrying: true }]
};
function render(snapshot, overrides = {}) {
  return renderToStaticMarkup(createElement(MemoryRouter, null, createElement(OverviewContent, {
    snapshot, loading: false, errors: [], refreshed: true, onRefresh() {}, ...overrides
  })));
}

test('unknown data does not claim an empty or healthy workspace', () => {
  const html = render(empty, { loading: true });
  assert.match(html, /正在加载/);
  assert.match(html, /qfo-metric-value">—/);
  assert.doesNotMatch(html, /暂无待处理|暂无运行记录|暂无需要处理/);
  assert.match(html, /disabled=""/);
});

test('server aggregates, retry reminders and task display names survive rendering', () => {
  const html = render({ ...empty, operations });
  assert.match(html, /qfo-metric-value">123/);
  assert.match(html, /120 次成功/);
  assert.match(html, /展示 1 \/ 14 项/);
  assert.match(html, /已有后续执行排队或运行中/);
  assert.match(html, /交易日历同步（Trading Calendar Sync）/);
  assert.doesNotMatch(html, /internal_task_key/);
  assert.match(html, /配置状态汇总/);
  assert.match(html, /已配置不代表连接可用/);
});

test('all statuses remain distinguishable and successful empty state is explicit', () => {
  const html = render({ ...empty, operations: { ...operations,
    metrics: { ...operations.metrics, attention_tasks: 0 }, attention: [],
    recent_runs: Object.keys(RUN_LABELS).map(status => ({ ...run, id: status, status }))
  } });
  for (const label of Object.values(RUN_LABELS)) assert.ok(html.includes(label), label);
  assert.match(html, /暂无需要处理/);
  assert.match(render({ ...empty, operations: { ...operations, recent_runs: [] } }), /暂无运行记录/);
});

test('asset count uses open records and failures visibly preserve stale numbers', () => {
  const html = render({ operations, etfs: { total_records: 1775 }, calendar: { total_records: 25871, open_day_count: 16002 } }, { errors: ['ETF 资产'] });
  assert.match(html, /1,775/); assert.match(html, /16,002/); assert.doesNotMatch(html, /25,871/);
  assert.match(html, /加载失败，已保留已有数据/); assert.match(html, /刷新失败/);
});

test('time formatting uses Shanghai across midnight and supports long durations', () => {
  assert.match(overviewTime('2026-09-08T16:00:03Z', true), /2026\/09\/09 00:00:03/);
  assert.equal(overviewTime(null), '—'); assert.equal(overviewTime('invalid'), '—');
  assert.equal(overviewDuration(null), '—'); assert.equal(overviewDuration(0.2), '< 1 秒');
  assert.equal(overviewDuration(3661), '1 时 1 分');
});

test('partial failures retain only failed regions and all reads bypass caches', async () => {
  const originalFetch = global.fetch;
  const requests = [];
  global.fetch = async (path, options) => {
    requests.push({ path, options });
    if (path.includes('/etfs/')) return { ok: false, status: 503 };
    return { ok: true, json: async () => path === '/api/admin/overview' ? operations : { open_day_count: 99 } };
  };
  try {
    const previous = { ...empty, etfs: { total_records: 1775 } };
    const result = await loadOverviewSnapshot(previous, new AbortController().signal);
    assert.strictEqual(result.snapshot.etfs, previous.etfs);
    assert.strictEqual(result.snapshot.operations, operations);
    assert.equal(result.snapshot.calendar.open_day_count, 99);
    assert.deepEqual(result.errors, ['ETF 资产']);
    assert.equal(requests.length, 3);
    for (const { options } of requests) { assert.equal(options.cache, 'no-store'); assert.ok(options.signal); }
  } finally { global.fetch = originalFetch; }
});

test('a 401 invalidates the aggregate instead of preserving an apparently valid session', async () => {
  const originalFetch = global.fetch;
  global.fetch = async () => ({ ok: false, status: 401 });
  try {
    await assert.rejects(loadOverviewSnapshot({ ...empty, operations }, new AbortController().signal), error => error instanceof OverviewApiError && error.status === 401);
  } finally { global.fetch = originalFetch; }
});

test('superseded requests cannot return a snapshot even if a fetch ignores cancellation', async () => {
  const originalFetch = global.fetch;
  const controller = new AbortController();
  global.fetch = async () => ({ ok: true, json: async () => ({}) });
  try {
    const pending = loadOverviewSnapshot(empty, controller.signal);
    controller.abort();
    await assert.rejects(pending, { name: 'AbortError' });
  } finally { global.fetch = originalFetch; }
});
