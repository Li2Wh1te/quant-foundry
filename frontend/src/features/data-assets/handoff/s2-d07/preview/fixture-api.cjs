/* D07-only loopback proxy. The upstream D05 API is synthetic, never real data. */
const assert = require('node:assert/strict');
const http = require('node:http');
const upstream = new URL(process.env.QF_D07_FIXTURE_UPSTREAM || 'http://127.0.0.1:18767');
assert.equal(upstream.hostname, '127.0.0.1');
const refusals = {
  'metadata-maintenance-error': [503, 'DATA_STORE_REBUILDING'],
  'metadata-restricted-error': [409, 'DATA_RESTRICTED'],
  'metadata-changed-error': [409, 'DATA_CHANGED'],
  'metadata-rebuild-error': [409, 'REBUILD_REQUIRED'],
  'metadata-missing-error': [404, 'DATASET_UNKNOWN']
};
const localIssues = ['issues-paged', 'issues-paged-changed', 'issues-paged-slow'];
let scenario = 'normal';
const requests = [];
function json(res, status, body) {
  res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
  res.end(JSON.stringify(body));
}
const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, 'http://127.0.0.1');
    if (url.pathname === '/__scenario') {
      const name = url.searchParams.get('name');
      const response = await fetch(new URL(`/__scenario?name=${encodeURIComponent(refusals[name] || localIssues.includes(name) ? 'normal' : name)}`, upstream));
      if (!response.ok) return json(res, 400, { error: 'Unknown isolated scenario' });
      scenario = name;
      return json(res, 200, { scenario, isolated: true });
    }
    if (url.pathname === '/__requests') return json(res, 200, { requests });
    const current = scenario;
    let raw = '';
    for await (const chunk of req) raw += chunk;
    const record = { method: req.method, path: url.pathname, query: url.search, scenario: current,
      body: raw ? JSON.parse(raw) : null, aborted: false };
    requests.push(record);
    res.on('close', () => { if (!res.writableEnded) record.aborted = true; });
    if (refusals[current] && url.pathname.startsWith('/api/admin/data-store/datasets/')) {
      const [status, code] = refusals[current];
      return json(res, status, { detail: { code, message: 'Synthetic upstream detail must remain hidden' } });
    }
    if (localIssues.includes(current) && url.pathname === '/api/admin/data-store/issues') {
      const offset = Number(url.searchParams.get('offset')), limit = Number(url.searchParams.get('limit'));
      assert.equal(limit, 20); assert.ok(Number.isSafeInteger(offset) && offset >= 0);
      const total = current === 'issues-paged-changed' && offset > 0 ? 46 : 45;
      const items = Array.from({ length: Math.min(limit, total - offset) }, (_, index) => ({
        dataset: url.searchParams.get('dataset'), scope_key: `fixture-scope-${offset + index}`,
        reason: index % 2 ? 'SOURCE_REFRESH_FAILED' : 'FULL_RANGE_UNPROVEN', kind: 'current',
        affected_objects: 2, updated_at: '2026-10-10T08:00:00.123456789Z'
      }));
      const answer = () => { if (!res.destroyed) json(res, 200, { items, total, affected_objects: total * 2,
        next_offset: offset + items.length < total ? offset + items.length : null }); };
      if (current === 'issues-paged-slow') {
        const timer = setTimeout(answer, 1300); res.on('close', () => clearTimeout(timer)); return;
      }
      return answer();
    }
    const proxy = http.request(new URL(req.url, upstream), { method: req.method, headers: req.headers }, response => {
      res.writeHead(response.statusCode, response.headers);
      response.pipe(res);
    });
    res.on('close', () => proxy.destroy());
    proxy.on('error', () => { if (!res.headersSent) json(res, 502, { detail: { code: 'NETWORK_ERROR' } }); else res.destroy(); });
    proxy.end(raw);
  } catch { if (!res.headersSent) json(res, 500, { detail: { code: 'ISOLATED_FIXTURE_ERROR' } }); else res.destroy(); }
});
server.listen(18768, '127.0.0.1', () => console.log('D07 synthetic fixture: http://127.0.0.1:18768 (D05 upstream required)'));
