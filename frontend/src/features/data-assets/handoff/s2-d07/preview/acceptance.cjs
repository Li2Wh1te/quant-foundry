/* Browser integration of the combined branch. Every value below is synthetic. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.FRONTEND_URL || 'http://127.0.0.1:5195';
const fixture = process.env.FIXTURE_URL || 'http://127.0.0.1:18768';
for (const url of [base, fixture]) assert.equal(new URL(url).hostname, '127.0.0.1', 'Loopback fixture only');
const output = process.env.SCREENSHOT_DIR || path.resolve(__dirname, '../screenshots');
fs.mkdirSync(output, { recursive: true });
const cases = [], errors = [], requests = [], geometry = [];
const title = kind => `隔离示例：${{ daily: '日线', nav: '净值', report: '报告片段' }[kind]}（来源报告值）`;
const table = p => p.getByRole('region', { name: '当前对象示例表格', exact: true });
const panel = p => p.locator('#qf-assets-panel-preview');
async function scenario(name) {
  const res = await fetch(`${fixture}/__scenario?name=${encodeURIComponent(name)}`);
  assert.equal(res.status, 200); assert.equal((await res.json()).scenario, name);
}
async function check(name, run) { await run(); cases.push(name); console.log(`PASS: ${name}`); }
async function shot(p, name) {
  await p.screenshot({ path: path.join(output, `${name}.jpg`), type: 'jpeg', quality: 85, animations: 'disabled' });
}
async function context(browser, width = 390) {
  const c = await browser.newContext({ viewport: { width, height: width === 1440 ? 900 : width === 1024 ? 768 : 844 } });
  await c.addInitScript(() => {
    if (location.hostname !== '127.0.0.1') return;
    sessionStorage.setItem('quant-foundry.api-token', 's2-d07-public-ui-fixture');
    localStorage.setItem('qfo-sidebar', innerWidth > 980 ? 'expanded' : 'collapsed');
    addEventListener('DOMContentLoaded', () => {
      const badge = document.createElement('div'); badge.textContent = 'D07 隔离夹具 · 非真实数据验收';
      badge.style.cssText = 'position:fixed;bottom:8px;right:8px;z-index:9999;pointer-events:none;padding:4px 8px;border:1px solid #d3b9a1;border-radius:5px;background:#fffaf2;color:#77511f;font:14px/22px sans-serif';
      document.body.append(badge);
    });
  });
  await c.route(url => url.hostname !== '127.0.0.1', route => route.abort());
  c.on('page', p => {
    p.on('pageerror', e => errors.push(e.message));
    p.on('request', r => {
      const url = new URL(r.url());
      if (url.pathname.startsWith('/api/')) requests.push({ method: r.method(), path: url.pathname, query: url.search,
        ...(r.method() === 'POST' ? { body: r.postDataJSON() } : {}) });
    });
  });
  return c;
}
async function open(p, kind = 'daily', view = 'preview') {
  await p.goto(`${base}/admin/data-assets/fixture.${kind}?view=${view}`);
  await p.getByRole('heading', { name: title(kind), exact: true }).waitFor();
}
async function openRead(p, kind = 'daily') { await scenario('normal'); await open(p, kind); await table(p).waitFor(); }
async function refresh(p) {
  const response = p.waitForResponse(r => new URL(r.url()).pathname.includes('/data-store/datasets/'));
  await p.getByRole('button', { name: '刷新页面', exact: true }).click(); await response;
  await p.waitForFunction(() => document.querySelector('.qf-assets')?.getAttribute('aria-busy') === 'false');
}
async function measure(p, name) {
  const g = await p.evaluate(() => ({ viewport: innerWidth, document: document.documentElement.scrollWidth,
    mainOverflow: getComputedStyle(document.querySelector('.qfo-main')).overflowY,
    body: getComputedStyle(document.querySelector('.qf-assets')).fontSize,
    tables: [...document.querySelectorAll('.qf-assets-scroll')].filter(e => !e.closest('[hidden]')).map(e =>
      ({ width: e.clientWidth, content: e.scrollWidth, height: e.clientHeight, contentHeight: e.scrollHeight })) }));
  assert.ok(g.document <= g.viewport + 1, `${name}: document overflow`); assert.equal(g.mainOverflow, 'auto');
  assert.equal(g.body, '14px'); for (const t of g.tables) assert.ok(t.contentHeight <= t.height + 1);
  geometry.push({ name, ...g });
}
function queryCount() { return requests.filter(r => r.method === 'POST').length; }
async function storage(p) {
  const state = await p.evaluate(() => ({ url: location.href,
    session: Object.fromEntries(Object.entries(sessionStorage).filter(([k]) => k !== 'quant-foundry.api-token')) }));
  assert.doesNotMatch(state.url, /cursor=|token=|subject=|from_key=|to_key=/);
  assert.doesNotMatch(JSON.stringify(state.session), /fixture-cursor|12345678901234567890|fixture-daily-object|1791619200123456789/);
}
(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium', args: ['--no-sandbox'] });
  try {
    for (const width of [1440, 1024, 390]) await check(`${width}px: catalog → overview → automatic bounded preview → updates → return; keyboard and precision`, async () => {
      await scenario('normal'); const c = await context(browser, width), p = await c.newPage();
      try {
        await p.goto(`${base}/admin/data-assets?search=&status=all&source=all&frequency=all&page=1`);
        await p.getByLabel('搜索数据集', { exact: true }).fill('日线');
        const link = p.getByRole('link', { name: title('daily'), exact: true }); await link.waitFor();
        await measure(p, `catalog-${width}`); await shot(p, `catalog-${width}`);
        await p.locator('.qfo-main').evaluate(e => { e.scrollTop = 250; });
        await link.press('Enter'); await p.getByRole('heading', { name: title('daily'), exact: true }).waitFor();
        assert.equal(await p.getByRole('link', { name: '数据资产', exact: true }).getAttribute('aria-current'), 'page');
        // D04's status belongs to the header, not the count metric band.
        assert.match(await p.locator('.qf-assets-heading-status').innerText(), /当前可用/);
        await measure(p, `overview-${width}`); await shot(p, `overview-${width}`);
        const initial = queryCount(); const overview = p.getByRole('tab', { name: '概览', exact: true });
        await overview.focus(); await overview.press('ArrowRight'); await table(p).waitFor();
        assert.equal(queryCount() - initial, 1, 'D05 navigation is a single user action');
        const previewTab = p.getByRole('tab', { name: '数据预览', exact: true });
        assert.equal(await previewTab.evaluate(e => e === document.activeElement), true);
        assert.notEqual(await previewTab.evaluate(e => getComputedStyle(e).outlineStyle), 'none');
        for (const value of ['12345678901234567890.123456789012345678', '900719925474099312345', '1791619200123456789', '—', 'false', '0']) {
          assert.ok(await table(p).getByRole('cell', { name: value, exact: true }).count() > 0, value);
        }
        assert.match(await panel(p).innerText(), /业务日期覆盖未验证/);
        if (width === 390) {
          await table(p).focus(); await table(p).press('ArrowRight');
          await p.waitForFunction(() => document.querySelector('.qf-preview-scroll').scrollLeft > 0);
          await table(p).evaluate(e => { e.scrollLeft = 0; });
        }
        await measure(p, `preview-${width}`); await shot(p, `preview-${width}`);
        await panel(p).getByRole('button', { name: '下一页', exact: true }).click();
        await panel(p).getByText('第 2 页 · 本页 1 行', { exact: true }).waitFor();
        await previewTab.press('End'); await p.locator('#qf-assets-panel-updates h2').first().waitFor();
        await measure(p, `updates-${width}`); await shot(p, `updates-${width}`);
        await p.getByRole('tab', { name: '更新与问题', exact: true }).press('Home');
        await p.getByRole('button', { name: '查看当前数据', exact: true }).click();
        await panel(p).getByText('第 2 页 · 本页 1 行', { exact: true }).waitFor();
        assert.equal(queryCount() - initial, 2, 'Completed pages survive same-object tabs without a reread');
        await storage(p); await p.getByRole('link', { name: '← 数据资产', exact: true }).press('Enter');
        await link.waitFor(); const params = new URL(p.url()).searchParams;
        for (const [key, value] of [['search', '日线'], ['status', 'all'], ['source', 'all'], ['frequency', 'all'], ['page', '1']]) assert.equal(params.get(key), value);
      } finally { await c.close(); }
    });
    const c = await context(browser), p = await c.newPage();
    try {
      for (const [name, text] of [
        ['unknown-descriptor', '状态待确认'],
        ['dataset-empty', '当前数据集已登记，当前物理存储为空；不说明来源或市场上没有这种数据。'],
        ['not-checked', '本地来源尚未检查，不能将尚未检查视为空。']
      ]) await check(`${name}: synchronized D04 presentation assertion`, async () => {
        await scenario(name); await open(p, 'daily', 'overview');
        if (name === 'unknown-descriptor') assert.equal(await p.locator('.qf-assets-heading-status').innerText(), text);
        else await p.getByText(text, { exact: true }).waitFor();
        await shot(p, `${name}-390`);
      });
      await check('report pages replace each other and remain explicitly incomplete', async () => {
        await openRead(p, 'report'); await panel(p).getByText(/此页不代表完整报告/).waitFor();
        assert.equal(await table(p).locator('tbody tr').count(), 2);
        await panel(p).getByRole('button', { name: '下一页', exact: true }).click();
        await panel(p).getByText('第 2 页 · 本页 1 行', { exact: true }).waitFor();
        assert.equal(await table(p).locator('tbody tr').count(), 1); await shot(p, 'report-page-2-390');
      });
      await check('D06 issue paging survives preview tabs, distinguishes counts and rejects a changed inventory', async () => {
        await scenario('issues-paged'); await open(p, 'daily', 'updates');
        const updates = p.locator('.qf-updates');
        await updates.getByText('第 1 页 · 记录 1–20 / 45', { exact: true }).waitFor();
        assert.equal(await updates.locator('.qf-updates-records > li').count(), 20);
        assert.deepEqual(await updates.locator('.qf-updates-counts dd').allTextContents(), ['45', '90', '20']);
        await updates.getByRole('button', { name: '下一页问题', exact: true }).click();
        await updates.getByText('第 2 页 · 记录 21–40 / 45', { exact: true }).waitFor();
        await shot(p, 'issues-page-2-390');
        const before = queryCount();
        await updates.getByRole('link', { name: '调整当前查询', exact: true }).click(); await table(p).waitFor();
        assert.equal(queryCount(), before + 1);
        await p.getByRole('tab', { name: '更新与问题', exact: true }).click();
        await updates.getByText('第 2 页 · 记录 21–40 / 45', { exact: true }).waitFor();
        await updates.getByRole('button', { name: '重新读取问题', exact: true }).click();
        await updates.getByText('第 1 页 · 记录 1–20 / 45', { exact: true }).waitFor();
        await scenario('issues-paged-changed'); await updates.getByRole('button', { name: '下一页问题', exact: true }).click();
        await updates.getByRole('alert').waitFor(); assert.equal(await updates.locator('.qf-updates-records > li').count(), 0);
        assert.equal(await updates.getByRole('button', { name: '下一页问题', exact: true }).count(), 0);
        await shot(p, 'issues-inventory-changed-390');
      });
      for (const name of ['metadata-maintenance-error', 'metadata-restricted-error', 'metadata-changed-error', 'metadata-rebuild-error', 'metadata-missing-error']) {
        await check(`${name}: detail GET refusal unmounts old preview and page stack`, async () => {
          await openRead(p); await scenario(name); const before = queryCount(); await refresh(p);
          await p.getByRole('alert').waitFor(); assert.equal(await p.locator('.qf-preview-result').count(), 0);
          assert.equal(await table(p).count(), 0); assert.equal(queryCount(), before);
          assert.equal(await p.getByRole('button', { name: '查看当前数据', exact: true }).count(), 0);
          await shot(p, `${name}-390`);
        });
      }
      for (const name of ['metadata-generation', 'metadata-quality', 'metadata-schema', 'metadata-subject']) await check(`${name}: changed descriptor clears the old page without POST`, async () => {
        await openRead(p); await scenario(name); const before = queryCount(); await refresh(p);
        await panel(p).getByRole('alert').waitFor(); assert.equal(await table(p).count(), 0); assert.equal(queryCount(), before);
      });
      for (const name of ['generation-next', 'restriction-next', 'query-changed', 'query-maintenance']) await check(`${name}: authoritative query clears results and never mixes pages`, async () => {
        await openRead(p); await scenario(name); await panel(p).getByRole('button', { name: '下一页', exact: true }).click();
        await panel(p).getByRole('alert').waitFor(); assert.equal(await p.locator('.qf-preview-result').count(), 0);
        await shot(p, `${name}-390`);
      });
      await check('query cancellation and leaving preview reject an actual delayed HTTP response', async () => {
        await scenario('slow'); await open(p); await panel(p).getByRole('button', { name: '取消读取', exact: true }).click();
        await panel(p).getByText(/本次读取已取消/).waitFor(); await p.waitForTimeout(1400); assert.equal(await table(p).count(), 0);
        await scenario('slow'); await panel(p).getByRole('button', { name: '查看当前数据', exact: true }).click();
        await p.getByRole('tab', { name: '概览', exact: true }).click(); await p.waitForTimeout(1400);
        await p.getByRole('tab', { name: '数据预览', exact: true }).click(); assert.equal(await table(p).count(), 0);
        await shot(p, 'cancelled-390');
      });
      await check('condition change and SPA object switch cancel the old request without leaking it', async () => {
        await openRead(p); await panel(p).getByText('调整查询', { exact: true }).click();
        await scenario('slow'); await panel(p).getByRole('button', { name: '查看当前数据', exact: true }).click();
        await p.getByLabel('对象键（文本）', { exact: true }).fill('new-fixture-condition'); await p.waitForTimeout(1400);
        assert.equal(await table(p).count(), 0); await storage(p);
        await scenario('slow'); await panel(p).getByRole('button', { name: '查看当前数据', exact: true }).click();
        await p.getByRole('link', { name: '← 数据资产', exact: true }).click();
        await p.getByRole('link', { name: title('nav'), exact: true }).click();
        await p.getByRole('heading', { name: title('nav'), exact: true }).waitFor(); await p.waitForTimeout(1400);
        assert.equal(await p.locator('.qf-preview-result').count(), 0); await shot(p, 'object-switched-390');
      });
      for (const name of ['query-forbidden', 'metadata-forbidden', 'issues-forbidden']) await check(`${name}: 403 revokes every mounted cache; GET recovery does not reread`, async () => {
        if (name.startsWith('issues')) { await scenario('issues-readable'); await open(p); await table(p).waitFor(); }
        else await openRead(p);
        await scenario(name); const before = queryCount();
        if (name.startsWith('query')) await panel(p).getByRole('button', { name: '查看当前数据', exact: true }).click();
        else await refresh(p);
        await p.getByRole('alert').first().waitFor(); assert.equal(await p.locator('.qf-preview-result').count(), 0);
        assert.equal(await p.getByRole('heading', { name: title('daily'), exact: true }).count(), 0);
        await shot(p, `${name}-390`); await scenario('normal'); await refresh(p);
        await p.getByRole('heading', { name: title('daily'), exact: true }).waitFor();
        assert.equal(queryCount(), before + (name.startsWith('query') ? 1 : 0)); assert.equal(await table(p).count(), 0);
      });
      await check('401 clears the existing login token and all mounted data', async () => {
        await openRead(p); await scenario('metadata-unauthorized');
        await p.getByRole('button', { name: '刷新页面', exact: true }).click(); await p.waitForURL(`${base}/login`);
        assert.equal(await p.evaluate(() => sessionStorage.getItem('quant-foundry.api-token')), null);
        assert.equal(await p.locator('.qf-assets').count(), 0); await shot(p, 'unauthorized-390');
      });
    } finally { await c.close(); }
    await check('existing workspace entrances and quick jump remain navigable with scoped styles', async () => {
      await scenario('normal'); const c = await context(browser, 1440), p = await c.newPage();
      try {
        await p.goto(`${base}/admin/data-assets`); await p.locator('.qf-catalog').waitFor();
        for (const [label, route] of [['总览', '/admin'], ['数据源', '/admin/data-sources'], ['采集任务', '/admin/tasks'],
          ['A 股市场', '/admin/data/etf-basics'], ['策略工作台', '/admin/strategies'], ['回测工作台', '/admin/backtest-runs']]) {
          await p.locator('.qfo-nav-item').getByText(label, { exact: true }).click(); await p.waitForURL(`${base}${route}`);
          await p.locator('.qfo-main').waitFor(); assert.equal(await p.locator('.qf-assets').count(), 0);
        }
        await p.keyboard.press('Control+k'); await p.getByRole('combobox', { name: '搜索页面或功能', exact: true }).fill('数据资产');
        await p.keyboard.press('Enter'); await p.waitForURL(`${base}/admin/data-assets`); await p.locator('.qf-catalog').waitFor();
        const before = requests.filter(r => r.path.includes('/data-store/')).length;
        await p.waitForTimeout(1600); assert.equal(requests.filter(r => r.path.includes('/data-store/')).length, before, 'No new background polling');
        await shot(p, 'navigation-1440');
      } finally { await c.close(); }
    });
    assert.deepEqual(errors, []);
    assert.ok(requests.every(r => r.method === 'GET' || r.method === 'POST' && r.path === '/api/admin/data-store/query'));
    for (const r of requests.filter(r => r.body)) {
      assert.equal(r.body.allow_partial, false); assert.ok(r.body.page_size <= 100); assert.ok(r.body.columns.length <= 32);
      assert.notEqual(r.body.representation, 'typed-object-nodes-v2');
    }
    fs.writeFileSync(path.join(output, 'acceptance.json'), JSON.stringify({ source: 'D07 synthetic HTTP fixture', passed: cases.length,
      cases, errors, geometry, requests, realDataAcceptance: false, productionOperations: false }, null, 2) + '\n');
    console.log(`D07 integration: ${cases.length} passed; zero page errors; GET and bounded POST /query only.`);
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
