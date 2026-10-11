/* Loopback-only D03 metadata fixtures. No backend, source, or database is called. */
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const version = fs.readFileSync(path.resolve(__dirname, '../../../../../../../VERSION'), 'utf8').trim();
const sources = ['隔离示例：已停用来源', '隔离示例：来源 B', '隔离示例：来源 C'];
const frequencies = ['daily', 'minute', 'report', 'unknown', 'future_frequency'];
const states = ['available', 'restricted', 'not_checked', 'empty', 'rebuild_required', 'rebuilding', 'future_state'];
const updates = ['failed', 'processed', null, 'backoff', 'deferred', 'running', 'incomplete'];
function dataset(index) {
  const id = String(index).padStart(3, '0'), state = states[(index - 1) % states.length], update = updates[(index - 1) % updates.length];
  return {
    dataset: index === 3 ? 'fixture.data / + 003' : `fixture.data_${id}`, name: `隔离示例：数据集 ${id}`,
    domain: 'fixture', source: sources[(index - 1) % sources.length], frequency: frequencies[(index - 1) % frequencies.length],
    representation: 'typed-object-nodes-v2', schema_id: 'fixture-schema', rule: 'fixture-only',
    fields: [{ column: 'subject', type: 'string', meaning: '隔离示例对象', logical_type: 'string', arithmetic: 'unsupported' }],
    limitations: ['隔离示例元数据，不代表生产读取许可。'], business_key: ['subject'], status: state,
    row_count: state === 'not_checked' ? null : state === 'empty' ? 0 : 1234,
    generation: 1, updated_at: '2026-10-10T06:00:00.123456789Z', issues: state === 'restricted' ? 1 : 0,
    last_update: update === null ? null : { state: update, complete: update === 'processed', qualified: false, reason: null,
      source_rows: 25, committed_partitions: 1, updated_at: '2026-10-10T07:00:00.123456789Z' },
    partitions: [], partitions_truncated: false, partition_range: { from: '2026-10', to: '2026-10', precision: 'partition' }, preview_key: null,
    // An extra fixture observation proves that source enablement cannot override
    // the independent current status. It is not a proposed API permission field.
    source_enabled: (index - 1) % sources.length !== 0
  };
}
let currentScenario = 'normal', requests = [];
const scenarios = ['normal', 'empty', 'one', 'large', 'budget', 'partial', 'partial-empty', 'changed', 'pagination',
  'error', 'slow', 'forbidden', 'forbidden-slow-body', 'partial-forbidden', 'unauthorized', 'maintenance', 'unknown', 'alias'];
function send(res, status, value) {
  if (res.destroyed || res.writableEnded) return;
  res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
  res.end(status === 204 ? '' : JSON.stringify(value));
}
async function handle(req, res) {
  const url = new URL(req.url, 'http://127.0.0.1');
  if (url.pathname === '/__scenario' && req.method === 'GET') {
    const requested = url.searchParams.get('name');
    if (!scenarios.includes(requested)) return send(res, 400, { isolated: true, scenarios });
    currentScenario = requested; requests = [];
    return send(res, 200, { isolated: true, scenario: currentScenario });
  }
  if (url.pathname === '/__requests' && req.method === 'GET') return send(res, 200, { isolated: true, requests });
  if (req.method !== 'GET') return send(res, 405, { detail: { code: 'ISOLATED_READ_ONLY' } });
  requests.push({ method: req.method, path: url.pathname, query: url.search });
  if (url.pathname === '/api/auth/verify') return send(res, 204);
  if (url.pathname === '/api/system/version') return send(res, 200, { version });
  const scenario = currentScenario, route = url.pathname.replace('/api/admin/data-store', '');
  if (!url.pathname.startsWith('/api/admin/data-store/')) return send(res, 404, { detail: { code: 'ISOLATED_ONLY' } });
  if (scenario === 'slow') await new Promise(resolve => setTimeout(resolve, 1200));
  if (scenario === 'error') return send(res, 503, { detail: { code: 'DATA_STORE_UNAVAILABLE', message: 'fixture-proxy-diagnostic' } });
  if (scenario === 'unauthorized') return send(res, 401, { detail: { code: 'AUTH_REQUIRED' } });
  if (scenario === 'forbidden') return send(res, 403, { detail: { code: 'FORBIDDEN' } });
  if (scenario === 'forbidden-slow-body') {
    res.writeHead(403, { 'Content-Type': 'application/json' }); res.flushHeaders(); res.write('{"detail":');
    return setTimeout(() => { if (!res.destroyed) res.end('{"code":"ISOLATED_DENIAL"}}'); }, 1500);
  }
  const size = scenario === 'empty' ? 0 : ['one', 'alias'].includes(scenario) ? 1 : scenario === 'budget' ? 1100
    : ['large', 'partial', 'changed', 'pagination', 'partial-forbidden'].includes(scenario) ? 121 : 60;
  let items = Array.from({ length: size }, (_, i) => dataset(i + 1));
  if (scenario === 'alias') items = [{ ...dataset(1), dataset: 'market.stock_daily.reported', name: '市场 · stock_daily' }];
  if (scenario === 'maintenance') items = items.map(item => ({ ...item, status: 'rebuilding' }));
  if (scenario === 'unknown') items = items.map(item => ({ ...item, status: 'future_state', frequency: 'future_frequency',
    representation: 'future_layout', row_count: null, last_update: null, updated_at: null }));
  if (route === '/datasets') {
    const limit = Number(url.searchParams.get('limit')), offset = Number(url.searchParams.get('offset'));
    if (!Number.isInteger(limit) || limit < 1 || limit > 100 || !Number.isInteger(offset) || offset < 0) return send(res, 400, { detail: { code: 'INVALID_REQUEST' } });
    if (scenario === 'partial' && offset) return send(res, 503, { detail: { code: 'DATA_STORE_UNAVAILABLE' } });
    if (scenario === 'partial-forbidden' && offset) return send(res, 403, { detail: { code: 'FORBIDDEN' } });
    if (scenario === 'partial-empty') return send(res, 200, { items: [], total: 1, phase: 'ready', next_offset: null });
    // Reverse server ordering so the browser must perform stable name sorting.
    const selected = items.slice().reverse().slice(offset, offset + limit);
    return send(res, 200, { items: selected, total: scenario === 'changed' && offset ? size + 1 : size,
      phase: scenario === 'maintenance' ? 'rebuilding' : 'ready',
      next_offset: scenario === 'pagination' ? 0 : offset + selected.length < size ? offset + selected.length : null });
  }
  if (route.startsWith('/datasets/')) {
    const item = items.find(item => item.dataset === decodeURIComponent(route.slice('/datasets/'.length)));
    return send(res, item ? 200 : 404, item ?? { detail: { code: 'DATASET_UNKNOWN' } });
  }
  if (route === '/issues') return send(res, 200, { items: [], total: 0, affected_objects: 0, next_offset: null });
  send(res, 404, { detail: { code: 'ISOLATED_ONLY' } });
}
const server = http.createServer((req, res) => {
  void handle(req, res).catch(error => {
    if (req.aborted || error.code === 'ECONNRESET') return;
    if (!res.headersSent) send(res, 500, { detail: { code: 'ISOLATED_FIXTURE_ERROR' } });
    else res.destroy();
  });
});
server.listen(18766, '127.0.0.1', () => console.log(`Isolated D03 API: http://127.0.0.1:18766; ${scenarios.join('/')}`));
