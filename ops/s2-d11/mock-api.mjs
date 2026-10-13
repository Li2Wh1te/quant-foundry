/** Artificial D11 factor fixtures only; not a live or formal backend response. */
import { createHash } from 'node:crypto';
import http from 'node:http';
import { responseFor as d10Response } from '../s2-d10/mock-api.mjs';

const source = '合成测试夹具 · 模拟接口（非真实行情）';
const factors = [];
for (let at = Date.parse('2022-01-01T00:00:00Z'), index = 0; at <= Date.parse('2026-10-09T00:00:00Z'); at += 86400000) {
  const day = new Date(at); if ([0, 6].includes(day.getUTCDay())) continue;
  factors.push({ trade_date: day.toISOString().slice(0, 10), factor: `${index++ < 600 ? 1 : 2}.123456789123` });
}
export function responseFor(path, method = 'GET', declaredTestBasis = process.env.QF_D11_UI_CONTRACT === '1') {
  const url = new URL(path, 'http://127.0.0.1'), q = url.searchParams;
  if (url.pathname.endsWith('/series')) {
    const [status, items] = d10Response(path, method);
    if (declaredTestBasis) {
      for (const item of items) {
        item.adjustments = ['none', 'pre', 'post'];
        for (const mode of ['pre', 'post']) item.adjustment_capabilities[mode] = {
          name: mode, availability: 'available', request_checked: false, mapping_implemented: true, metadata_known: true,
          limitations: [{ code: 'TEST_CONTRACT_ONLY', message: '仅UI模拟常量F=1契约，不证明E69生产语义或默认API链。' }] };
      }
      return [status, items];
    }
    for (const item of items) for (const mode of ['pre', 'post']) item.adjustment_capabilities[mode] = {
      name: mode, availability: 'restricted', request_checked: false, mapping_implemented: true, metadata_known: true,
      limitations: [{ code: 'FACTOR_ANCHOR_UNVERIFIED', message: 'E69 累计公式、逐日覆盖与原始归一化基准未核验。' }] };
    return [status, items];
  }
  if (declaredTestBasis && url.pathname.endsWith('/bars')) {
    const adjustment = q.get('adjustment') || 'none';
    const raw = new URL(url); raw.searchParams.set('adjustment', 'none');
    const [status, body] = d10Response(raw.pathname + raw.search, method);
    if (status === 200) body.basis = { adjustment, policy: 'ui-only-constant-factor-one', as_of: body.window.as_of,
      anchor: adjustment === 'none' ? null : { date: adjustment === 'pre' ? body.window.as_of : '2022-01-01', factor: '1' },
      note: source + ' · 仅UI模拟常量F=1，曲线不变，不作为正式变换或来源语义证据。' };
    return [status, body];
  }
  if (!url.pathname.endsWith('/factors')) return d10Response(path, method);
  const start = q.get('start'), end = q.get('end'), as_of = q.get('as_of') || end, limit = Number(q.get('limit') || 512);
  const condition = JSON.stringify({ start, end, as_of, limit }); let before = null;
  if (q.has('cursor')) {
    try { const value = JSON.parse(Buffer.from(q.get('cursor'), 'base64url').toString()); if (value.condition !== condition) throw Error(); before = value.before; }
    catch { return [422, { detail: { code: 'INVALID_CURSOR' } }]; }
  }
  const rows = factors.filter(row => row.trade_date >= start && row.trade_date <= end && row.trade_date <= as_of && (!before || row.trade_date < before));
  const selected = rows.slice(-limit), items = declaredTestBasis ? selected.map(row => ({ ...row, factor: '1' })) : selected;
  const next_cursor = rows.length > limit ? Buffer.from(JSON.stringify({ condition, before: items[0].trade_date })).toString('base64url') : null;
  return [200, { items, next_cursor, basis: { adjustment: 'none', policy: 'synthetic-factor-observations', as_of, anchor: null,
    note: declaredTestBasis ? `${source}。仅UI常量F=1契约，不能授权生产E69。` : `${source}。因子原值可查看；累计公式、覆盖与原始基准未核验，前/后复权不可用。` },
    meta: { read_token: createHash('sha256').update(condition).digest('hex'), read_at: '2026-10-13T09:00:00Z',
      business_as_of: items.at(-1)?.trade_date ?? null, scope: `人工样本 · ${start} 至 ${end}`, source_labels: [source],
      coverage: { verified: false, state: 'unknown', missing_dates: null, note: '仅实际因子观察，不能证明日期覆盖。' }, limitations: [] } }];
}
if (process.argv[1]?.endsWith('/s2-d11/mock-api.mjs')) http.createServer((req, res) => {
  const [status, value] = responseFor(req.url, req.method);
  res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }); res.end(value === null ? '' : JSON.stringify(value));
}).listen(8010, '127.0.0.1', () => console.log('D11 artificial loopback API: http://127.0.0.1:8010'));
