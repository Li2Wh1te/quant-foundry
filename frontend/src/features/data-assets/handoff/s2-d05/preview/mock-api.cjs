/* D05 loopback fixture only. No database, supplier, production endpoint or real token. */
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const version = fs.readFileSync(path.resolve(__dirname, '../../../../../../../VERSION'), 'utf8').trim();
const field = (column, overrides = {}) => ({ column, type: 'string', meaning: `Fixture.${column}`,
  logical_type: 'typed_scalar', arithmetic: 'type_only_units_require_domain_contract', ...overrides });
const identities = ['representation', 'subject', 'object_key', 'member_key'];
const fields = [...identities.map(column => field(column, { meaning: 'current_identity_or_confirmation' })),
  field('basis_state'), field('basis_token'),
  field('f0_decimal', { logical_type: 'exact_decimal_text', arithmetic: 'unsupported' }),
  field('f1_integer', { type: 'int64' }), field('f2_event_ns', { type: 'int64' }),
  field('f3_null'), field('f4_flag', { type: 'bool' }), field('f5_zero', { type: 'int64' }), field('f6_text')];
const common = { domain: 'fixture', source: '本地隔离夹具', representation: 'typed-object-nodes-v2',
  schema_id: 'fixture-schema-v2', rule: 'fixture-read-only', business_key: identities, fields,
  limitations: ['隔离夹具，不代表真实读取许可。', '单位与计算口径未声明。'],
  status: 'available', row_count: 3, generation: 7, issues: 0, legacy_restrictions: 0,
  updated_at: '2026-10-10T08:00:00.123456789Z', last_update: null,
  partitions: ['fixture-partition'], partitions_truncated: false,
  partition_range: { from: 'fixture-partition', to: 'fixture-partition', precision: 'partition' } };
const datasets = ['daily', 'nav', 'report'].map(kind => ({ ...common, dataset: `fixture.${kind}`,
  name: `隔离示例：${{ daily: '日线', nav: '净值', report: '报告片段' }[kind]}（来源报告值）`,
  frequency: kind === 'report' ? 'report' : 'daily',
  fields: kind === 'report' ? [...fields, ...Array.from({ length: 40 }, (_, index) => field(`f${index + 7}_member`))] : fields,
  preview_key: { representation: `fixture_${kind}_reported`, subject: `fixture-${kind}-object`,
    object_key: kind === 'report' ? 'fixture-period:2026' : '2026-01-02', partition: 'fixture-partition' } }));
const scenarios = ['normal', 'slow', 'query-empty', 'empty-page', 'dataset-empty', 'no-preview', 'no-fields', 'no-columns',
  'not-checked', 'unknown-descriptor', 'metadata-restricted-readable', 'metadata-generation', 'metadata-quality', 'metadata-schema', 'metadata-subject',
  'query-network', 'query-timeout', 'query-changed', 'generation-next', 'query-restricted', 'restriction-next',
  'query-maintenance', 'query-rebuild', 'query-file-invalid', 'query-unknown-error', 'query-forbidden', 'query-unauthorized',
  'query-unknown-status', 'query-schema-mismatch', 'query-partial', 'query-malformed', 'query-dataset-mismatch',
  'metadata-forbidden', 'metadata-unauthorized', 'issues-readable', 'issues-forbidden', 'issues-unauthorized'];
