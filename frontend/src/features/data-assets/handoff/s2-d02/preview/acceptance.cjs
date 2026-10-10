/* Replay only the loopback fixture UI. These checks do not grant production access. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.FRONTEND_URL || 'http://127.0.0.1:5178';
const fixture = 'http://127.0.0.1:18765';
assert.ok(['127.0.0.1', 'localhost'].includes(new URL(base).hostname), 'Loopback preview only');
const output = process.env.SCREENSHOT_DIR || path.resolve(__dirname, '../screenshots/cloud');
fs.mkdirSync(output, { recursive: true });
const results = [], pageErrors = [], requests = [], deniedTimings = [];
const title = id => `隔离示例：日频数据 ${id}`;
const detail = id => `${base}/admin/data-assets/fixture.daily_${id}`;
const rows = p => p.getByRole('region', { name: '当前数据预览表格', exact: true });

async function scenario(name) {
  const response = await fetch(`${fixture}/__scenario?name=${encodeURIComponent(name)}`);
  assert.equal(response.status, 200);
  assert.equal((await response.json()).scenario, name);
}
async function check(name, run) {
  await run(); results.push(name); console.log(`PASS: ${name}`);
}
async function screenshot(p, name) {
  await p.screenshot({ path: path.join(output, `${name}.jpg`), type: 'jpeg', quality: 85, animations: 'disabled' });
}
async function context(browser, width = 390, expanded = false) {
  const c = await browser.newContext({ viewport: { width, height: width === 1440 ? 900 : width === 1024 ? 768 : 844 } });
  await c.addInitScript(value => {
    // New pages also initialize an opaque about:blank document before navigation.
    if (!['127.0.0.1', 'localhost'].includes(location.hostname)) return;
    sessionStorage.setItem('quant-foundry.api-token', 's2-d02-isolated-fixture');
    localStorage.setItem('qfo-sidebar', value ? 'expanded' : 'collapsed');
  }, expanded);
  await c.route(url => !['127.0.0.1', 'localhost'].includes(url.hostname), route => route.abort());
  c.on('page', p => {
    p.on('pageerror', e => pageErrors.push(e.message));
    p.on('request', r => {
      const url = new URL(r.url());
      if (url.pathname.startsWith('/api/')) requests.push({ method: r.method(), path: url.pathname });
      if (url.pathname.endsWith('/query') && r.method() === 'POST') {
        const body = r.postDataJSON();
        assert.equal(body.allow_partial, false);
        assert.equal(body.representation, 'fixture_daily_raw');
      }
    });
  });
  return c;
}
async function openDetail(p, id = '01', view = 'overview') {
  await p.goto(`${detail(id)}?view=${view}`);
  await p.getByRole('heading', { name: title(id), exact: true }).waitFor();
}
async function read(p) {
  await p.getByRole('button', { name: '读取当前预览', exact: true }).click();
  await rows(p).waitFor();
}
async function geometry(p) {
  const values = await p.evaluate(() => {
    const main = document.querySelector('.qfo-main');
    return { viewport: innerWidth, document: document.documentElement.scrollWidth,
      main: { width: main.clientWidth, overflow: getComputedStyle(main).overflowY },
      title: getComputedStyle(document.querySelector('h1')).fontSize,
      body: getComputedStyle(document.querySelector('.qf-assets')).fontSize,
      tables: [...document.querySelectorAll('.qf-assets-scroll')].filter(e => !e.closest('[hidden]')).map(e =>
        ({ width: e.clientWidth, content: e.scrollWidth, height: e.clientHeight, contentHeight: e.scrollHeight })) };
  });
  assert.ok(values.document <= values.viewport + 1, 'No document horizontal overflow');
  assert.equal(values.main.overflow, 'auto');
  assert.equal(values.body, '14px');
  for (const table of values.tables) assert.ok(table.contentHeight <= table.height + 1, 'No nested vertical table scroll');
  return values;
}

(async () => {
  const browser = await chromium.launch({ headless: true,
    ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}), args: ['--no-sandbox'] });
  const geometryResults = [];
  try {
    for (const width of [1440, 1024, 390]) {
      await check(`${width}px: list context, three views, keyboard, sidebar and quick jump`, async () => {
        await scenario('normal');
        const c = await context(browser, width, width > 980), p = await c.newPage();
        try {
          await p.goto(`${base}/admin/data-assets?search=日频&status=all&source=local&frequency=daily&page=2`);
          await p.getByRole('link', { name: title('21'), exact: true }).waitFor();
          geometryResults.push({ view: 'catalog', ...await geometry(p) });
          await screenshot(p, `catalog-${width}`);
          await p.locator('.qfo-main').evaluate(e => { e.scrollTop = 230; });
          await p.getByRole('link', { name: title('21'), exact: true }).press('Enter');
          await p.getByRole('heading', { name: title('21'), exact: true }).waitFor();
          const saved = await p.evaluate(() => [...Array(sessionStorage.length)].map((_, i) => sessionStorage.key(i))
            .filter(k => k.startsWith('qf-assets-scroll:')).map(k => Number(sessionStorage.getItem(k)))[0]);
          assert.ok(saved > 0);
          assert.equal(await p.getByRole('link', { name: '数据资产', exact: true }).getAttribute('aria-current'), 'page');
          geometryResults.push({ view: 'overview', ...await geometry(p) });
          await screenshot(p, `overview-${width}`);
          const overview = p.getByRole('tab', { name: '概览', exact: true });
          await overview.focus(); await overview.press('ArrowRight');
          await p.waitForURL(url => url.searchParams.get('view') === 'preview');
          assert.equal(await p.getByRole('tab', { name: '数据预览', exact: true }).evaluate(e => e === document.activeElement), true);
          assert.notEqual(await p.getByRole('tab', { name: '数据预览', exact: true }).evaluate(e => getComputedStyle(e).outlineStyle), 'none');
          await read(p);
          assert.equal(await rows(p).getByRole('cell', { name: '12345678901234567890.123456789', exact: true }).count(), 1);
          await p.getByRole('button', { name: '下一页', exact: true }).click();
          await p.getByText('第 2 页 · 本页 1 行', { exact: true }).waitFor();
          if (await rows(p).evaluate(e => e.scrollWidth > e.clientWidth)) {
            await rows(p).focus(); await rows(p).press('ArrowRight');
            await p.waitForFunction(() => document.querySelector('#qf-assets-panel-preview .qf-assets-scroll').scrollLeft > 0);
            await rows(p).evaluate(e => { e.scrollLeft = 0; });
          }
          geometryResults.push({ view: 'preview', ...await geometry(p) });
          await screenshot(p, `preview-${width}`);
          await p.getByRole('tab', { name: '数据预览', exact: true }).press('End');
          await p.getByRole('heading', { name: '最近更新与能力限制', exact: true }).waitFor();
          geometryResults.push({ view: 'updates', ...await geometry(p) });
          await screenshot(p, `updates-${width}`);
          await p.getByRole('tab', { name: '更新与问题', exact: true }).press('Home');
          await p.getByRole('button', { name: '查看当前数据', exact: true }).click();
          await p.getByText('第 2 页 · 本页 1 行', { exact: true }).waitFor();
          await p.getByRole('link', { name: '← 数据资产', exact: true }).press('Enter');
          await p.getByRole('link', { name: title('21'), exact: true }).waitFor();
          const params = new URL(p.url()).searchParams;
          for (const [key, value] of [['search', '日频'], ['status', 'all'], ['source', 'local'], ['frequency', 'daily'], ['page', '2']]) assert.equal(params.get(key), value);
          await p.waitForFunction(expected => Math.abs(document.querySelector('.qfo-main').scrollTop - expected) < 2, saved);
          if (width > 980) {
            await p.getByRole('button', { name: '收起侧栏', exact: true }).click();
            assert.equal(await p.locator('.qfo-sidebar-collapsed').count(), 1);
            await p.getByRole('button', { name: '展开侧栏', exact: true }).click();
            assert.equal(await p.locator('.qfo-sidebar-collapsed').count(), 0);
          }
          await p.keyboard.press('Control+k');
          await p.getByRole('combobox', { name: '搜索页面或功能', exact: true }).fill('数据资产');
          await p.getByRole('combobox', { name: '搜索页面或功能', exact: true }).press('Enter');
          await p.waitForURL(`${base}/admin/data-assets`);
          assert.equal(await p.getByRole('dialog').count(), 0);
        } finally { await c.close(); }
      });
    }
    await check('narrow layout retains an expanded desktop preference without consuming content width', async () => {
      await scenario('normal'); const c = await context(browser, 1440, true), p = await c.newPage();
      try {
        await openDetail(p); await p.setViewportSize({ width: 390, height: 844 });
        await p.waitForFunction(() => document.querySelector('.qfo-sidebar').getBoundingClientRect().width <= 75);
        assert.equal(await p.evaluate(() => localStorage.getItem('qfo-sidebar')), 'expanded');
        assert.equal(await p.locator('.qfo-nav-item span').first().evaluate(e => getComputedStyle(e).display), 'none');
        await geometry(p); await screenshot(p, 'remembered-sidebar-390');
        await p.setViewportSize({ width: 1440, height: 900 });
        await p.waitForFunction(() => document.querySelector('.qfo-sidebar').getBoundingClientRect().width > 200);
        assert.equal(await p.getByRole('button', { name: '收起侧栏', exact: true }).count(), 1);
      } finally { await c.close(); }
    });
    const c = await context(browser), p = await c.newPage();
    try {
      await check('browser back and forward keep the same object preview session', async () => {
        await scenario('normal'); await openDetail(p, '01', 'preview'); await read(p);
        await p.getByRole('tab', { name: '更新与问题', exact: true }).click();
        await p.goBack(); await p.waitForURL(url => url.searchParams.get('view') === 'preview');
        assert.equal(await rows(p).count(), 1);
        await p.goForward(); await p.waitForURL(url => url.searchParams.get('view') === 'updates');
      });
      await check('initial catalog failure and later permission revocation never become empty success', async () => {
        await scenario('error'); await p.goto(`${base}/admin/data-assets`);
        await p.getByRole('alert').waitFor();
        assert.equal(await p.getByText('当前目录未登记业务数据集。', { exact: true }).count(), 0);
        await screenshot(p, 'catalog-error-390');
        await scenario('normal'); await p.goto(`${base}/admin/data-assets`);
        await p.getByRole('link', { name: title('01'), exact: true }).waitFor();
        await scenario('forbidden'); await p.getByRole('button', { name: '刷新页面', exact: true }).click();
        await p.getByRole('alert').filter({ hasText: '读取权限' }).waitFor();
        assert.equal(await p.getByRole('region', { name: '数据目录表格', exact: true }).count(), 0);
        assert.equal(await p.locator('.qf-assets-metrics').count(), 0);
      });
      await check('loading and refresh retain structure and local failures retain old observations', async () => {
        await scenario('slow'); await p.goto(`${base}/admin/data-assets`);
        await p.getByText('正在读取当前数据目录…', { exact: true }).waitFor();
        await screenshot(p, 'catalog-loading-390');
        await p.getByRole('link', { name: title('01'), exact: true }).waitFor();
        const tableBox = await p.getByRole('region', { name: '数据目录表格', exact: true }).boundingBox();
        await p.getByRole('button', { name: '刷新页面', exact: true }).click();
        await p.getByRole('button', { name: '刷新中…', exact: true }).waitFor();
        assert.equal(await p.getByRole('region', { name: '数据目录表格', exact: true }).count(), 1);
        assert.equal((await p.getByRole('region', { name: '数据目录表格', exact: true }).boundingBox()).y, tableBox.y);
        await p.getByRole('button', { name: '刷新页面', exact: true }).waitFor();
        await scenario('error'); await p.getByRole('button', { name: '刷新页面', exact: true }).click();
        await p.getByRole('alert').filter({ hasText: '当前保留' }).waitFor();
        await screenshot(p, 'catalog-refresh-error-390');
        assert.equal(await p.getByRole('link', { name: title('01'), exact: true }).count(), 1);
      });
      await check('true catalog empty, incomplete catalog and unknown facts remain distinct', async () => {
        await scenario('empty'); await p.goto(`${base}/admin/data-assets`);
        await p.getByText('当前目录未登记业务数据集。', { exact: true }).waitFor();
        await screenshot(p, 'catalog-empty-390');
        await scenario('catalog-incomplete'); await p.goto(`${base}/admin/data-assets`);
        await p.getByText('目录未完整加载，筛选仅涵盖已加载的数据集，完整状态计数未声明。', { exact: true }).waitFor();
        assert.deepEqual(await p.locator('.qf-assets-metrics strong').allTextContents(), ['48', '未声明', '未声明', '未声明']);
        await scenario('unknown'); await openDetail(p);
        assert.ok((await p.locator('.qf-assets-metrics strong').allTextContents()).includes('状态待确认'));
        assert.equal(await p.getByText('当前数据集已登记，尚无正式记录。', { exact: true }).count(), 0);
      });
      await check('detail loading, missing dataset and true empty preserve usable shell', async () => {
        await scenario('slow'); await p.goto(detail('01'));
        await p.getByText('正在读取数据集详情…', { exact: true }).first().waitFor();
        await screenshot(p, 'detail-loading-390');
        await p.getByRole('heading', { name: title('01'), exact: true }).waitFor();
        await scenario('normal'); await p.goto(`${base}/admin/data-assets/fixture.missing`);
        await p.getByRole('alert').waitFor();
        assert.equal(await p.getByRole('button', { name: '查看当前数据', exact: true }).isDisabled(), true);
        await screenshot(p, 'detail-missing-390');
        await openDetail(p, '04');
        await p.getByText('当前数据集已登记，尚无正式记录。', { exact: true }).waitFor();
        await openDetail(p, '03');
        await p.getByText('本地来源尚未检查，不能将未检查视为空。', { exact: true }).waitFor();
      });
      await check('detail refresh failure keeps summary; issue failure stays local', async () => {
        await scenario('normal'); await openDetail(p, '02', 'updates');
        await p.getByRole('heading', { name: '当前问题与限制', exact: true }).waitFor();
        await scenario('issues-error'); await p.getByRole('button', { name: '刷新页面', exact: true }).click();
        await p.getByRole('alert').filter({ hasText: '问题详情读取失败' }).waitFor();
        assert.equal(await p.getByRole('heading', { name: title('02'), exact: true }).count(), 1);
        await screenshot(p, 'issues-error-390');
        await scenario('error'); await p.getByRole('button', { name: '刷新页面', exact: true }).click();
        await p.getByRole('alert').filter({ hasText: '当前保留上次读取的数据集摘要' }).waitFor();
      });
      await check('preview exact text, empty and ordinary failure retain truthful page state', async () => {
        await scenario('normal'); await openDetail(p, '01', 'preview'); await read(p);
        await scenario('query-error'); await p.getByRole('button', { name: '下一页', exact: true }).click();
        await p.getByRole('alert').filter({ hasText: '当前保留' }).waitFor();
        assert.equal(await p.getByText('第 1 页 · 本页 1 行', { exact: true }).count(), 1);
        await scenario('preview-empty'); await p.getByRole('button', { name: '读取当前预览', exact: true }).click();
        await p.getByText(/所选范围没有匹配记录/).waitFor();
        assert.equal(await rows(p).count(), 0);
        assert.equal(await p.getByRole('button', { name: '下一页', exact: true }).isDisabled(), true);
        await screenshot(p, 'preview-empty-390');
      });
      for (const name of ['changed', 'invalid-cursor', 'query-restricted', 'query-generation']) {
        await check(`${name}: old rows and cursor stack are withdrawn`, async () => {
          await scenario('normal'); await openDetail(p, '01', 'preview'); await read(p);
          await scenario(name); await p.getByRole('button', { name: '下一页', exact: true }).click();
          await p.getByRole('alert').waitFor(); assert.equal(await rows(p).count(), 0);
          assert.equal(await p.getByRole('button', { name: '下一页', exact: true }).count(), 0);
          if (name === 'changed') await screenshot(p, 'preview-changed-390');
        });
      }
      for (const name of ['generation', 'maintenance', 'restricted']) {
        await check(`${name}: metadata refresh invalidates prior preview`, async () => {
          await scenario('normal'); await openDetail(p, '01', 'preview'); await read(p);
          await scenario(name); await p.getByRole('button', { name: '刷新页面', exact: true }).click();
          await p.getByRole('alert').filter({ hasText: '当前数据已改变' }).waitFor();
          assert.equal(await rows(p).count(), 0);
          if (name === 'maintenance') {
            assert.equal(await p.getByRole('button', { name: '读取当前预览', exact: true }).isDisabled(), true);
            await screenshot(p, 'preview-maintenance-390');
          }
        });
      }
      for (const name of ['generation', 'maintenance']) {
        await check(`${name}: refresh cancels the first pending preview before any page exists`, async () => {
          await scenario('normal'); await openDetail(p, '01', 'preview');
          await scenario('query-slow');
          const pending = p.waitForRequest(r => new URL(r.url()).pathname.endsWith('/query'));
          await p.getByRole('button', { name: '读取当前预览', exact: true }).click(); await pending;
          await scenario(name); await p.getByRole('button', { name: '刷新页面', exact: true }).click();
          await p.getByRole('alert').filter({ hasText: '当前数据已改变' }).waitFor();
          await p.waitForTimeout(1650); assert.equal(await rows(p).count(), 0);
        });
      }
      await check('condition changes cancel delayed preview and keep cursors out of navigation storage', async () => {
        await scenario('normal'); await openDetail(p, '01', 'preview');
        await p.getByText('调整预览条件（技术信息）', { exact: true }).click();
        await scenario('query-slow');
        const pending = p.waitForRequest(r => new URL(r.url()).pathname.endsWith('/query'));
        await p.getByRole('button', { name: '读取当前预览', exact: true }).click(); await pending;
        await p.getByLabel('标的标识', { exact: true }).fill('another-isolated-object');
        await p.waitForTimeout(1650); assert.equal(await rows(p).count(), 0);
        const stored = await p.evaluate(() => [...Array(sessionStorage.length)].map((_, i) => sessionStorage.key(i))
          .filter(k => k.startsWith('qf-assets')).map(k => sessionStorage.getItem(k)).join('|'));
        assert.equal(stored.includes('fixture-page-two'), false);
        assert.equal(p.url().includes('cursor='), false);
      });
      await check('object change unmounts prior sessions and rejects late preview', async () => {
        await scenario('normal'); await openDetail(p, '01', 'preview');
        await scenario('query-slow');
        const pending = p.waitForRequest(r => new URL(r.url()).pathname.endsWith('/query'));
        await p.getByRole('button', { name: '读取当前预览', exact: true }).click(); await pending;
        await scenario('normal'); await p.getByRole('link', { name: '← 数据资产', exact: true }).click();
        await p.getByRole('link', { name: title('02'), exact: true }).click();
        await p.getByRole('button', { name: '查看当前数据', exact: true }).click();
        await p.waitForTimeout(1650); assert.equal(await rows(p).count(), 0);
        await read(p); await p.getByText('第 1 页 · 本页 1 行', { exact: true }).waitFor();
      });
      for (const name of ['query-forbidden', 'query-forbidden-slow', 'issues-forbidden']) {
        await check(`${name}: permission revocation clears the whole detail`, async () => {
          await scenario('normal'); await openDetail(p, name === 'issues-forbidden' ? '02' : '01', 'preview'); await read(p);
          await scenario(name); const started = Date.now();
          await p.getByRole('button', { name: name === 'issues-forbidden' ? '刷新页面' : '读取当前预览', exact: true }).click();
          await p.getByRole('heading', { name: '数据集详情', exact: true }).waitFor({ timeout: 1200 });
          assert.equal(await rows(p).count(), 0);
          await p.getByRole('alert').filter({ hasText: '读取权限' }).waitFor();
          if (name === 'query-forbidden-slow') deniedTimings.push({ name, elapsedMs: Date.now() - started });
          await geometry(p);
          if (name === 'query-forbidden') await screenshot(p, 'detail-forbidden-390');
        });
      }
      await check('401 ignores stalled body and keeps the existing login flow', async () => {
        await scenario('normal'); await openDetail(p, '01', 'preview'); await read(p);
        await scenario('query-unauthorized-slow'); const started = Date.now();
        await p.getByRole('button', { name: '读取当前预览', exact: true }).click();
        await p.waitForURL(`${base}/login`, { timeout: 1200 });
        deniedTimings.push({ name: 'query-unauthorized-slow', elapsedMs: Date.now() - started });
        assert.equal(await p.evaluate(() => sessionStorage.getItem('quant-foundry.api-token')), null);
      });
      await check('explicit logout cancels pending preview and removes authentication', async () => {
        await scenario('normal'); await openDetail(p, '01', 'preview');
        await scenario('query-slow');
        const pending = p.waitForRequest(r => new URL(r.url()).pathname.endsWith('/query'));
        await p.getByRole('button', { name: '读取当前预览', exact: true }).click(); await pending;
        await p.getByRole('button', { name: '退出登录', exact: true }).click();
        await p.waitForURL(`${base}/login`);
        await p.waitForTimeout(1650);
        assert.equal(await p.evaluate(() => sessionStorage.getItem('quant-foundry.api-token')), null);
        assert.equal(await p.locator('.qf-assets').count(), 0);
      });
    } finally { await c.close(); }
    assert.deepEqual(pageErrors, []);
    assert.ok(requests.every(r => r.method === 'GET' || (r.method === 'POST' && r.path === '/api/admin/data-store/query')));
    fs.writeFileSync(path.join(output, 'acceptance.json'), JSON.stringify({ isolated: true, productionIntegration: false,
      casesPassed: results.length, results, geometry: geometryResults, deniedTimings, pageErrors,
      requestMethods: [...new Set(requests.map(r => `${r.method} ${r.path}`))] }, null, 2) + '\n');
    console.log(`PASS: ${results.length} isolated browser cases; no page errors or write operations`);
  } catch (error) {
    console.error(error); process.exitCode = 1;
  } finally { await scenario('normal'); await browser.close(); }
})();
