/* Local-only UI fixtures. This process binds loopback and never calls a backend. */
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const version = fs.readFileSync(path.join(__dirname, '../worktree/VERSION'), 'utf8').trim();
const states = ['available', 'restricted', 'not_checked', 'empty'];
const datasets = Array.from({ length: 48 }, (_, index) => ({
  dataset: `fixture.daily_${String(index + 1).padStart(2, '0')}`,
  name: `隔离示例：日频数据 ${String(index + 1).padStart(2, '0')}`,
  domain: 'fixture', source: '本地验收夹具', frequency: 'daily',
  representation: 'typed-object-nodes-v2', schema_id: 'fixture-schema', rule: 'fixture-only',
  fields: [
    { column: 'subject', type: 'string', meaning: '当前示例对象', logical_type: 'string', arithmetic: 'unsupported' },
    { column: 'object_key', type: 'string', meaning: '业务日期键', logical_type: 'date', arithmetic: 'unsupported' },
    { column: 'f1_value', type: 'decimal-string', meaning: '隔离示例值；保留十进制字符串', logical_type: 'decimal', arithmetic: 'unsupported' }
  ],
  limitations: ['隔离测试数据，不代表生产读取许可。', '业务日期完整覆盖未声明。'],
  business_key: ['representation', 'subject', 'object_key'], status: states[index % 4],
  row_count: index % 4 === 2 ? null : index % 4 === 3 ? 0 : 120,
  generation: 7, updated_at: '2026-10-10T06:00:00Z', issues: index % 4 === 1 ? 2 : 0,
  legacy_restrictions: 0,
  last_update: index % 4 === 2 ? null : {
    state: index % 4 === 1 ? 'incomplete' : 'processed', complete: index % 4 !== 1,
    qualified: false, reason: null, source_rows: 120, committed_partitions: 1,
    updated_at: '2026-10-10T06:00:00Z'
  },
  partitions: ['2026-10'], partitions_truncated: false,
  partition_range: { from: '2026-10', to: '2026-10', precision: 'partition' },
  preview_key: { representation: 'fixture_daily_raw', subject: 'fixture-object', object_key: '2026-10-09', partition: '2026-10' }
}));
let scenario = 'normal';
const scenarios = ['normal', 'empty', 'error', 'slow', 'issues-error', 'forbidden', 'changed',
  'unauthorized', 'issues-forbidden', 'query-forbidden', 'query-forbidden-slow', 'query-unauthorized-slow',
  'maintenance', 'generation', 'restricted', 'preview-empty', 'query-slow', 'unknown',
  'catalog-incomplete', 'query-restricted', 'query-generation', 'invalid-cursor', 'query-error'];
