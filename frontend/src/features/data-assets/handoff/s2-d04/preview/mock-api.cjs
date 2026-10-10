/* Synthetic D04 descriptors, served only on loopback. No database, supplier,
 * production backend or application authentication credential is used. */
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const version = fs.readFileSync(path.resolve(__dirname, '../../../../../../../VERSION'), 'utf8').trim();
const field = overrides => ({ column: 'f0_value', type: 'string', meaning: 'Model.value',
  logical_type: 'typed_scalar', arithmetic: 'type_only_units_require_domain_contract', ...overrides });
const common = {
  domain: 'fixture', source: '本地隔离夹具', representation: 'typed-object-nodes-v2',
  schema_id: 'fixture-schema-v2', rule: 'fixture-metadata-only',
  business_key: ['representation', 'subject', 'object_key', 'member_key'], status: 'available',
  row_count: 1234567, generation: 7, updated_at: '2026-10-10T08:00:00.123456789Z',
  issues: 1, legacy_restrictions: 0,
  last_update: { state: 'failed', complete: false, qualified: false, reason: 'SOURCE_RANGES_BLOCKED',
    source_rows: null, committed_partitions: null, updated_at: '2026-10-10T07:59:00Z' },
  partitions: ['2026-01.b01', '2026-03.b02'], partitions_truncated: true,
  partition_range: { from: '2026-01.b01', to: null, precision: 'partition' }
};
const datasets = [
  { ...common, dataset: 'fixture.daily', name: '隔离示例：日线（来源报告值／未锚定）', frequency: 'daily',
    fields: [field({ column: 'subject', meaning: 'current_identity_or_confirmation' }),
      field({ column: 'f0_trade_date', type: 'date32[day]', meaning: 'DailyPoint.trade_date' }),
      field({ column: 'f0_close', meaning: 'DailyPoint.close', logical_type: 'exact_decimal_text', arithmetic: 'unsupported' }),
      field({ column: 'f0_reported', type: 'bool', meaning: '来源报告值；未声明单位与价格基准' })],
    limitations: ['隔离夹具，不代表生产读取许可。', 'source_local_identity_only', 'adjustment_anchor_and_formula_unverified'],
    preview_key: { representation: 'fixture_daily_reported', subject: 'fixture-object-a', object_key: '2026-01-02', partition: '2026-01.b01' } },
  { ...common, dataset: 'fixture.nav', name: '隔离示例：净值（来源报告值）', frequency: 'daily',
    fields: [field({ column: 'f0_nav_date', type: 'date32[day]', meaning: 'NavPoint.nav_date' }),
      field({ column: 'f0_nav', meaning: 'NavPoint.value', logical_type: 'exact_decimal_text', arithmetic: 'unsupported' }),
      field({ column: 'f0_unknown_unit', meaning: '', type: 'decimal128(38, 18)', arithmetic: 'future_capability' })],
    limitations: ['隔离夹具，不代表生产读取许可。', '来源报告净值，单位与公式未声明。'],
    preview_key: { representation: 'fixture_nav_reported', subject: 'fixture-object-b', object_key: '2026-01-03', partition: '2026-01.b01' } },
  { ...common, dataset: 'fixture.report', name: '隔离示例：完整报告（来源报告值）', frequency: 'report',
    fields: Array.from({ length: 47 }, (_, index) => field({ column: `f${index}_report_value`,
      meaning: index === 2 ? '来源报告成员值；单位未声明' : `ReportMember.metric_${index}`,
      logical_type: index % 3 === 0 ? 'exact_decimal_text' : 'typed_scalar',
      arithmetic: index % 3 === 0 ? 'unsupported' : 'type_only_units_require_domain_contract' })),
    limitations: ['隔离夹具，不代表生产读取许可。', '报告成员可能跨页，单页节点不代表完整报告。', 'provider_period_and_formula_unverified'],
    preview_key: { representation: 'fixture_report_members', subject: 'fixture-object-c', object_key: 'fixture-period-2026', partition: '2026-01.b01' } }
];
const scenarios = ['normal', 'slow', 'refresh-error', 'empty', 'not-checked', 'unknown', 'restricted', 'maintenance',
  'rebuild-required', 'fields-empty', 'fields-reduced', 'limits-empty', 'no-preview', 'issues-error', 'missing', 'malformed', 'forbidden', 'unauthorized'];
