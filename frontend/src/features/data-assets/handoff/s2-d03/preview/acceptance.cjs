/* Actual-browser D03 acceptance against the loopback fixture; no production IO. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.FRONTEND_URL || 'http://127.0.0.1:5179';
const fixture = 'http://127.0.0.1:18766';
assert.ok(['127.0.0.1', 'localhost'].includes(new URL(base).hostname), 'Loopback preview only');
const output = process.env.SCREENSHOT_DIR || path.resolve(__dirname, '../screenshots');
fs.mkdirSync(output, { recursive: true });
const results = [], errors = [], requests = [], geometryResults = [], denialTimings = [];
const sourceB = '隔离示例：来源 B';
const listRows = p => p.locator('.qf-catalog tbody tr');
const title = id => `隔离示例：数据集 ${String(id).padStart(3, '0')}`;
const root = p => p.locator('.qf-catalog');
async function scenario(name) {
  const response = await fetch(`${fixture}/__scenario?name=${encodeURIComponent(name)}`);
  assert.equal(response.status, 200); assert.equal((await response.json()).scenario, name);
}
async function check(name, run) { await run(); results.push(name); console.log(`PASS: ${name}`); }
async function ready(p) {
  await p.waitForFunction(() => document.querySelector('.qf-catalog')?.getAttribute('aria-busy') === 'false');
}
async function refresh(p, button = '刷新页面') {
  // Wait for the new request before inspecting aria-busy. Otherwise a fast
  // click can still observe the previous idle render before React's effect.
  const response = p.waitForResponse(res => new URL(res.url()).pathname === '/api/admin/data-store/datasets');
  await p.getByRole('button', { name: button, exact: true }).click();
  await response; await ready(p);
}
async function open(p, query = '') { await p.goto(`${base}/admin/data-assets${query ? `?${query}` : ''}`); await ready(p); }
async function metrics(p) { return p.locator('.qf-catalog .qf-assets-metrics strong').allTextContents(); }
async function choose(p, label, option) {
  await p.getByRole('combobox', { name: label, exact: true }).click();
  await p.getByRole('option', { name: option, exact: true }).click();
  await p.getByRole('combobox', { name: label, exact: true }).filter({ hasText: option }).waitFor();
}
async function screenshot(p, name, top = true) {
  if (top) await p.locator('.qfo-main').evaluate(e => { e.scrollTop = 0; });
  await p.screenshot({ path: path.join(output, `${name}.jpg`), type: 'jpeg', quality: 85, animations: 'disabled' });
}
async function geometry(p, name) {
  const value = await p.evaluate(() => {
    const main = document.querySelector('.qfo-main'), catalog = document.querySelector('.qf-catalog');
    return { viewport: innerWidth, document: document.documentElement.scrollWidth, mainOverflow: getComputedStyle(main).overflowY,
      body: getComputedStyle(catalog).fontSize, label: getComputedStyle(catalog.querySelector('label')).fontSize,
      tables: [...catalog.querySelectorAll('.qf-assets-scroll')].map(e => ({ width: e.clientWidth, contentWidth: e.scrollWidth,
        height: e.clientHeight, contentHeight: e.scrollHeight })) };
  });
  assert.ok(value.document <= value.viewport + 1); assert.equal(value.mainOverflow, 'auto');
  assert.equal(value.body, '14px'); assert.equal(value.label, '14px');
  for (const table of value.tables) assert.ok(table.contentHeight <= table.height + 1, 'Main owns vertical scroll');
  geometryResults.push({ name, ...value });
}
async function context(browser, width = 390) {
  const c = await browser.newContext({ viewport: { width, height: width === 1440 ? 900 : width === 1024 ? 768 : 844 },
    permissions: ['clipboard-read', 'clipboard-write'] });
  await c.addInitScript(expanded => {
    if (!['127.0.0.1', 'localhost'].includes(location.hostname)) return;
    sessionStorage.setItem('quant-foundry.api-token', 's2-d03-isolated-fixture');
    localStorage.setItem('qfo-sidebar', expanded ? 'expanded' : 'collapsed');
    addEventListener('DOMContentLoaded', () => {
      const badge = document.createElement('div'); badge.textContent = '隔离示例 · 非生产数据';
      badge.style.cssText = 'position:fixed;bottom:6px;right:8px;z-index:9999;padding:3px 8px;border:1px solid #d6b99a;border-radius:4px;background:#fff4e7;color:#77511f;font:12px/18px sans-serif;pointer-events:none';
      document.body.append(badge);
    });
  }, width > 980);
  await c.route(url => !['127.0.0.1', 'localhost'].includes(url.hostname), route => route.abort());
  c.on('page', p => {
    p.on('pageerror', error => errors.push(error.message));
    p.on('request', req => {
      const url = new URL(req.url());
      if (url.pathname.startsWith('/api/')) requests.push({ method: req.method(), path: url.pathname, query: url.search });
    });
  });
  return c;
}

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium', args: ['--no-sandbox'] });
  try {
    for (const width of [1440, 1024, 390]) {
      await check(`${width}px: stable catalog, separate facts, 20-item pages, detail and return context`, async () => {
        await scenario('normal'); const c = await context(browser, width), p = await c.newPage();
        try {
          await open(p, 'search=隔离示例&status=all&source=all&frequency=all&page=1');
          assert.equal(await listRows(p).count(), 20);
          assert.deepEqual(await metrics(p), ['60', '9', '9', '9']);
          const first = listRows(p).first();
          assert.equal(await first.getByRole('link').textContent(), title(1));
          assert.match(await first.locator('td').nth(3).textContent(), /当前可用（按服务端声明）/);
          assert.match(await first.locator('td').nth(4).textContent(), /本次更新失败/);
          assert.match(await first.locator('td').nth(2).textContent(), /已停用来源/);
          assert.equal(await first.locator('details').getAttribute('open'), null);
          await geometry(p, `catalog-${width}`); await screenshot(p, `catalog-${width}`);
          if (width === 390) {
            const sheet = p.getByRole('region', { name: '数据目录表格', exact: true });
            await sheet.focus(); await sheet.press('ArrowRight');
            await p.waitForFunction(() => document.querySelector('.qf-catalog .qf-assets-scroll').scrollLeft > 0);
            assert.notEqual(await sheet.evaluate(e => getComputedStyle(e).outlineStyle), 'none');
            await sheet.evaluate(e => { e.scrollLeft = 530; });
            await p.locator('.qfo-main').evaluate(e => { e.scrollTop = 610; });
            await screenshot(p, 'catalog-table-390', false);
            await sheet.evaluate(e => { e.scrollLeft = 0; });
          }
          await p.getByRole('button', { name: '下一页', exact: true }).click();
          await p.waitForURL(url => url.searchParams.get('page') === '2');
          await p.getByRole('link', { name: title(21), exact: true }).waitFor();
          await p.locator('.qfo-main').evaluate(e => { e.scrollTop = 340; });
          await p.getByRole('link', { name: title(21), exact: true }).click();
          await p.getByRole('heading', { name: title(21), exact: true }).waitFor();
          assert.equal(new URL(p.url()).searchParams.get('page'), '2');
          assert.equal(new URL(p.url()).searchParams.get('frequency'), 'all');
          const saved = await p.evaluate(() => [...Array(sessionStorage.length)].map((_, i) => sessionStorage.key(i))
            .filter(key => key.startsWith('qf-assets-scroll:') && key.includes('page=2')).map(key => Number(sessionStorage.getItem(key)))[0]);
          assert.ok(saved > 0);
          await p.goBack(); await ready(p);
          await p.waitForFunction(() => document.querySelector('.qfo-main').scrollTop > 0);
          assert.equal(new URL(p.url()).searchParams.get('page'), '2');
          assert.equal(await p.getByLabel('搜索数据集', { exact: true }).inputValue(), '隔离示例');
          await p.goForward(); await p.getByRole('heading', { name: title(21), exact: true }).waitFor();
          await p.getByRole('link', { name: '← 数据资产', exact: true }).click(); await ready(p);
          assert.equal(new URL(p.url()).searchParams.get('source'), 'all');
          assert.equal(new URL(p.url()).searchParams.get('page'), '2');
          await p.waitForFunction(() => document.querySelector('.qfo-main').scrollTop > 0);
        } finally { await c.close(); }
      });
    }
    const c = await context(browser), p = await c.newPage();
    try {
      await check('0/1 registrations: honest empty directory and single-page recovery', async () => {
        await scenario('empty'); await open(p); assert.deepEqual(await metrics(p), ['0', '0', '0', '0']);
        await p.getByText('当前目录未登记业务数据集。', { exact: true }).waitFor(); await screenshot(p, 'catalog-empty-390');
        await scenario('one'); await refresh(p);
        assert.equal(await listRows(p).count(), 1); assert.equal(await p.getByRole('button', { name: '下一页', exact: true }).isDisabled(), true);
      });
      await check('121 registrations: bounded completion and all seven pages without duplicates or omissions', async () => {
        await scenario('large'); await open(p); assert.equal((await metrics(p))[0], '121');
        const observed = [];
        for (let page = 1; page <= 7; page++) {
          // URL navigation can settle before React commits its new table.
          await p.getByRole('link', { name: title((page - 1) * 20 + 1), exact: true }).waitFor();
          observed.push(...await listRows(p).getByRole('link').allTextContents());
          assert.equal(await listRows(p).count(), page === 7 ? 1 : 20);
          if (page < 7) { await p.getByRole('button', { name: '下一页', exact: true }).click(); await p.waitForURL(url => url.searchParams.get('page') === String(page + 1)); }
        }
        assert.equal(observed.length, 121); assert.equal(new Set(observed).size, 121);
        assert.deepEqual(observed, Array.from({ length: 121 }, (_, i) => title(i + 1)));
        const log = (await (await fetch(`${fixture}/__requests`)).json()).requests;
        assert.ok(log.some(req => new URLSearchParams(req.query).get('offset') === '100'));
        assert.ok(log.filter(req => req.path.startsWith('/api/admin/data-store/')).every(req => req.path.endsWith('/datasets')));
      });
      await check('Chinese aliases, original names and encoded identifiers remain searchable', async () => {
        await scenario('alias'); await open(p);
        for (const text of ['股票日频', 'stock_daily', 'market.stock_daily']) {
          await p.getByLabel('搜索数据集', { exact: true }).fill(text);
          await p.getByRole('link', { name: '市场 · 股票日频', exact: true }).waitFor(); assert.equal(await listRows(p).count(), 1);
        }
        await scenario('normal'); await open(p, new URLSearchParams({ search: 'fixture.data / + 003' }).toString());
        const link = p.getByRole('link', { name: title(3), exact: true });
        assert.match(await link.getAttribute('href'), /fixture.data%20%2F%20%2B%20003/);
        await link.click(); await p.getByRole('heading', { name: title(3), exact: true }).waitFor();
        await p.getByRole('link', { name: '← 数据资产', exact: true }).click(); await ready(p);
        assert.equal(await p.getByLabel('搜索数据集', { exact: true }).inputValue(), 'fixture.data / + 003');
      });
      await check('source/frequency/status filters intersect, reset page, and survive refresh plus back/forward', async () => {
        await scenario('normal'); await open(p, 'page=3');
        await choose(p, '来源', sourceB); await p.waitForURL(url => url.searchParams.get('source') === sourceB);
        assert.equal(new URL(p.url()).searchParams.get('page'), '1');
        await choose(p, '频率', '分钟'); await p.waitForURL(url => url.searchParams.get('frequency') === 'minute');
        assert.equal(await listRows(p).count(), 4);
        await choose(p, '当前状态', '存在限制'); await p.waitForURL(url => url.searchParams.get('status') === 'restricted');
        assert.equal(await listRows(p).count(), 1);
        const query = new URL(p.url()).search;
        await refresh(p);
        assert.equal(new URL(p.url()).search, query); assert.equal(await listRows(p).count(), 1);
        await p.goBack(); assert.equal(new URL(p.url()).searchParams.get('status'), null);
        await p.goForward(); assert.equal(new URL(p.url()).searchParams.get('status'), 'restricted');
        await screenshot(p, 'catalog-filters-390');
      });
      await check('no match differs from an empty directory and offers filter recovery', async () => {
        await p.getByLabel('搜索数据集', { exact: true }).fill('不存在的数据集');
        await p.getByText('没有符合筛选条件的数据集。', { exact: true }).waitFor();
        await screenshot(p, 'catalog-no-match-390');
        assert.equal(await p.getByText('当前目录未登记业务数据集。', { exact: true }).count(), 0);
        await p.getByRole('button', { name: '清除筛选', exact: true }).first().click();
        await p.getByRole('link', { name: title(1), exact: true }).waitFor(); assert.equal(await listRows(p).count(), 20);
      });
      await check('invalid and disappeared pages are corrected in the URL after refresh', async () => {
        await open(p, 'page=999&search=隔离示例&status=all&source=all&frequency=all');
        await p.waitForURL(url => url.searchParams.get('page') === '3');
        await scenario('one'); await refresh(p);
        await p.waitForURL(url => url.searchParams.get('page') === '1');
        assert.equal(new URL(p.url()).searchParams.get('search'), '隔离示例'); assert.equal(await listRows(p).count(), 1);
        for (const page of ['-1', '03', '1.5', '9999999999999999999999']) { await open(p, `page=${page}`); await p.waitForURL(url => url.searchParams.get('page') === '1'); }
      });
      await check('initial failure exposes retry and safe error text without inventing an empty directory', async () => {
        await scenario('error'); await open(p);
        await p.getByRole('alert').filter({ hasText: '首次读取目录失败' }).waitFor();
        assert.deepEqual(await metrics(p), ['待加载', '待加载', '待加载', '待加载']);
        assert.equal(await p.getByText('fixture-proxy-diagnostic').count(), 0); await screenshot(p, 'catalog-error-390');
        await scenario('normal'); await refresh(p, '重试读取目录'); assert.equal(await listRows(p).count(), 20);
      });
      await check('loading keeps the controls, four pending metrics, and a readable status', async () => {
        await scenario('slow'); await p.goto(`${base}/admin/data-assets`);
        await p.getByText('正在读取当前数据目录…', { exact: true }).waitFor();
        assert.equal(await p.getByRole('button', { name: '刷新页面', exact: true }).isDisabled(), true);
        assert.deepEqual(await metrics(p), ['待加载', '待加载', '待加载', '待加载']); await screenshot(p, 'catalog-loading-390'); await ready(p);
      });
      await check('ordinary refresh failure retains the old directory and exact observation time', async () => {
        const before = await p.locator('.qf-catalog .qf-assets-asof').textContent();
        await scenario('error'); await refresh(p);
        assert.equal(await p.locator('.qf-catalog .qf-assets-asof').textContent(), before); assert.equal((await metrics(p))[0], '60');
        await p.getByRole('alert').filter({ hasText: '当前保留' }).waitFor(); await screenshot(p, 'catalog-refresh-error-390');
      });
      await check('partial load displays loaded-scope filtering, pending global metrics, and retry completion', async () => {
        await scenario('partial'); await open(p);
        assert.deepEqual(await metrics(p), ['待加载', '待加载', '待加载', '待加载']);
        assert.match(await p.locator('.qf-catalog-results').textContent(), /已加载范围内筛选.*100 个匹配/);
        await p.getByText(/目录未完整加载：已加载 100/).waitFor(); await screenshot(p, 'catalog-partial-390');
        await p.getByLabel('搜索数据集', { exact: true }).fill('数据集 001');
        await p.getByText('已加载范围内没有符合筛选条件的数据集。', { exact: true }).waitFor();
        await scenario('large'); await refresh(p, '重试完整加载');
        assert.equal((await metrics(p))[0], '121'); await p.getByRole('link', { name: title(1), exact: true }).waitFor();
      });
      await check('mid-refresh failure preserves the complete previous catalog instead of replacing it with 100 rows', async () => {
        await open(p); const before = await p.locator('.qf-catalog .qf-assets-asof').textContent();
        await scenario('partial'); await refresh(p);
        assert.equal((await metrics(p))[0], '121'); assert.equal(await p.locator('.qf-catalog .qf-assets-asof').textContent(), before);
        assert.match(await p.locator('.qf-catalog-results').textContent(), /全部目录.*121 个匹配/);
      });
      for (const name of ['budget', 'changed', 'pagination', 'partial-empty']) {
        await check(`${name}: incomplete metadata cannot advertise whole-directory counts`, async () => {
          await scenario(name); await open(p); assert.deepEqual(await metrics(p), ['待加载', '待加载', '待加载', '待加载']);
          await p.getByRole('button', { name: '重试完整加载', exact: true }).waitFor();
          assert.match(await p.locator('.qf-catalog-results').textContent(), /已加载范围内筛选/);
          if (name === 'budget') { assert.match(await p.locator('.qf-catalog-results').textContent(), /1,000 个匹配/); await screenshot(p, 'catalog-budget-390'); }
          if (name === 'partial-empty') { await p.getByText('当前尚无已加载的数据集。', { exact: true }).waitFor(); assert.equal(await p.getByText('当前目录未登记业务数据集。', { exact: true }).count(), 0); }
        });
      }
      await check('unknown states, frequencies, storage layout and values remain explicit; vanished options keep URL selections', async () => {
        await scenario('unknown'); await open(p);
        assert.match(await listRows(p).first().textContent(), /状态待确认.*暂无更新记录.*未声明/);
        await choose(p, '频率', '未声明（future_frequency）');
        assert.equal(await listRows(p).count(), 20); await screenshot(p, 'catalog-unknown-390');
        await open(p, 'source=retired-source&frequency=retired-frequency&page=2');
        assert.match(await p.getByRole('combobox', { name: '来源', exact: true }).textContent(), /retired-source/);
        assert.equal(new URL(p.url()).searchParams.get('source'), 'retired-source');
        assert.equal(await listRows(p).count(), 0); await p.waitForURL(url => url.searchParams.get('page') === '1');
      });
      await check('technical identifiers expand/copy with keyboard and preserve timestamp precision', async () => {
        await scenario('normal'); await open(p, 'search=数据集 001');
        const summary = listRows(p).first().locator('summary'); await summary.focus(); await summary.press('Enter');
        assert.notEqual(await summary.evaluate(e => getComputedStyle(e).outlineStyle), 'none');
        await listRows(p).first().getByRole('button', { name: /复制 .* 的数据集标识/ }).click();
        await listRows(p).first().getByText('已复制', { exact: true }).waitFor();
        assert.equal(await p.evaluate(() => navigator.clipboard.readText()), 'fixture.data_001');
        assert.match(await listRows(p).first().textContent(), /2026-10-10T06:00:00\.123456789Z/);
        await geometry(p, 'technical-390'); await screenshot(p, 'catalog-technical-390', false);
      });
      await check('rapid repeated filters and refreshes retain the final URL selection and reset pages', async () => {
        await open(p, 'page=3');
        for (let i = 0; i < 3; i++) {
          await p.getByLabel('搜索数据集', { exact: true }).fill('数据集 002');
          await p.getByLabel('搜索数据集', { exact: true }).fill('数据集 001');
          await p.getByLabel('搜索数据集', { exact: true }).fill('隔离示例');
          await choose(p, '来源', sourceB); await choose(p, '频率', '分钟');
          await refresh(p);
          assert.equal(await listRows(p).count(), 4); assert.equal(new URL(p.url()).searchParams.get('page'), '1');
          await p.getByRole('button', { name: '清除筛选', exact: true }).click();
        }
      });
      await check('maintenance leaves directory metadata visible and does not claim query readiness', async () => {
        await scenario('maintenance'); await open(p); assert.equal(await listRows(p).count(), 20);
        await p.getByText(/数据底座处于维护状态/).waitFor(); await screenshot(p, 'catalog-maintenance-390');
      });
      for (const name of ['forbidden', 'forbidden-slow-body', 'partial-forbidden']) {
        await check(`${name}: fresh permission denial immediately clears old React metadata and global counts`, async () => {
          await scenario('normal'); await open(p); await scenario(name);
          const start = Date.now(); await p.getByRole('button', { name: '刷新页面', exact: true }).click();
          await p.getByRole('alert').filter({ hasText: '没有当前数据的读取权限' }).waitFor();
          const elapsedMs = Date.now() - start; assert.equal(await listRows(p).count(), 0);
          assert.deepEqual(await metrics(p), ['待加载', '待加载', '待加载', '待加载']);
          assert.equal(await p.locator('.qf-catalog .qf-assets-asof').count(), 0);
          if (name === 'forbidden-slow-body') { assert.ok(elapsedMs < 1200, 'Do not wait for the 1500ms error body'); denialTimings.push({ scenario: name, elapsedMs, bodyDelayMs: 1500 }); }
          await screenshot(p, `catalog-${name}-390`);
        });
      }
      await check('401 returns to the existing login flow and clears the synthetic session', async () => {
        await scenario('normal'); await open(p); await scenario('unauthorized');
        await p.getByRole('button', { name: '刷新页面', exact: true }).click(); await p.waitForURL('**/login');
        assert.equal(await p.evaluate(() => sessionStorage.getItem('quant-foundry.api-token')), null);
        assert.equal(await root(p).count(), 0);
      });
    } finally { await c.close(); }
    await check('late directory response after SPA route change cannot overwrite the new detail view', async () => {
      await scenario('slow'); const c = await context(browser), p = await c.newPage();
      try {
        await p.goto(`${base}/admin/data-assets`); await p.getByText('正在读取当前数据目录…', { exact: true }).waitFor();
        await scenario('normal');
        await p.evaluate(() => { history.pushState(null, '', '/admin/data-assets/fixture.data_001'); dispatchEvent(new PopStateEvent('popstate')); });
        await p.getByRole('heading', { name: title(1), exact: true }).waitFor(); await p.waitForTimeout(1600);
        assert.equal(await root(p).count(), 0); assert.equal(await p.getByRole('heading', { name: title(1), exact: true }).count(), 1);
      } finally { await c.close(); }
    });
    assert.deepEqual(errors, []); assert.ok(requests.every(req => req.method === 'GET'), 'All exercised API actions are read-only');
    console.log(`PASS: ${results.length} cases; zero page errors; all API methods GET`);
  } finally {
    fs.writeFileSync(path.join(output, 'acceptance.json'), JSON.stringify({ isolated: true, results, errors, geometry: geometryResults, denialTimings,
      requestMethods: [...new Set(requests.map(req => req.method))], paths: [...new Set(requests.map(req => req.path))] }, null, 2) + '\n');
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