const send = (res, status, body) => {
  if (res.destroyed || res.writableEnded) return;
  res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
  res.end(status === 204 ? '' : JSON.stringify(body));
};
const handleRequest = async (req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1');
  if (url.pathname === '/__scenario') {
    const requested = url.searchParams.get('name');
    if (scenarios.includes(requested)) scenario = requested;
    return send(res, 200, { scenario, isolated: true });
  }
  if (url.pathname === '/api/auth/verify') return send(res, 204);
  if (url.pathname === '/api/system/version') return send(res, 200, { version });
  if (!url.pathname.startsWith('/api/admin/data-store/')) return send(res, 503, { detail: { message: '隔离预览只提供数据资产夹具。' } });
  // Capture the response scenario before delays so a late response can be tested
  // against an object/condition change without silently becoming a new scenario.
  const current = scenario;
  const route = url.pathname.replace('/api/admin/data-store', '');
  let body;
  if (route === '/query' && req.method === 'POST') {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    body = JSON.parse(Buffer.concat(chunks).toString());
  }
  if (current === 'slow' || (current === 'query-slow' && route === '/query')) await new Promise(resolve => setTimeout(resolve, 1500));
  if (current === 'error') return send(res, 503, { detail: { code: 'DATA_STORE_UNAVAILABLE', message: '隔离示例：数据服务暂不可用，请刷新页面重试。' } });
  if (current === 'forbidden' || (current === 'issues-forbidden' && route === '/issues')
    || (current === 'query-forbidden' && route === '/query')) return send(res, 403, { detail: { code: 'FORBIDDEN', message: '隔离示例：没有当前数据的读取权限。' } });
  if (current === 'unauthorized') return send(res, 401, { detail: { code: 'AUTH_REQUIRED' } });
  if (route === '/query' && ['query-forbidden-slow', 'query-unauthorized-slow'].includes(current)) {
    res.writeHead(current === 'query-forbidden-slow' ? 403 : 401, { 'Content-Type': 'application/json' });
    res.flushHeaders(); res.write('{"detail":');
    return setTimeout(() => res.end('{"code":"ISOLATED_DENIAL"}}'), 1500);
  }
  const displayed = datasets.map(item => current === 'generation' ? { ...item, generation: 8 }
    : current === 'maintenance' ? { ...item, status: 'rebuilding' }
    : current === 'restricted' ? { ...item, status: 'restricted' }
    : current === 'unknown' ? { ...item, status: 'future_fixture_state', row_count: null, issues: null,
      last_update: null, updated_at: null } : item);
  if (route === '/datasets') {
    if (current === 'catalog-incomplete' && Number(url.searchParams.get('offset')) > 0) return send(res, 503, { detail: { code: 'CATALOG_UNAVAILABLE' } });
    return send(res, 200, { items: current === 'empty' ? [] : displayed,
      total: current === 'empty' ? 0 : current === 'catalog-incomplete' ? 49 : datasets.length,
      phase: current === 'maintenance' ? 'rebuilding' : 'ready', next_offset: current === 'catalog-incomplete' ? 48 : null });
  }
  if (route.startsWith('/datasets/')) {
    const item = displayed.find(item => item.dataset === decodeURIComponent(route.slice('/datasets/'.length)));
    return send(res, item ? 200 : 404, item || { detail: { code: 'DATASET_UNKNOWN', message: '隔离示例数据集不存在。' } });
  }
  if (route === '/issues') {
    if (current === 'issues-error') return send(res, 503, { detail: { code: 'CATALOG_UNAVAILABLE', message: '隔离示例：问题列表读取失败，请刷新页面重试。' } });
    return send(res, 200, { items: [{ dataset: url.searchParams.get('dataset'), scope_key: 'fixture-object', reason: 'REPORT_INCOMPLETE', kind: 'current', updated_at: '2026-10-10T06:00:00Z' }], total: 2, next_offset: 1 });
  }
  if (route === '/query' && req.method === 'POST') {
    if (['changed', 'maintenance', 'restricted', 'invalid-cursor', 'query-error'].includes(current)) return send(res, current === 'query-error' ? 503 : 409,
      { detail: { code: ({ changed: 'DATA_CHANGED', maintenance: 'DATA_STORE_REBUILDING', restricted: 'DATA_RESTRICTED',
        'invalid-cursor': 'INVALID_CURSOR', 'query-error': 'DATA_STORE_UNAVAILABLE' })[current] } });
    const empty = current === 'preview-empty';
    return send(res, 200, {
      dataset: body.dataset, status: current === 'query-restricted' ? 'restricted' : empty ? 'empty' : 'available', request_satisfied: false,
      business_date_coverage_verified: false, partial_requested: false,
      rows: empty ? [] : [{ subject: '隔离示例对象', object_key: body.cursor ? '2026-10-10' : '2026-10-09', f1_value: '12345678901234567890.123456789' }],
      next_cursor: empty || body.cursor ? null : 'fixture-page-two',
      generation: current === 'generation' || (current === 'query-generation' && body.cursor) ? 8 : 7,
      actual_range: { from: empty ? null : body.cursor ? '2026-10-10' : '2026-10-09',
        to: empty ? null : body.cursor ? '2026-10-10' : '2026-10-09' }, selected_partitions: ['2026-10'],
      limitations: ['隔离夹具，覆盖未证明。']
    });
  }
  send(res, 404, { detail: { code: 'NOT_FOUND', message: '隔离预览未提供此接口。' } });
};
const server = http.createServer((req, res) => {
  // Browser cancellation is an expected part of the late-response checks and
  // must not terminate the isolated fixture process.
  void handleRequest(req, res).catch(error => {
    if (req.aborted || error.code === 'ECONNRESET') return;
    if (!res.headersSent) send(res, 500, { detail: { code: 'ISOLATED_FIXTURE_ERROR' } });
    else res.destroy();
  });
});
server.listen(18765, '127.0.0.1', () => console.log(`Isolated fixture API: http://127.0.0.1:18765; scenarios: ${scenarios.join('/')}`));