let scenario = 'normal';
const requests = [];
function send(res, status, body) {
  if (res.destroyed || res.writableEnded) return;
  res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
  if (status === 204) return res.end();
  // Exercise the shared exact JSON parser with unquoted integers too. All values are synthetic.
  const text = JSON.stringify(body).replaceAll('"__RAW_BIGINT__"', '900719925474099312345')
    .replaceAll('"__RAW_NS__"', '1791619200123456789');
  res.end(text);
}
const reject = (res, status, code) => send(res, status, { detail: { code, message: 'fixture raw upstream text must never appear' } });
function describe(item, current) {
  const value = structuredClone(item);
  if (current === 'dataset-empty') Object.assign(value, { status: 'empty', row_count: 0, preview_key: null });
  if (current === 'no-preview') value.preview_key = null;
  if (current === 'no-fields') value.fields = [];
  if (current === 'no-columns') Object.assign(value, { fields: [], business_key: [] });
  if (current === 'not-checked') Object.assign(value, { status: 'not_checked', generation: null, row_count: null });
  if (current === 'unknown-descriptor') value.status = 'future_state';
  if (current === 'metadata-restricted-readable') value.status = 'restricted';
  if (current.startsWith('issues-')) Object.assign(value, { status: 'restricted', issues: 1 });
  if (current === 'metadata-generation') value.generation = 8;
  if (current === 'metadata-quality') value.issues = 2;
  if (current === 'metadata-schema') value.schema_id = 'fixture-schema-next';
  if (current === 'metadata-subject') value.preview_key.subject = 'fixture-new-default';
  return value;
}
function pageResult(item, body, current) {
  const second = !!body.cursor;
  const empty = current === 'query-empty' || current === 'empty-page' && !second;
  const rows = empty ? [] : Array.from({ length: second ? 1 : 2 }, (_, index) => {
    const row = { representation: body.representation, subject: body.subject, object_key: body.from_key,
      member_key: `${body.subject}/${second ? 'page-2' : 'page-1'}/${index}`, basis_state: 'qualified', basis_token: 'synthetic-internal',
      f0_decimal: second ? '0.000000000000000001' : '12345678901234567890.123456789012345678',
      f1_integer: '__RAW_BIGINT__', f2_event_ns: '__RAW_NS__', f3_null: null, f4_flag: false, f5_zero: 0,
      f6_text: '2026-10-10T08:00:00.123456789Z' };
    for (const column of body.columns) if (!(column in row)) row[column] = `synthetic-${column}`;
    return Object.fromEntries(body.columns.map(column => [column, row[column]]));
  });
  return { dataset: item.dataset, status: empty ? 'empty' : 'available', rows,
    next_cursor: second || current === 'query-empty' ? null : `fixture-cursor:${body.subject}:page-2`,
    generation: current === 'generation-next' && second || current === 'metadata-generation' ? 8 : 7,
    request_satisfied: false, business_date_coverage_verified: false, partial_requested: false,
    actual_range: { from: rows.length ? body.from_key : null, to: rows.length ? body.to_key : null },
    selected_partitions: ['fixture-partition'], limitations: item.limitations,
    schema_id: current === 'metadata-schema' ? 'fixture-schema-next' : item.schema_id };
}
async function handle(req, res) {
  const url = new URL(req.url, 'http://127.0.0.1');
  if (url.pathname === '/__scenario') {
    const name = url.searchParams.get('name');
    if (!scenarios.includes(name)) return send(res, 400, { error: 'Unknown fixture scenario' });
    scenario = name; return send(res, 200, { scenario, isolated: true });
  }
  if (url.pathname === '/__requests') return send(res, 200, { requests });
  if (url.pathname === '/api/auth/verify') return send(res, 204);
  if (url.pathname === '/api/system/version') return send(res, 200, { version });
  if (!url.pathname.startsWith('/api/admin/data-store/')) return reject(res, 404, 'ISOLATED_ROUTE_MISSING');
  const route = url.pathname.replace('/api/admin/data-store', ''), current = scenario;
  const record = { method: req.method, path: url.pathname, scenario: current, aborted: false, body: null };
  requests.push(record);
  res.on('close', () => { if (!res.writableEnded) record.aborted = true; });
  if (req.method !== 'GET' && !(req.method === 'POST' && route === '/query')) return reject(res, 405, 'ISOLATED_READ_ONLY');
  if (route === '/query') {
    let raw = '';
    for await (const chunk of req) raw += chunk;
    const body = JSON.parse(raw); record.body = body;
    const item = datasets.find(value => value.dataset === body.dataset);
    if (!item || body.allow_partial !== false || body.representation !== item.preview_key.representation || body.frequency !== item.frequency
      || !Array.isArray(body.columns) || !body.columns.length || body.columns.length > 32 || new Set(body.columns).size !== body.columns.length
      || body.columns.some(column => !item.fields.some(field => field.column === column))
      || !Number.isInteger(body.page_size) || body.page_size < 1 || body.page_size > 100 || body.from_key > body.to_key) {
      return reject(res, 422, 'INVALID_VALUE');
    }
    if (body.cursor && body.cursor !== `fixture-cursor:${body.subject}:page-2`) return reject(res, 409, 'INVALID_CURSOR');
    if (current === 'slow' || body.subject === 'fixture-ignore-abort') await new Promise(resolve => setTimeout(resolve, 1300));
    const errors = { 'query-timeout': [504, 'QUERY_TIMEOUT'], 'query-changed': [409, 'DATA_CHANGED'],
      'query-restricted': [409, 'DATA_RESTRICTED'], 'query-maintenance': [503, 'DATA_STORE_REBUILDING'],
      'query-rebuild': [409, 'REBUILD_REQUIRED'], 'query-file-invalid': [422, 'FILE_INVALID'],
      'query-unknown-error': [409, 'UNKNOWN_QUALITY_FAILURE'], 'query-forbidden': [403, 'FIXTURE_DENIED'],
      'query-unauthorized': [401, 'FIXTURE_DENIED'] };
    if (errors[current]) return reject(res, ...errors[current]);
    if (current === 'query-network') return res.destroy();
    const result = pageResult(item, body, current);
    if (current === 'restriction-next' && body.cursor) result.status = 'restricted';
    if (current === 'query-unknown-status') result.status = 'future_quality';
    if (current === 'query-schema-mismatch') result.schema_id = 'unexpected-fixture-schema';
    if (current === 'query-partial') result.partial_requested = true;
    if (current === 'query-malformed') result.rows = { invalid: true };
    if (current === 'query-dataset-mismatch') result.dataset = 'fixture.other';
    return send(res, 200, result);
  }
  if (current === 'metadata-forbidden') return reject(res, 403, 'FIXTURE_DENIED');
  if (current === 'metadata-unauthorized') return reject(res, 401, 'FIXTURE_DENIED');
  if (route === '/datasets') return send(res, 200, { items: datasets.map(item => describe(item, current)), total: datasets.length, phase: 'ready', next_offset: null });
  if (route.startsWith('/datasets/')) {
    const item = datasets.find(value => value.dataset === decodeURIComponent(route.slice('/datasets/'.length)));
    return item ? send(res, 200, describe(item, current)) : reject(res, 404, 'DATASET_UNKNOWN');
  }
  if (route === '/issues') {
    if (current === 'issues-forbidden') return reject(res, 403, 'FIXTURE_DENIED');
    if (current === 'issues-unauthorized') return reject(res, 401, 'FIXTURE_DENIED');
    return send(res, 200, { items: [], total: 0, next_offset: null });
  }
  if (route === '/status') return send(res, 200, { items: [], total: 0, phase: 'ready', code: 'FIXTURE', fresh_install: false, next_offset: null });
  return reject(res, 404, 'ISOLATED_ROUTE_MISSING');
}
const server = http.createServer((req, res) => { void handle(req, res).catch(error => {
  if (req.aborted || error.code === 'ECONNRESET') return;
  if (!res.headersSent) reject(res, 500, 'ISOLATED_FIXTURE_FAILURE'); else res.destroy();
}); });
server.listen(18767, '127.0.0.1', () => console.log(`D05 isolated fixture http://127.0.0.1:18767; ${scenarios.join('/')}`));
