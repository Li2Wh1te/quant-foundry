/* Browser acceptance is confined to the synthetic loopback D04 fixture.
 * No fixture observation is evidence of real API or production availability. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.FRONTEND_URL || 'http://127.0.0.1:5184';
const fixture = 'http://127.0.0.1:18766';
assert.ok(['127.0.0.1', 'localhost'].includes(new URL(base).hostname));
const output = process.env.SCREENSHOT_DIR || path.resolve(__dirname, '../screenshots');
fs.mkdirSync(output, { recursive: true });
const results = [], pageErrors = [], requests = [], geometryResults = [];
const names = { daily: '隔离示例：日线（来源报告值／未锚定）', nav: '隔离示例：净值（来源报告值）', report: '隔离示例：完整报告（来源报告值）' };
const detail = (id, view = 'overview') => `${base}/admin/data-assets/fixture.${id}?search=隔离&page=1&view=${view}`;
async function scenario(name) {
  const response = await fetch(`${fixture}/__scenario?name=${name}`);
  assert.equal(response.status, 200); assert.equal((await response.json()).scenario, name);
}
async function check(name, run) { await run(); results.push(name); console.log(`PASS: ${name}`); }
async function shot(page, name) { await page.screenshot({ path: path.join(output, `${name}.jpg`), type: 'jpeg', quality: 88, animations: 'disabled' }); }
async function open(page, id = 'daily') {
  await page.goto(detail(id));
  await page.getByRole('heading', { name: names[id], exact: true }).waitFor();
  await page.getByRole('heading', { name: '能提供什么', exact: true }).waitFor();
}
async function context(browser, width = 390) {
  const c = await browser.newContext({ viewport: { width, height: width === 1440 ? 900 : width === 1024 ? 768 : 844 }, permissions: ['clipboard-read', 'clipboard-write'] });
  await c.addInitScript(() => {
    if (!['127.0.0.1', 'localhost'].includes(location.hostname)) return;
    sessionStorage.setItem('quant-foundry.api-token', 's2-d04-isolated-fixture');
    localStorage.setItem('qfo-sidebar', innerWidth > 980 ? 'expanded' : 'collapsed');
    // Label every evidence image, including loading/denial states whose normal
    // application UI intentionally no longer contains a fixture dataset name.
    addEventListener('DOMContentLoaded', () => {
      const label = document.createElement('div');
      label.textContent = '隔离夹具 · 非真实接口验收'; label.setAttribute('aria-hidden', 'true');
      label.style.cssText = 'position:fixed;right:8px;bottom:8px;z-index:9999;pointer-events:none;padding:4px 8px;border:1px solid #d3b9a1;border-radius:6px;background:#fffaf2;color:#77511f;font:14px/22px sans-serif';
      document.body.append(label);
    });
  });
  await c.route(url => !['127.0.0.1', 'localhost'].includes(url.hostname), route => route.abort());
  c.on('page', p => {
    p.on('pageerror', problem => pageErrors.push(problem.message));
    p.on('request', request => {
      if (new URL(request.url()).pathname.startsWith('/api/admin/data-store/')) requests.push({ method: request.method(), path: new URL(request.url()).pathname });
    });
  });
  return c;
}
async function geometry(page, kind) {
  const measured = await page.evaluate(() => {
    const main = document.querySelector('.qfo-main');
    const visible = selector => [...document.querySelectorAll(selector)].filter(e => !e.closest('[hidden]'));
    return { viewport: innerWidth, document: document.documentElement.scrollWidth,
      mainOverflow: getComputedStyle(main).overflowY,
      copy: visible('.qf-dataset-copy').filter(e => e.closest('details').open).map(e => ({ width: e.getBoundingClientRect().width, right: e.getBoundingClientRect().right })),
      tables: visible('.qf-dataset-fields .qf-assets-scroll').map(e => ({ width: e.clientWidth, content: e.scrollWidth, height: e.clientHeight, contentHeight: e.scrollHeight })),
      prose: visible('.qf-dataset-details p').map(e => getComputedStyle(e).fontSize) };
  });
  assert.ok(measured.document <= measured.viewport + 1, 'No document horizontal overflow');
  assert.equal(measured.mainOverflow, 'auto');
  for (const table of measured.tables) assert.ok(table.contentHeight <= table.height + 1, 'Table has no nested vertical scrolling');
  for (const size of measured.prose) assert.equal(size, '14px');
  for (const button of measured.copy) assert.ok(button.width > 0 && button.right <= measured.viewport + 1);
  geometryResults.push({ kind, ...measured });
}

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium', args: ['--no-sandbox'] });
  try {
    for (const width of [1440, 1024, 390]) {
      const c = await context(browser, width), p = await c.newPage();
      try {
        await check(`${width}px: daily, NAV and report overview keep qualifiers and unknown business cutoff`, async () => {
          await scenario('normal');
          for (const id of ['daily', 'nav', 'report']) {
            await open(p, id);
            assert.equal(await p.locator('.qf-dataset-technical details').getAttribute('open'), null);
            assert.equal(await p.getByRole('button', { name: '查看当前数据', exact: true }).isEnabled(), true);
            assert.ok((await p.locator('.qf-dataset-metrics').innerText()).includes('业务数据截至日\n未声明'));
            assert.ok((await p.locator('.qf-dataset-metrics').innerText()).includes('物理存储行\n1,234,567'));
            assert.equal(await p.getByText('fixture-schema-v2', { exact: true }).isVisible(), false);
            await geometry(p, `${id}-${width}`); await shot(p, `${id}-${width}`);
          }
        });
        await check(`${width}px: field disclosure, local horizontal scrolling and keyboard focus`, async () => {
          await open(p);
          const region = p.getByRole('region', { name: '数据集字段表格', exact: true });
          await region.scrollIntoViewIfNeeded();
          assert.ok((await region.innerText()).includes('未提供业务说明'));
          assert.ok((await region.innerText()).includes('不支持直接计算'));
          await region.focus(); await region.press('ArrowRight');
          if (await region.evaluate(e => e.scrollWidth > e.clientWidth)) await p.waitForFunction(() => document.querySelector('.qf-dataset-fields .qf-assets-scroll').scrollLeft > 0);
          assert.notEqual(await region.evaluate(e => getComputedStyle(e).outlineStyle), 'none');
          await region.evaluate(e => { e.scrollLeft = 0; });
          const disclosure = region.locator('summary').first();
          await disclosure.focus(); await disclosure.press('Enter');
          assert.equal(await region.getByText('current_identity_or_confirmation', { exact: true }).isVisible(), true);
          await shot(p, `fields-${width}`);
          await region.evaluate(e => { e.scrollLeft = e.scrollWidth; });
          await shot(p, `field-types-${width}`);
          await region.evaluate(e => { e.scrollLeft = 0; });
        });
        await check(`${width}px: technical identifiers expand and preserve a missing partition endpoint`, async () => {
          const summary = p.getByText('展开定位与存储信息', { exact: true });
          await summary.focus(); await summary.press('Enter');
          await p.locator('.qf-dataset-technical').scrollIntoViewIfNeeded();
          const text = await p.locator('.qf-dataset-technical').innerText();
          assert.ok(text.includes('源表示键')); assert.ok(text.includes('fixture_daily_reported'));
          assert.ok(text.includes('终点：未声明')); assert.ok(text.includes('分区列表已截断'));
          assert.ok(text.includes('adjustment_anchor_and_formula_unverified'));
          await geometry(p, `technical-${width}`); await shot(p, `technical-${width}`);
          await p.getByText('存储分区范围', { exact: true }).scrollIntoViewIfNeeded();
          await shot(p, `technical-range-${width}`);
        });
      } finally { await c.close(); }
    }
    const c = await context(browser), p = await c.newPage();
    try {
      await check('field searches, no matches, paging and repeated tab switches never request business data', async () => {
        await scenario('normal'); await open(p, 'report');
        const input = p.getByRole('searchbox', { name: '搜索字段元数据', exact: true });
        const region = p.getByRole('region', { name: '数据集字段表格', exact: true });
        await p.getByRole('tab', { name: '更新与问题', exact: true }).click();
        await p.getByRole('heading', { name: '当前问题与限制', exact: true }).waitFor();
        await p.getByRole('tab', { name: '概览', exact: true }).click();
        const before = requests.length;
        await input.fill('f46_'); assert.equal(await region.locator('tbody tr').count(), 1);
        await input.fill('missing-field'); await p.getByText('没有匹配的字段元数据。', { exact: true }).waitFor();
        await shot(p, 'fields-no-match-390');
        await p.getByRole('button', { name: '清除筛选', exact: true }).click();
        assert.equal(await region.locator('tbody tr').count(), 20);
        await p.getByRole('button', { name: '下一页字段', exact: true }).click();
        await p.getByRole('button', { name: '下一页字段', exact: true }).click();
        await p.getByText('字段 41–47 / 47', { exact: true }).waitFor();
        assert.equal(await region.locator('tbody tr').count(), 7);
        assert.equal(await p.getByRole('button', { name: '下一页字段', exact: true }).isDisabled(), true);
        await input.fill('f12_');
        for (let i = 0; i < 12; i++) {
          await p.getByRole('tab', { name: '数据预览', exact: true }).click();
          await p.getByRole('tab', { name: '概览', exact: true }).click();
        }
        assert.equal(await input.inputValue(), 'f12_');
        assert.equal(requests.length, before, 'Metadata interactions produce no network reads');
        await shot(p, 'fields-search-390');
        await p.locator('.qfo-main').evaluate(e => { e.scrollTop = 0; });
        assert.equal(await p.getByRole('heading', { name: names.report, exact: true }).isVisible(), true);
        assert.equal(await p.getByRole('link', { name: '← 数据资产', exact: true }).isVisible(), true);
        await p.getByRole('button', { name: '查看当前数据', exact: true }).click();
        await p.waitForURL(url => url.searchParams.get('view') === 'preview');
        assert.equal(new URL(p.url()).searchParams.get('search'), '隔离');
        await p.goBack(); await p.waitForURL(url => url.searchParams.get('view') === 'overview');
        assert.equal(await input.inputValue(), 'f12_');
      });
      await check('same-object refresh clamps a reduced field list without a false empty page', async () => {
        await scenario('normal'); await open(p, 'report');
        await p.getByRole('button', { name: '下一页字段', exact: true }).click();
        await p.getByRole('button', { name: '下一页字段', exact: true }).click();
        await scenario('fields-reduced'); await p.getByRole('button', { name: '刷新页面', exact: true }).click();
        await p.getByText('3 / 3 个字段元数据', { exact: true }).waitFor();
        assert.equal(await p.locator('.qf-dataset-fields tbody tr').count(), 3);
        assert.equal(await p.getByText('没有匹配的字段元数据。', { exact: true }).count(), 0);
      });
      await check('copy success, repeated operations and clipboard failure stay local to technical information', async () => {
        await scenario('normal'); await open(p);
        await p.getByText('展开定位与存储信息', { exact: true }).click();
        for (let i = 0; i < 4; i++) await p.getByRole('button', { name: '复制数据集标识', exact: true }).click();
        await p.getByText('已复制数据集标识。', { exact: true }).waitFor();
        assert.equal(await p.evaluate(() => navigator.clipboard.readText()), 'fixture.daily');
        await p.evaluate(() => { navigator.clipboard.writeText = async () => { throw new Error('synthetic clipboard denial'); }; });
        await p.getByRole('button', { name: '复制Schema 标识', exact: true }).click();
        await p.getByRole('alert').filter({ hasText: '复制失败' }).waitFor();
        assert.equal(await p.locator('.qf-dataset-fields tbody tr').count(), 4);
        assert.equal(await p.getByRole('heading', { name: '能提供什么', exact: true }).count(), 1);
        await p.getByRole('alert').filter({ hasText: '复制失败' }).scrollIntoViewIfNeeded();
        await shot(p, 'copy-failure-390');
      });
      for (const [name, label, explanation] of [
        ['restricted', '存在限制', '也不表示所有范围均不可读取'], ['empty', '当前为空', '当前物理存储为空'],
        ['not-checked', '尚未检查', '不能将尚未检查视为空'], ['maintenance', '暂不可读取／处理中', '请稍后刷新页面'],
        ['rebuild-required', '需要重建', '页面不会执行重建'], ['unknown', '状态待确认', '当前状态尚未确认']
      ]) await check(`${name}: descriptor explanation remains separate from actual read permission`, async () => {
        await scenario(name); await open(p);
        const boundaries = await p.locator('.qf-dataset-boundaries').innerText();
        assert.ok(boundaries.includes(label)); assert.ok(boundaries.includes(explanation));
        assert.equal(await p.getByRole('button', { name: '查看当前数据', exact: true }).isEnabled(), true);
        await shot(p, `${name}-390`);
      });
      await check('absent fields, limits and preview keys remain undeclared rather than empty data', async () => {
        for (const name of ['fields-empty', 'limits-empty', 'no-preview']) {
          await scenario(name); await open(p);
          assert.equal(await p.getByRole('heading', { name: '能提供什么', exact: true }).count(), 1);
          if (name === 'fields-empty') await p.getByText('接口未声明字段说明；不能据此判断数据集为空。', { exact: true }).waitFor();
          if (name === 'limits-empty') await p.getByText('服务端未提供具体限制说明；这不代表没有限制。', { exact: true }).waitFor();
          if (name === 'no-preview') {
            await p.getByText('展开定位与存储信息', { exact: true }).click();
            assert.ok((await p.locator('.qf-dataset-technical').innerText()).includes('源表示键\n未声明'));
          }
          await shot(p, `${name}-390`);
        }
      });
      await check('loading and dataset changes withdraw A immediately, cancel delayed requests and reject late responses', async () => {
        await scenario('slow'); await p.goto(detail('daily'));
        await p.getByText('正在读取数据集详情…', { exact: true }).first().waitFor();
        assert.equal(await p.locator('.qf-dataset-details').count(), 0);
        await shot(p, 'loading-390');
        await p.getByRole('heading', { name: names.daily, exact: true }).waitFor();
        await p.getByRole('searchbox', { name: '搜索字段元数据', exact: true }).fill('f0_close');
        const refresh = p.waitForRequest(r => r.url().includes('/datasets/fixture.daily'));
        await p.getByRole('button', { name: '刷新页面', exact: true }).click(); await refresh;
        await p.getByRole('link', { name: '← 数据资产', exact: true }).click();
        await p.getByRole('link', { name: names.nav, exact: true }).click();
        await p.getByText('正在读取数据集详情…', { exact: true }).first().waitFor();
        assert.equal(await p.locator('.qf-dataset-details').count(), 0);
        assert.equal(await p.getByRole('heading', { name: names.daily, exact: true }).count(), 0);
        await shot(p, 'switch-loading-390');
        await p.getByRole('link', { name: '← 数据资产', exact: true }).click();
        await scenario('normal'); await p.getByRole('link', { name: names.report, exact: true }).click();
        await p.getByRole('heading', { name: names.report, exact: true }).waitFor();
        await p.waitForTimeout(1600);
        assert.equal(await p.getByRole('heading', { name: names.report, exact: true }).count(), 1);
        assert.equal(await p.getByRole('searchbox', { name: '搜索字段元数据', exact: true }).inputValue(), '');
        const observed = await (await fetch(`${fixture}/__requests`)).json();
        assert.ok(observed.requests.some(r => r.scenario === 'slow' && r.aborted), 'An old object request was cancelled');
      });
      await check('ordinary issues failure and detail refresh failure preserve already acquired explanations', async () => {
        await scenario('issues-error'); await open(p);
        await p.getByRole('tab', { name: '更新与问题', exact: true }).click();
        await p.getByRole('alert').filter({ hasText: '问题详情读取失败' }).waitFor();
        await p.getByRole('tab', { name: '概览', exact: true }).click();
        assert.equal(await p.locator('.qf-dataset-fields tbody tr').count(), 4);
        await scenario('refresh-error'); await p.getByRole('button', { name: '刷新页面', exact: true }).click();
        await p.getByRole('alert').filter({ hasText: '当前保留上次读取的数据集摘要' }).waitFor();
        assert.equal(await p.getByRole('heading', { name: '能提供什么', exact: true }).count(), 1);
        await shot(p, 'refresh-failure-390');
      });
      for (const name of ['missing', 'malformed']) await check(`${name}: failed metadata never becomes an empty or valid descriptor`, async () => {
        await scenario(name); await p.goto(detail('daily')); await p.getByRole('alert').waitFor();
        assert.equal(await p.locator('.qf-dataset-details').count(), 0);
        assert.equal(await p.locator('.qf-assets-state.state-empty').count(), 0);
        await shot(p, `${name}-390`);
      });
      await check('403 withdraws all descriptor metadata and 401 follows the existing login flow', async () => {
        await scenario('normal'); await open(p);
        await scenario('forbidden'); await p.getByRole('button', { name: '刷新页面', exact: true }).click();
        await p.getByRole('alert').filter({ hasText: '读取权限' }).waitFor();
        assert.equal(await p.locator('.qf-dataset-details').count(), 0); await shot(p, 'forbidden-390');
        await scenario('unauthorized'); await p.goto(detail('daily')); await p.waitForURL(`${base}/login`);
      });
    } finally { await c.close(); }
    assert.equal(pageErrors.length, 0, `Page errors: ${pageErrors.join('; ')}`);
    assert.ok(requests.length > 0); assert.ok(requests.every(r => r.method === 'GET'), 'D04 does not submit business queries or mutations');
    fs.writeFileSync(path.join(output, 'acceptance.json'), JSON.stringify({ isolated: true, passed: results.length,
      cases: results, pageErrors, geometry: geometryResults, requests, realApiVerified: false, productionOperations: false }, null, 2));
    console.log(`D04 acceptance: ${results.length} passed; ${pageErrors.length} page errors; GET only.`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
