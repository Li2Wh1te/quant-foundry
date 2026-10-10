/* Actual project API under a closed isolated maintenance receipt; no business data. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base = 'http://127.0.0.1:5198';
const output = process.env.QF_D07_NATIVE_SCREENSHOTS;
fs.mkdirSync(output, { recursive: true });
(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium', args: ['--no-sandbox'] });
  const errors = [], requests = [], cases = [];
  try {
    for (const width of [1440, 1024, 390]) {
      const c = await browser.newContext({ viewport: { width, height: width === 1440 ? 900 : width === 1024 ? 768 : 844 } });
      await c.addInitScript(token => {
        if (location.hostname !== '127.0.0.1') return;
        sessionStorage.setItem('quant-foundry.api-token', token);
        localStorage.setItem('qfo-sidebar', innerWidth > 980 ? 'expanded' : 'collapsed');
        addEventListener('DOMContentLoaded', () => {
          const badge = document.createElement('div'); badge.textContent = '原生 HTTP · 隔离维护门禁 · 无业务数据';
          badge.style.cssText = 'position:fixed;right:8px;bottom:8px;z-index:9999;pointer-events:none;padding:4px 8px;background:#fffaf2;border:1px solid #d3b9a1;border-radius:5px;font:14px/22px sans-serif';
          document.body.append(badge);
        });
      }, process.env.QF_API_TOKEN);
      // No route substitution: the real Vite proxy reaches the real FastAPI app.
      await c.route(url => url.hostname !== '127.0.0.1', route => route.abort());
      const p = await c.newPage(); p.on('pageerror', e => errors.push(e.message));
      p.on('request', r => { const url = new URL(r.url()); if (url.pathname.startsWith('/api/')) requests.push({ method: r.method(), path: url.pathname }); });
      try {
        await p.goto(`${base}/admin/data-assets`);
        await p.waitForFunction(() => document.querySelector('.qf-catalog')?.getAttribute('aria-busy') === 'false');
        assert.ok(await p.locator('.qf-catalog tbody tr').count() > 0);
        assert.equal(await p.locator('.qf-catalog tbody').getByText('暂不可读取／处理中', { exact: true }).count() > 0, true);
        assert.equal(await p.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true);
        await p.screenshot({ path: path.join(output, `native-catalog-${width}.jpg`), type: 'jpeg', quality: 85 });
        await p.goto(`${base}/admin/data-assets/${encodeURIComponent(process.env.QF_D07_NATIVE_DATASET)}?view=overview`);
        await p.getByRole('heading', { name: '能提供什么', exact: true }).waitFor();
        assert.ok(await p.locator('.qf-dataset-fields tbody tr').count() > 0);
        await p.screenshot({ path: path.join(output, `native-overview-${width}.jpg`), type: 'jpeg', quality: 85 });
        await p.getByRole('button', { name: '查看当前数据', exact: true }).click();
        await p.getByText(/暂无可预填对象/).waitFor();
        assert.equal(await p.locator('.qf-preview-result').count(), 0);
        assert.equal(await p.locator('#qf-assets-panel-preview').getByRole('button', { name: '查看当前数据', exact: true }).isDisabled(), true);
        await p.screenshot({ path: path.join(output, `native-preview-gate-${width}.jpg`), type: 'jpeg', quality: 85 });
        await p.getByRole('tab', { name: '更新与问题', exact: true }).click();
        await p.getByText('当前筛选没有已登记的问题记录；不代表业务覆盖或质量已经确认。', { exact: true }).waitFor();
        await p.screenshot({ path: path.join(output, `native-updates-${width}.jpg`), type: 'jpeg', quality: 85 });
        cases.push(`${width}px native catalog/schema, closed preview, D06 empty issue collection`);
      } finally { await c.close(); }
    }
    assert.deepEqual(errors, []); assert.ok(requests.every(r => r.method === 'GET'));
    fs.writeFileSync(path.join(output, 'native-browser.json'), JSON.stringify({ source: 'Actual project HTTP; isolated closed receipt; no business rows',
      passed: cases.length, cases, errors, requests, realDataAcceptance: false }, null, 2) + '\n');
    console.log(`Native browser: ${cases.length} viewport flows passed; zero page errors; GET only.`);
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