let scenario = 'normal';
const requests = [];
function send(res, status, body) {
  if (res.destroyed || res.writableEnded) return;
  res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
  res.end(status === 204 ? '' : JSON.stringify(body));
}
function describe(item, current) {
  const modified = { ...item };
  const states = { empty: 'empty', 'not-checked': 'not_checked', unknown: 'new_fixture_state', restricted: 'restricted',
    maintenance: 'rebuilding', 'rebuild-required': 'rebuild_required' };
  if (states[current]) modified.status = states[current];
  if (current === 'empty') modified.row_count = 0;
  if (['not-checked', 'unknown'].includes(current)) Object.assign(modified, { row_count: null, generation: null, updated_at: null, last_update: null });
  if (current === 'fields-empty') modified.fields = [];
  if (current === 'fields-reduced') modified.fields = item.fields.slice(0, 3);
  if (current === 'limits-empty') modified.limitations = [];
  if (current === 'no-preview') modified.preview_key = null;
  if (current === 'malformed') modified.fields = [{ column: 'invalid-fixture-field' }];
  return modified;
}
async function handle(req, res) {
  const url = new URL(req.url, 'http://127.0.0.1');
  if (url.pathname === '/__scenario') {
    const name = url.searchParams.get('name');
    if (scenarios.includes(name)) scenario = name;
    return send(res, 200, { scenario, isolated: true });
  }
  if (url.pathname === '/__requests') return send(res, 200, { requests });
  if (url.pathname === '/api/auth/verify') return send(res, 204);
  if (url.pathname === '/api/system/version') return send(res, 200, { version });
  if (!url.pathname.startsWith('/api/admin/data-store/')) return send(res, 404, { detail: { code: 'ISOLATED_ROUTE_MISSING' } });
  const current = scenario;
  const record = { method: req.method, path: url.pathname, scenario: current, aborted: false };
  requests.push(record);
  res.on('close', () => { if (!res.writableEnded) record.aborted = true; });
  // Only the existing read-only query may POST. All other mutations are absent.
  const route = url.pathname.replace('/api/admin/data-store', '');
  if (req.method !== 'GET' && !(req.method === 'POST' && route === '/query')) return send(res, 405, { detail: { code: 'ISOLATED_READ_ONLY' } });
  if (current === 'slow' && route.startsWith('/datasets/')) await new Promise(resolve => setTimeout(resolve, 1400));
  if (['forbidden', 'unauthorized'].includes(current)) return send(res, current === 'forbidden' ? 403 : 401, { detail: { code: 'FIXTURE_DENIED' } });
  if (current === 'refresh-error') return send(res, 503, { detail: { code: 'DATA_STORE_UNAVAILABLE' } });
  if (route === '/datasets') return send(res, 200, { items: datasets.map(item => describe(item, current)), total: datasets.length, phase: 'ready', next_offset: null });
  if (route.startsWith('/datasets/')) {
    const item = datasets.find(value => value.dataset === decodeURIComponent(route.slice('/datasets/'.length)));
    return send(res, item && current !== 'missing' ? 200 : 404,
      item && current !== 'missing' ? describe(item, current) : { detail: { code: 'DATASET_UNKNOWN' } });
  }
  if (route === '/issues') {
    if (current === 'issues-error') return send(res, 503, { detail: { code: 'DATA_STORE_UNAVAILABLE' } });
    return send(res, 200, { items: [{ dataset: url.searchParams.get('dataset'), scope_key: 'fixture-object', reason: 'REPORT_INCOMPLETE',
      kind: 'current', updated_at: '2026-10-10T08:00:00Z', affected_objects: 2 }], total: 1, next_offset: null });
  }
  // D04 validates navigation to the existing D05 seam, not business-row reads.
  if (route === '/query') return send(res, 503, { detail: { code: 'ISOLATED_PREVIEW_NOT_IMPLEMENTED' } });
  send(res, 404, { detail: { code: 'ISOLATED_ROUTE_MISSING' } });
}
const server = http.createServer((req, res) => { void handle(req, res).catch(error => {
  if (req.aborted || error.code === 'ECONNRESET') return;
  if (!res.headersSent) send(res, 500, { detail: { code: 'ISOLATED_FIXTURE_FAILURE' } });
  else res.destroy();
}); });
server.listen(18766, '127.0.0.1', () => console.log(`D04 isolated fixture: http://127.0.0.1:18766; ${scenarios.join('/')}`));
