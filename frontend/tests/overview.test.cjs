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
const { getOperationsOverview, OverviewApiError } = require('../src/api/overview.ts');
const { overviewTime, overviewDuration, RUN_LABELS } = require('../src/components/overviewPresentation.ts');
const emptyRead = { data: null, error: null, pending: false, stale: false, canContinue: false };
const empty = { operations: null, market: emptyRead };
const summary = { catalog_key: 'etf-current', total_instruments: 17, listed_count: null, exchanges: ['SSE'],
  first_list_date: '2020-01-01', latest_list_date: '2026-01-02', meta: { business_as_of: null,
    read_at: '2026-10-13T08:00:00Z', source_labels: ['人工隔离契约'], coverage: { verified: false, state: 'unknown', missing_dates: null, note: '未核验' } } };
const marketRead = (extra = {}) => ({ ...emptyRead, data: summary, canContinue: true, ...extra });
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

test('formal directory objects and listing dates never become physical rows, a global market total or price coverage', () => {
  const html = render({ operations, market: marketRead() });
  assert.match(html, />17<\/b>/); assert.match(html, /个目录对象/);
  assert.match(html, /非物理行数或全市场证券数/);
  assert.match(html, /2020-01-01 → 2026-01-02/);
  assert.match(html, /业务截至：未知/); assert.match(html, /覆盖未核验/);
  assert.match(html, /全局开市数量未声明/);
  assert.doesNotMatch(html, /条开市记录|最近更新|条记录/);
});

test('network stale is local and explicit; restricted formal results do not preserve asset numbers', () => {
  const stale = render({ operations, market: marketRead({ stale: true,
    error: { message: '网络失败', code: 'NETWORK_ERROR' }, canContinue: false }) });
  assert.match(stale, /同条件旧摘要/); assert.match(stale, />17<\/b>/);
  const denied = render({ operations, market: { ...emptyRead, error: { message: '质量限制', code: 'DATA_RESTRICTED' } } });
  assert.doesNotMatch(denied, />17<\/b>/); assert.match(denied, /质量限制/);
  assert.match(denied, /120 次成功/); // Independent operations remain visible.
});

test('unconfigured source and failed operations do not close an independently readable formal directory', () => {
  const html = render({ operations: { ...operations, sources: [{ ...operations.sources[0], configured: false }] },
    market: marketRead() }, { errors: ['运营指标、数据源与运行记录'] });
  assert.match(html, /未配置/); assert.match(html, />17<\/b>/);
  assert.match(html, /来源配置与采集结果不决定已有合格本地数据的读取许可/);
  assert.match(html, /上次快照/); assert.match(html, /本次目录摘要已读取/);
});

test('time formatting uses Shanghai across midnight and supports long durations', () => {
  assert.match(overviewTime('2026-09-08T16:00:03Z', true), /2026\/09\/09 00:00:03/);
  assert.equal(overviewTime(null), '—'); assert.equal(overviewTime('invalid'), '—');
  assert.equal(overviewDuration(null), '—'); assert.equal(overviewDuration(0.2), '< 1 秒');
  assert.equal(overviewDuration(3661), '1 时 1 分');
});

test('operations request is read-only and separate from formal assets, with no legacy asset calls', async () => {
  const originalFetch = global.fetch, requests = [];
  global.fetch = async (path, options) => { requests.push({ path, options }); return { ok: true, json: async () => operations }; };
  try {
    assert.strictEqual(await getOperationsOverview(new AbortController().signal), operations);
    assert.equal(requests.length, 1); assert.equal(requests[0].path, '/api/admin/overview');
    assert.equal(requests[0].options.cache, 'no-store'); assert.ok(requests[0].options.signal);
  } finally { global.fetch = originalFetch; }
});

test('a 401 invalidates the aggregate instead of preserving an apparently valid session', async () => {
  const originalFetch = global.fetch;
  global.fetch = async () => ({ ok: false, status: 401 });
  try {
    await assert.rejects(getOperationsOverview(new AbortController().signal), error => error instanceof OverviewApiError && error.status === 401);
  } finally { global.fetch = originalFetch; }
});

test('superseded requests cannot return a snapshot even if a fetch ignores cancellation', async () => {
  const originalFetch = global.fetch;
  const controller = new AbortController();
  global.fetch = async () => ({ ok: true, json: async () => ({}) });
  try {
    const pending = getOperationsOverview(controller.signal);
    controller.abort();
    await assert.rejects(pending, { name: 'AbortError' });
  } finally { global.fetch = originalFetch; }
});


test('shared watchlist permission failures invalidate formal panels before any slow error body', async () => {
  const { listWatchlist } = require('../src/api/etfWatchlist.ts');
  const { onDataAssetsInvalidation } = require('../src/features/data-assets/data/lifecycle.ts');
  const originalFetch = global.fetch;
  let reason, bodyRead = false;
  const stop = onDataAssetsInvalidation(value => { reason = value; });
  global.fetch = async () => ({ ok: false, status: 403, json: () => { bodyRead = true; return new Promise(() => {}); } });
  try {
    await assert.rejects(listWatchlist(), error => error.status === 403);
    assert.equal(reason, 'permission'); assert.equal(bodyRead, false);
  } finally { stop(); global.fetch = originalFetch; }
});
