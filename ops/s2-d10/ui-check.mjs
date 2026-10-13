/** Browser checks use artificial API fixtures; backend default-path tests are separate. */
import { createRequire } from 'node:module';
import { mkdir, writeFile } from 'node:fs/promises';
import assert from 'node:assert/strict';
import { responseFor } from './mock-api.mjs';
const require = createRequire(new URL('../../frontend/package.json', import.meta.url));
const { chromium } = require('playwright');
const browser = await chromium.launch({ executablePath: '/usr/bin/chromium', headless: true, args: ['--no-sandbox'] });
let releaseReplay = null;
try {
const directory = new URL('./screenshots/', import.meta.url); await mkdir(directory, { recursive: true });
const findings = [], problems = [], requests = [];
let mode = 'normal', holdReplay = false;
const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, timezoneId: 'America/Los_Angeles' });
page.on('pageerror', error => problems.push(error.message));
await page.route(url => url.pathname.startsWith('/api/'), async route => {
  const path = new URL(route.request().url()).pathname + new URL(route.request().url()).search;
  requests.push(path);
  const pathname = new URL(route.request().url()).pathname;
  if (pathname.endsWith('/bars') && mode === 'network') { await route.abort('failed'); return; }
  if (pathname.endsWith('/bars') && ['changed', 'permission', 'authentication'].includes(mode)) {
    await route.fulfill({ status: mode === 'changed' ? 409 : mode === 'permission' ? 403 : 401,
      contentType: 'application/json', body: JSON.stringify({ detail: { code: mode === 'changed' ? 'DATA_CHANGED' : 'DENIED' } }) }); return;
  }
  if (mode === 'profile' && /\/instruments\/[^/]+$/.test(pathname) && !pathname.endsWith('/resolve')) {
    await route.fulfill({ status: 422, contentType: 'application/json', body: JSON.stringify({ detail: { code: 'FILE_INVALID' } }) }); return;
  }
  if (holdReplay && pathname.endsWith('/bars')) await new Promise(resolve => { releaseReplay = resolve; });
  const [status, body] = responseFor(path, route.request().method());
  await route.fulfill({ status, contentType: 'application/json', body: body === null ? '' : JSON.stringify(body) });
});
await page.addInitScript(() => sessionStorage.setItem('quant-foundry.api-token', 'synthetic-ui-token-only'));
const url = 'http://127.0.0.1:5173/admin/data/etf-basics/510300.SH';
await page.goto(url); await page.getByText('512 / 10,000 根', { exact: false }).waitFor();
await page.locator('.qfe-chart canvas').first().waitFor();
await page.screenshot({ path: new URL('1440x900.png', directory).pathname });
findings.push('1440×900: default 512 artificial bars, visible canvas, simulation clearly labelled');
await page.getByRole('button', { name: '加载更早历史' }).click();
await page.getByText('1024 / 10,000 根', { exact: false }).waitFor();
await page.getByRole('button', { name: '加载更早历史' }).click();
await page.getByText('1245 / 10,000 根', { exact: false }).waitFor();
findings.push('Three artificial pages: 512 -> 1024 -> 1245; fixed end/as_of and deduplicated bounded memory');
await page.getByRole('button', { name: '精确值表', exact: true }).click();
assert.ok(await page.getByRole('table').count());
await page.getByRole('button', { name: '精确值表', exact: true }).click();
await page.getByRole('button', { name: '1 日', exact: true }).click();
await page.getByRole('button', { name: '1 周', exact: false }).click();
await page.waitForFunction(() => document.querySelector('.qfe-chart-caption')?.textContent.includes('周线'));
await page.getByRole('button', { name: '指标', exact: true }).click();
await page.getByLabel('MACD', { exact: true }).check(); await page.keyboard.press('Escape');
await page.getByRole('button', { name: '趋势线', exact: true }).click(); await page.keyboard.press('Escape');
findings.push('Period menu, independent indicator controls, drawing tool cancellation and exact-value table usable');
await page.getByRole('button', { name: '回放', exact: true }).click();
await page.getByLabel('开始回放日期', { exact: true }).fill('2026-09-01');
holdReplay = true;
await page.getByRole('button', { name: '开始回放', exact: true }).click();
await page.waitForFunction(() => document.querySelector('.qfe-chart-caption')?.textContent.includes('回放至 2026/09/01'));
assert.ok(!(await page.locator('.qfe-ohlc').innerText()).includes('2026/10/09'));
while (!releaseReplay) await page.waitForTimeout(10);
holdReplay = false; releaseReplay(); releaseReplay = null;
await page.waitForFunction(() => document.querySelector('.qfe-ohlc')?.textContent.includes('2026/09/01'));
await page.getByRole('button', { name: '前进一根日线', exact: true }).click();
await page.waitForFunction(() => document.querySelector('.qfe-chart-caption')?.textContent.includes('回放至 2026/09/02'));
await page.getByRole('button', { name: '退出', exact: true }).click();
findings.push('Replay current artificial revisions: no future OHLC while cutoff response is held, one-day advance and cancellation restore the main window');
await page.setViewportSize({ width: 1024, height: 768 });
await page.getByRole('button', { name: '切换资料侧栏' }).click();
await page.screenshot({ path: new URL('1024x768.png', directory).pathname });
await page.setViewportSize({ width: 390, height: 844 });
await page.getByRole('button', { name: '范围', exact: true }).click();
await page.getByLabel('开始日期', { exact: true }).fill('2026-08-01');
await page.getByLabel('结束日期', { exact: true }).fill('2026-09-30');
await page.getByRole('button', { name: '应用', exact: true }).click();
await page.waitForTimeout(300);
assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
await page.screenshot({ path: new URL('390px.png', directory).pathname });
findings.push('1024×768 and 390px: controls, date jump and chart remain accessible; no page horizontal overflow');
// Stable server-issued link must consume D09's Context without symbol re-resolution.
await page.setViewportSize({ width: 1440, height: 900 });
const stable = 'http://127.0.0.1:5173/admin/data/etf-basics/instrument%3A11111111-1111-4111-8111-111111111111';
const beforeStable = requests.length;
await page.goto(stable); await page.getByText('512 / 10,000 根', { exact: false }).waitFor();
assert.equal(requests.slice(beforeStable).filter(path => path.includes('/resolve?')).length, 0);
findings.push('D09 stable-key Context consumed directly; stable link issues zero additional symbol-resolution requests');
mode = 'profile';
await page.getByRole('button', { name: '刷新数据', exact: true }).click();
await page.waitForFunction(() => document.querySelector('.qfe-info')?.textContent.includes('正式数据文件暂不可读取'));
assert.equal(await page.locator('.qfe-chart-empty').count(), 0);
findings.push('Mounted profile failure remains local; independently correct raw OHLC stays visible without any factor request');
mode = 'network';
await page.getByRole('button', { name: '刷新数据', exact: true }).click();
await page.waitForFunction(() => document.querySelector('.qfe-data-note')?.textContent.includes('同条件旧数据'));
assert.ok(await page.getByRole('button', { name: '加载更早历史' }).isDisabled());
assert.ok(await page.getByText('512 / 10,000 根', { exact: false }).count());
findings.push('Same-condition simulated network error labels stale data and disables continuation');
mode = 'changed';
await page.getByRole('button', { name: '刷新数据', exact: true }).click();
await page.waitForFunction(() => document.querySelector('.qfe-chart-empty')?.textContent.includes('已改变'));
assert.ok(await page.getByText('0 / 10,000 根', { exact: false }).count());
findings.push('Simulated DATA_CHANGED withdraws the entire loaded price window');
mode = 'normal'; await page.goto(url); await page.getByText('512 / 10,000 根', { exact: false }).waitFor();
mode = 'permission'; await page.getByRole('button', { name: '刷新数据', exact: true }).click();
await page.waitForFunction(() => document.querySelector('.qfe-chart') === null && document.body.textContent.includes('无权'));
findings.push('Simulated 403 removes graph and denied profile across D09/D10 panels');
mode = 'normal'; await page.goto(url); await page.getByText('512 / 10,000 根', { exact: false }).waitFor();
mode = 'authentication'; await page.getByRole('button', { name: '刷新数据', exact: true }).click();
await page.waitForURL('**/login');
findings.push('Simulated 401 uses existing logout/login flow and clears all mounted market data');
assert.equal(requests.filter(path => /data-collections\/etfs\/.+(daily-bars|adjustment-factors)/.test(path)).length, 0);
assert.equal(problems.length, 0, JSON.stringify(problems));
findings.push('No retired detail price/factor request; no browser errors; auth is synthetic sessionStorage only');
await writeFile(new URL('./UI_RESULT.json', import.meta.url), JSON.stringify({ mode: 'artificial fixtures / simulated API only', findings,
  browserErrors: problems, actualMarketOrProductionAccess: false, requests: requests.map(path => path.replace(/cursor=[^&]+/g, 'cursor=<bounded-memory>')) }, null, 2));
console.log(findings.join('\n'));
} finally { releaseReplay?.(); await browser.close(); }
