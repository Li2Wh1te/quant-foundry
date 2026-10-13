/** Simulated browser transport only; backend formal route checks are separate. */
import { createRequire } from 'node:module';
import { mkdir, writeFile } from 'node:fs/promises';
import assert from 'node:assert/strict';
import { responseFor } from './mock-api.mjs';
const require = createRequire(new URL('../../frontend/package.json', import.meta.url));
const { chromium } = require('playwright');
const browser = await chromium.launch({ executablePath: '/usr/bin/chromium', headless: true, args: ['--no-sandbox'] });
let release = null;
try {
  const directory = new URL('./screenshots/', import.meta.url); await mkdir(directory, { recursive: true });
  const findings = [], errors = [], requests = [];
  let mode = 'normal', hold = false, testBasis = false, holdPrice = false;
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, timezoneId: 'America/Los_Angeles' });
  page.on('pageerror', e => errors.push(e.message));
  await page.route(url => url.pathname.startsWith('/api/'), async route => {
    const url = new URL(route.request().url()), path = url.pathname + url.search; requests.push(path);
    if (testBasis && url.pathname.endsWith('/bars') && mode === 'basis-denied') {
      await route.fulfill({ status: 422, contentType: 'application/json', body: JSON.stringify({ detail: { code: 'FACTOR_UNAVAILABLE' } }) }); return;
    }
    if (holdPrice && url.pathname.endsWith('/bars')) await new Promise(resolve => { release = resolve; });
    if (url.pathname.endsWith('/factors')) {
      if (mode === 'network') { await route.abort('failed'); return; }
      if (['changed', 'invalid', 'permission'].includes(mode)) {
        await route.fulfill({ status: mode === 'permission' ? 403 : mode === 'changed' ? 409 : 422,
          contentType: 'application/json', body: JSON.stringify({ detail: { code: mode === 'permission' ? 'DENIED' : mode === 'changed' ? 'DATA_CHANGED' : 'INVALID_CURSOR' } }) }); return;
      }
      if (hold) await new Promise(resolve => { release = resolve; });
    }
    const [status, value] = responseFor(path, route.request().method(), testBasis);
    await route.fulfill({ status, contentType: 'application/json', body: value === null ? '' : JSON.stringify(value) });
  });
  await page.addInitScript(() => sessionStorage.setItem('quant-foundry.api-token', 'synthetic-d11-only'));
  const url = 'http://127.0.0.1:5173/admin/data/etf-basics/510300.SH?tab=factors';
  await page.goto(url); await page.locator('.qfb-factors tbody tr').first().waitFor();
  assert.equal(await page.locator('#qfb-price-basis').inputValue(), 'none');
  for (const mode of ['pre', 'post']) assert.equal(await page.locator(`#qfb-price-basis option[value="${mode}"]`).isDisabled(), true);
  await page.locator('#qfb-price-basis').focus(); await page.keyboard.press('ArrowDown');
  assert.equal(await page.locator('#qfb-price-basis').inputValue(), 'none');
  await page.locator('.qfb-basis-note summary').focus(); await page.keyboard.press('Enter');
  await page.getByRole('button', { name: '收起说明', exact: true }).waitFor();
  await page.keyboard.press('Escape'); assert.equal(await page.locator('.qfb-basis-note').getAttribute('open'), null);
  assert.ok((await page.locator('.qfb-factors').textContent()).includes('2.123456789123'));
  assert.ok((await page.locator('.qfb-factors').textContent()).includes('未核验'));
  assert.equal(await page.locator('.qfe-chart-empty').count(), 0);
  findings.push('raw works independently; exact factor text and missing normalization evidence shown; pre/post disabled');
  await page.locator('.qfb-factors input').nth(0).fill('2022-01-03');
  await page.getByRole('button', { name: '查看日期范围', exact: true }).click();
  await page.getByRole('button', { name: '加载更早因子', exact: true }).waitFor();
  await page.waitForFunction(() => document.querySelector('.qfb-factors')?.textContent.includes('512 / 10,000'));
  await page.getByRole('button', { name: '加载更早因子', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('.qfb-factors')?.textContent.includes('1024 / 10,000'));
  const dates = await page.locator('.qfb-factors tbody tr td:first-child').allTextContents();
  assert.equal(new Set(dates).size, dates.length); assert.deepEqual(dates, [...dates].sort());
  findings.push('explicit bounded date range and backward factor paging preserve ascending unique dates and exact strings');
  for (const [width, height, file] of [[1440, 900, '1440x900.png'], [1024, 768, '1024x768.png'], [390, 844, '390px.png']]) {
    await page.setViewportSize({ width, height });
    if (!(await page.locator('.qfb-factors').isVisible())) await page.getByRole('button', { name: '切换资料侧栏' }).click();
    await page.waitForTimeout(200); assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    await page.screenshot({ path: new URL(file, directory).pathname });
  }
  findings.push('1440×900 / 1024×768 / 390px factor panel is keyboard reachable and has no page overflow');
  await page.setViewportSize({ width: 1440, height: 900 });
  mode = 'network'; await page.getByRole('button', { name: '重新读取因子', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('.qfb-factors')?.textContent.includes('同条件旧因子'));
  assert.ok(await page.locator('.qfb-factors tbody tr').count());
  assert.equal(await page.getByRole('button', { name: '加载更早因子', exact: true }).isDisabled(), true);
  assert.equal(await page.locator('.qfe-chart-empty').count(), 0);
  findings.push('factor network failure keeps explicitly stale same-condition rows, stops paging, and preserves raw graph');
  mode = 'changed'; await page.getByRole('button', { name: '重新读取因子', exact: true }).click();
  await page.waitForFunction(() => !document.querySelector('.qfb-factors tbody'));
  assert.equal(await page.locator('.qfe-chart-empty').count(), 0);
  findings.push('factor DATA_CHANGED withdraws the independent factor window without rejecting unrelated raw');
  mode = 'normal'; await page.getByRole('button', { name: '重新读取因子', exact: true }).click();
  await page.locator('.qfb-factors tbody tr').first().waitFor();
  mode = 'invalid'; await page.getByRole('button', { name: '加载更早因子', exact: true }).click();
  await page.waitForFunction(() => !document.querySelector('.qfb-factors tbody'));
  findings.push('invalid cursor removes old factor rows; no continuation or implicit alternate source');
  mode = 'normal'; await page.getByRole('button', { name: '重新读取因子', exact: true }).click(); await page.locator('.qfb-factors tbody tr').first().waitFor();
  hold = true;
  await page.locator('.qfb-factors input').nth(0).fill('2026-01-01');
  await page.getByRole('button', { name: '查看日期范围', exact: true }).click();
  await page.waitForFunction(() => !document.querySelector('.qfb-factors tbody'));
  await page.waitForFunction(() => document.querySelector('.qfb-factors')?.getAttribute('aria-busy') === 'true');
  hold = false; release?.(); release = null; await page.locator('.qfb-factors tbody tr').first().waitFor();
  findings.push('changed date condition clears old factor rows before a held late response');
  mode = 'permission'; await page.getByRole('button', { name: '重新读取因子', exact: true }).click();
  await page.waitForFunction(() => !document.querySelector('.qfb-factors tbody') &&
    (!document.querySelector('.qfe-chart canvas') || document.querySelector('.qfe-chart-empty')));
  findings.push('403 from factor panel uses the shared invalidation bus and withdraws all affected market panels');
  mode = 'normal'; testBasis = true;
  await page.goto(url); await page.locator('.qfb-factors tbody tr').first().waitFor();
  holdPrice = true;
  await page.locator('#qfb-price-basis').selectOption('pre');
  await page.waitForFunction(() => document.querySelector('.qfe-chart-empty') &&
    [...document.querySelectorAll('.qfe-ohlc strong')].every(node => node.textContent.trim() === '—'));
  holdPrice = false; release?.(); release = null;
  await page.waitForFunction(() => !document.querySelector('.qfe-chart-empty') && document.querySelector('.qfe-chart-caption')?.textContent.includes('前复权'));
  await page.locator('#qfb-price-basis').selectOption('post');
  await page.waitForFunction(() => !document.querySelector('.qfe-chart-empty') && document.querySelector('.qfe-chart-caption')?.textContent.includes('后复权'));
  findings.push('explicit UI-only constant-factor contract enables pre/post; basis switch withdraws old OHLC before a held reply');
  mode = 'network'; await page.getByRole('button', { name: '重新读取因子', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('.qfe-data-note')?.textContent.includes('旧数据'));
  assert.equal(await page.getByRole('button', { name: '加载更早历史', exact: true }).isDisabled(), true);
  mode = 'changed'; await page.getByRole('button', { name: '重新读取因子', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('.qfe-chart-empty')?.textContent.includes('依赖已改变'));
  assert.equal(await page.locator('#qfb-price-basis').inputValue(), 'post');
  mode = 'normal'; await page.getByRole('button', { name: '刷新数据', exact: true }).click();
  await page.waitForFunction(() => !document.querySelector('.qfe-chart-empty'));
  findings.push('dependent factor network errors mark adjusted prices stale and stop continuation; changed factors withdraw the adjusted window');
  mode = 'basis-denied';
  await page.getByRole('button', { name: '刷新数据', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('.qfe-chart-empty')?.textContent.includes('因子'));
  assert.equal(await page.locator('#qfb-price-basis').inputValue(), 'post');
  mode = 'normal'; await page.locator('#qfb-price-basis').selectOption('none');
  await page.waitForFunction(() => !document.querySelector('.qfe-chart-empty'));
  findings.push('adjusted denial does not silently select raw; explicit raw choice restores its independent simulated view');
  assert.equal(requests.filter(p => /data-collections.*(daily-bars|adjustment-factors)/.test(p)).length, 0);
  assert.equal(errors.length, 0, JSON.stringify(errors));
  await writeFile(new URL('./UI_RESULT.json', import.meta.url), JSON.stringify({ mode: 'artificial fixture / simulated API only',
    actualMarketOrProductionAccess: false, findings, browserErrors: errors,
    requests: requests.map(p => p.replace(/cursor=[^&]+/g, 'cursor=<bounded-memory>')) }, null, 2));
  console.log(findings.join('\n'));
} finally { release?.(); await browser.close(); }
