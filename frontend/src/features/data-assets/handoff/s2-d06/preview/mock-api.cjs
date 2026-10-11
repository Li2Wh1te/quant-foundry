/* Loopback-only D06 fixtures. No database, real credentials or supplier calls. */
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const version = fs.readFileSync(path.resolve(__dirname, '../../../../../../../VERSION'), 'utf8').trim();
const common = {
  domain: 'fixture', source: '本地隔离夹具', frequency: 'daily', representation: 'typed-object-nodes-v2',
  schema_id: 'fixture-schema', rule: 'fixture-rule', fields: [], business_key: ['subject', 'object_key'],
  status: 'available', row_count: 1430, generation: 7, updated_at: '2026-10-10T08:00:00.123456789Z',
  issues: 43, legacy_restrictions: 2,
  limitations: ['隔离夹具，不代表生产读取许可。', 'units_and_formulas_unverified'],
  last_update: { state: 'failed', complete: false, qualified: false, reason: 'SOURCE_REFRESH_FAILED',
    source_rows: 1430, committed_partitions: null, updated_at: '2026-10-10T07:59:00Z' },
  partitions: [], partitions_truncated: false, partition_range: { from: null, to: null, precision: 'partition' },
  preview_key: { representation: 'fixture-reported', subject: 'fixture-object', object_key: '2026-01-02', partition: 'fixture-01' }
};
const datasets = ['daily', 'report'].map(id => ({ ...common, dataset: `fixture.${id}`, name: `隔离示例：${id === 'daily' ? '日线更新与问题' : '报告更新与问题'}` }));
const reasons = ['SOURCE_REFRESH_FAILED', 'FULL_RANGE_UNPROVEN', 'REPORT_INCOMPLETE', 'SOURCE_CONFIRMATION_UNPROVEN',
  'SOURCE_RANGES_BLOCKED', 'LEGACY_RESTRICTION', 'FUTURE_REASON_CODE'];
const itemsFor = id => Array.from({ length: 43 }, (_, i) => ({ dataset: i === 3 ? null : id,
  // Two real fixture records have every returned attribute equal, just as UNION ALL may return.
  scope_key: `fixture-${id}/scope-${i === 1 ? 0 : i}`, reason: reasons[(i === 1 ? 0 : i) % reasons.length],
  kind: i % 7 === 5 ? 'legacy' : 'current', affected_objects: i < 2 ? 2 : 3,
  updated_at: '2026-10-10T08:00:00.123456789Z' }));
const scenarios = ['normal', 'complete-unqualified', 'qualified', 'no-update', 'unknown', 'empty', 'counts-unknown',
  'issues-error', 'page-error', 'page-changed', 'malformed', 'slow', 'forbidden', 'unauthorized', 'generation-changed',
  'restricted', 'maintenance', 'rebuild-required', 'refresh-error'];
let scenario = 'normal';
const requests = [];
function send(res, status, body) {
  if (res.destroyed || res.writableEnded) return;
  res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
  res.end(status === 204 ? '' : JSON.stringify(body));
}
function describe(item, current) {
  const value = structuredClone(item);
  if (current === 'complete-unqualified') value.last_update = { ...value.last_update, state: 'processed', complete: true, reason: 'FULL_RANGE_UNPROVEN' };
  if (current === 'qualified') value.last_update = { ...value.last_update, state: 'processed', complete: true, qualified: true, reason: null };
  if (current === 'no-update') value.last_update = null;
  if (current === 'unknown') { value.status = 'future_state'; value.last_update.state = 'future_update'; value.last_update.reason = 'FUTURE_REASON_CODE'; }
  if (current === 'empty') { value.issues = 0; value.legacy_restrictions = 0; }
  if (current === 'counts-unknown') { value.issues = null; delete value.legacy_restrictions; value.last_update.source_rows = null; }
  if (current === 'generation-changed') value.generation = 8;
  const states = { restricted: 'restricted', maintenance: 'rebuilding', 'rebuild-required': 'rebuild_required' };
  if (states[current]) value.status = states[current];
  return value;
}
async function handle(req, res) {
  const url = new URL(req.url, 'http://127.0.0.1');
  if (url.pathname === '/__scenario') {
    const name = url.searchParams.get('name'); if (scenarios.includes(name)) scenario = name;
    return send(res, 200, { scenario, isolated: true });
  }
  if (url.pathname === '/__requests') return send(res, 200, { requests });
  if (url.pathname === '/api/auth/verify') return send(res, 204);
  if (url.pathname === '/api/system/version') return send(res, 200, { version });
  if (!url.pathname.startsWith('/api/admin/data-store/')) return send(res, 404, { detail: { code: 'ISOLATED_ROUTE_MISSING' } });
  const current = scenario, offset = Number(url.searchParams.get('offset') || 0), limit = Number(url.searchParams.get('limit') || 20);
  const record = { method: req.method, path: url.pathname, dataset: url.searchParams.get('dataset'), offset, limit, scenario: current, aborted: false };
  requests.push(record);
  res.on('close', () => { if (!res.writableEnded) record.aborted = true; });
  if (req.method !== 'GET') return send(res, 405, { detail: { code: 'ISOLATED_READ_ONLY' } });
  const route = url.pathname.replace('/api/admin/data-store', '');
  if (current === 'slow' && route === '/issues') await new Promise(resolve => setTimeout(resolve, 1600));
  if (['forbidden', 'unauthorized'].includes(current) && route === '/issues') return send(res, current === 'forbidden' ? 403 : 401, { detail: { code: 'FIXTURE_DENIED' } });
  if (current === 'refresh-error' && route.startsWith('/datasets/')) return send(res, 503, { detail: { code: 'DATA_STORE_UNAVAILABLE' } });
  if (route === '/datasets') return send(res, 200, { items: datasets.map(item => describe(item, current)), total: 2, phase: 'ready', next_offset: null });
  if (route.startsWith('/datasets/')) {
    const item = datasets.find(value => value.dataset === decodeURIComponent(route.slice('/datasets/'.length)));
    return send(res, item ? 200 : 404, item ? describe(item, current) : { detail: { code: 'DATASET_UNKNOWN' } });
  }
  if (route === '/issues') {
    if (current === 'issues-error' || current === 'page-error' && offset > 0) return send(res, 503, { detail: { code: 'DATA_STORE_UNAVAILABLE' } });
    const id = url.searchParams.get('dataset'), all = current === 'empty' ? [] : itemsFor(id);
    if (current === 'page-changed' && offset > 0) all.push({ ...all[all.length - 1], scope_key: 'fixture-extra' });
    const body = { items: all.slice(offset, offset + limit), total: all.length,
      affected_objects: all.reduce((sum, item) => sum + item.affected_objects, 0), next_offset: offset + limit < all.length ? offset + limit : null };
    if (current === 'counts-unknown') { delete body.affected_objects; body.items.forEach(item => { delete item.affected_objects; }); }
    if (current === 'malformed') body.next_offset = offset;
    return send(res, 200, body);
  }
  send(res, 404, { detail: { code: 'ISOLATED_ROUTE_MISSING' } });
}
const server = http.createServer((req, res) => { void handle(req, res).catch(error => {
  if (req.aborted || error.code === 'ECONNRESET') return;
  if (!res.headersSent) send(res, 500, { detail: { code: 'ISOLATED_FIXTURE_FAILURE' } }); else res.destroy();
}); });
server.listen(18768, '127.0.0.1', () => console.log('D06 isolated fixture: http://127.0.0.1:18768'));
