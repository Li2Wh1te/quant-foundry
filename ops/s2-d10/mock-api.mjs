/** Artificial fixtures + loopback simulation only. No provider or user data. */
import http from 'node:http';
import { createHash } from 'node:crypto';
const instrumentKey = 'instrument:11111111-1111-4111-8111-111111111111';
const source = '合成测试夹具 · 模拟接口（非真实行情）';
const daily = [];
for (let timestamp = Date.parse('2022-01-01T00:00:00Z'), i = 0; timestamp <= Date.parse('2026-10-09T00:00:00Z'); timestamp += 86400000) {
  const day = new Date(timestamp); if ([0, 6].includes(day.getUTCDay())) continue;
  const date = day.toISOString().slice(0, 10), value = 4.1 + .18 * Math.sin(i / 13) + .1 * Math.cos(i / 27) + i / 8000;
  const close = value + .02 * Math.sin(i / 5); i++;
  daily.push({ bucket_start: date, bucket_end: date, first_trade_date: date, last_trade_date: date,
    open: value.toFixed(6), high: (Math.max(value, close) + .03).toFixed(6), low: (Math.min(value, close) - .03).toFixed(6), close: close.toFixed(6),
    volume: String(1000 + i * 19), amount: String(4100 + i * 21), completion: 'unknown' });
}
const capability = { name: 'raw', availability: 'available', request_checked: true, mapping_implemented: true, metadata_known: true, limitations: [] };
const units = { price: '元', volume: '手', amount: '千元' };
const meta = (read_token, asOf) => ({ read_token, read_at: '2026-10-13T07:00:00Z', business_as_of: asOf,
  scope: '人工测试样本', source_labels: [source], coverage: { verified: false, state: 'unknown', missing_dates: null, note: source }, limitations: [] });
function bucket(day, period) {
  const d = new Date(`${day}T00:00:00Z`);
  if (period === 'week') d.setUTCDate(d.getUTCDate() - (d.getUTCDay() + 6) % 7);
  else if (period === 'month') d.setUTCDate(1);
  else if (period === 'year') { d.setUTCMonth(0); d.setUTCDate(1); }
  return d.toISOString().slice(0, 10);
}
// Simulation for UI interaction only; no backend aggregation acceptance claim.
function aggregate(rows, period, end) {
  const result = [];
  for (const row of rows) {
    const start = bucket(row.last_trade_date, period), prior = result.at(-1);
    if (prior?.bucket_start === start) {
      prior.high = Math.max(Number(prior.high), Number(row.high)).toFixed(6);
      prior.low = Math.min(Number(prior.low), Number(row.low)).toFixed(6);
      prior.close = row.close; prior.last_trade_date = row.last_trade_date;
      prior.volume = String(Number(prior.volume) + Number(row.volume)); prior.amount = String(Number(prior.amount) + Number(row.amount));
    } else {
      const finish = new Date(`${start}T00:00:00Z`);
      if (period === 'week') finish.setUTCDate(finish.getUTCDate() + 6);
      else if (period === 'month') { finish.setUTCMonth(finish.getUTCMonth() + 1); finish.setUTCDate(0); }
      else if (period === 'year') { finish.setUTCFullYear(finish.getUTCFullYear() + 1); finish.setUTCDate(0); }
      const bucket_end = finish.toISOString().slice(0, 10);
      result.push({ ...row, bucket_start: start, bucket_end, completion: bucket_end > end ? 'partial' : 'unknown' });
    }
  }
  return result;
}
export function responseFor(path, method = 'GET') {
  const url = new URL(path, 'http://127.0.0.1'), q = url.searchParams, pathname = decodeURIComponent(url.pathname);
  if (pathname === '/api/auth/verify') return [204, null];
  if (pathname === '/api/system/version') return [200, { version: '0.3.0' }];
  if (pathname === '/api/admin/etf-watchlist') return [200, { items: [], offset: 0, limit: 50, has_more: false }];
  if (pathname.startsWith('/api/admin/etf-watchlist/')) return [200, { changed: true, message: '仅模拟自选操作，未写真实数据。' }];
  if (pathname === '/api/admin/market/instruments/resolve') return [200, { instrument_key: instrumentKey, symbol: '510300.SH', asset_type: 'etf', meta: meta('1'.repeat(64), null) }];
  if (pathname === `/api/admin/market/instruments/${instrumentKey}`) return [200, { instrument: { instrument_key: instrumentKey, symbol: '510300.SH', name: '合成样本 ETF', asset_type: 'etf',
    exchange: 'SSE', list_status: null, list_date: '2022-01-01', index_name: null, manager_name: null, management_fee: null, management_fee_unit: null, capabilities: [capability] }, meta: meta('2'.repeat(64), null) }];
  if (pathname.endsWith('/series')) return [200, [{ series_key: 'etf-daily-local', label: source, instrument_key: instrumentKey,
    input_frequency: 'daily', periods: ['day', 'week', 'month', 'year'], adjustments: ['none'], adjustment_capabilities: { none: capability }, price_basis: 'unadjusted', units, capability }]];
  if (pathname.endsWith('/bars')) {
    const period = q.get('period') || 'day', end = q.get('end') || '2026-10-13', as_of = q.get('as_of') || end;
    const start = q.get('start'), limit = Number(q.get('limit') || 512), cursor = q.get('cursor');
    if (q.get('adjustment') && q.get('adjustment') !== 'none') return [422, { detail: { code: 'FACTOR_ANCHOR_UNVERIFIED' } }];
    const condition = JSON.stringify({ period, end, as_of, start, limit });
    const read_token = createHash('sha256').update(condition).digest('hex');
    let before = null;
    if (cursor) {
      try { const parsed = JSON.parse(Buffer.from(cursor, 'base64url').toString()); if (parsed.condition !== condition) throw new Error(); before = parsed.before; }
      catch { return [422, { detail: { code: 'INVALID_CURSOR' } }]; }
    }
    const rows = aggregate(daily.filter(row => row.last_trade_date <= end && row.last_trade_date <= as_of && (!start || row.last_trade_date >= start)), period, end)
      .filter(row => !before || row.bucket_start < before);
    const items = rows.slice(-limit), next_cursor = rows.length > limit ? Buffer.from(JSON.stringify({ condition, before: items[0].bucket_start })).toString('base64url') : null;
    return [200, { items, instrument_key: instrumentKey, series_key: 'etf-daily-local', period, units, next_cursor,
      window: { start, end, as_of }, basis: { adjustment: 'none', policy: 'synthetic-ui-simulation', as_of, anchor: null, note: source }, meta: meta(read_token, items.at(-1)?.last_trade_date || null) }];
  }
  return [404, { detail: { code: 'CAPABILITY_UNAVAILABLE' } }];
}
if (process.argv[1]?.endsWith('mock-api.mjs')) {
  http.createServer((req, res) => {
    const [status, body] = responseFor(req.url, req.method);
    res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }); res.end(body === null ? '' : JSON.stringify(body));
  }).listen(8010, '127.0.0.1', () => console.log('D10 synthetic mock API: http://127.0.0.1:8010 (no real market/user data)'));
}
