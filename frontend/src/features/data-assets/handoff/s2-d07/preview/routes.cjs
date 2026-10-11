/* All App route patterns and retired redirects; synthetic unavailable-data API. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.FRONTEND_URL || 'http://127.0.0.1:5195';
assert.equal(new URL(base).hostname, '127.0.0.1');
const output = process.env.SCREENSHOT_DIR || path.resolve(__dirname, '../screenshots');
const routes = [
  ['/', '/admin'], ['/login'], ['/admin'], ['/admin/data-assets'], ['/admin/data-assets/fixture.daily'],
  ['/admin/data-sources'], ['/admin/tasks'], ['/admin/data/trading-calendar', '/admin/data/etf-basics?calendar=open'],
  ['/admin/data/etf-basics'], ['/admin/data/etf-basics/510300.SH'], ['/admin/strategy-data'], ['/admin/strategies'],
  ['/admin/backtest-accounts'], ['/admin/backtest-preflight', '/admin/backtest-runs'], ['/admin/backtest-compare'],
  ['/admin/backtest-runs/fixture-run/results'], ['/admin/backtest-runs'], ['/admin/strategies/fixture-strategy'],
  ['/admin/strategies/fixture-strategy/backtests'], ['/admin/retired-unknown', '/admin'],
  ['/admin/logs', '/admin'], ['/admin/data/daily-quotes', '/admin']
];
(async () => {
  await fetch('http://127.0.0.1:18768/__scenario?name=normal');
  const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium', args: ['--no-sandbox'] });
  const c = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const cases = [], errors = [], requests = [];
  try {
    await c.addInitScript(() => { if (location.hostname === '127.0.0.1') sessionStorage.setItem('quant-foundry.api-token', 's2-d07-public-route-fixture'); });
    await c.route(url => url.hostname !== '127.0.0.1', route => route.abort());
    const p = await c.newPage(); p.on('pageerror', e => errors.push(e.message));
    p.on('request', r => { const u = new URL(r.url()); if (u.pathname.startsWith('/api/')) requests.push({ method: r.method(), path: u.pathname }); });
    for (const [route, redirect = route] of routes) {
      console.log(`CHECK: ${route}`);
      await p.goto(base + route); await p.waitForURL(base + redirect);
      if (redirect === '/login') await p.getByRole('button', { name: /登录/ }).waitFor();
      else {
        // ETF chart detail owns its full-window layout; other workspaces share the shell.
        await p.locator(redirect.startsWith('/admin/data/etf-basics/') ? '.qfe' : '.qfo-main').waitFor();
        assert.equal(await p.locator('.qf-assets').count() > 0, redirect.startsWith('/admin/data-assets'));
        if (!redirect.startsWith('/admin/data-assets')) assert.equal(await p.locator('.qf-preview,.qf-updates,.qf-catalog').count(), 0);
      }
      cases.push({ route, destination: new URL(p.url()).pathname + new URL(p.url()).search });
    }
    assert.deepEqual(errors, []); assert.ok(requests.every(r => r.method === 'GET'));
    fs.mkdirSync(output, { recursive: true });
    fs.writeFileSync(path.join(output, 'routes.json'), JSON.stringify({ source: 'Actual App routes with synthetic unavailable-data HTTP',
      passed: cases.length, cases, errors, requests, realDataAcceptance: false }, null, 2) + '\n');
    console.log(`Routes: ${cases.length} passed; zero page errors; GET only.`);
  } finally { await c.close(); await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
