/* D06 real browser interactions against loopback synthetic DTOs only. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.FRONTEND_URL || 'http://127.0.0.1:5186';
const fixture = 'http://127.0.0.1:18768';
assert.ok(['127.0.0.1', 'localhost'].includes(new URL(base).hostname));
const output = process.env.SCREENSHOT_DIR || path.resolve(__dirname, '../screenshots');
fs.mkdirSync(output, { recursive: true });
const results = [], errors = [], requests = [], geometryResults = [];
const detail = (id = 'daily', view = 'updates') => `${base}/admin/data-assets/fixture.${id}?search=隔离&page=2&view=${view}`;
const panel = p => p.locator('.qf-updates');
async function scenario(name) {
  const r = await fetch(`${fixture}/__scenario?name=${name}`); assert.equal(r.status, 200); assert.equal((await r.json()).scenario, name);
}
async function check(name, action) { await action(); results.push(name); console.log(`PASS: ${name}`); }
async function ready(p) {
  await p.getByRole('button', { name: '重新读取问题', exact: true }).waitFor();
  await p.waitForFunction(() => { const button = document.querySelector('.qf-updates-actions button'); return button && !button.disabled; });
}
async function open(p, name = 'normal', id = 'daily') { await scenario(name); await p.goto(detail(id)); await ready(p); }
async function refreshIssues(p) { await p.getByRole('button', { name: '重新读取问题', exact: true }).click(); await ready(p); }
async function shot(p, name) { await p.screenshot({ path: path.join(output, `${name}.jpg`), type: 'jpeg', quality: 88, animations: 'disabled' }); }
async function position(p, selector) {
  await p.locator(selector).first().evaluate(e => { const main = document.querySelector('.qfo-main'); main.scrollTop += e.getBoundingClientRect().top - main.getBoundingClientRect().top - 18; });
}
async function geometry(p, name) {
  const value = await p.evaluate(() => ({ viewport: innerWidth, document: document.documentElement.scrollWidth,
    mainOverflow: getComputedStyle(document.querySelector('.qfo-main')).overflowY,
    fonts: [...document.querySelectorAll('.qf-updates p, .qf-updates dt, .qf-updates summary')].map(e => getComputedStyle(e).fontSize),
    buttons: [...document.querySelectorAll('.qf-updates button')].map(e => ({ left: e.getBoundingClientRect().left, right: e.getBoundingClientRect().right, width: e.getBoundingClientRect().width })),
    compactCounts: [...document.querySelectorAll('.qf-updates-counts dd')].filter(e => /^\d{1,3}$/.test(e.textContent)).map(e => ({ height: e.getBoundingClientRect().height, line: parseFloat(getComputedStyle(e).lineHeight) })),
    nested: [...document.querySelectorAll('.qf-updates *')].filter(e => ['scroll','auto'].includes(getComputedStyle(e).overflowY)).length
  }));
  assert.ok(value.document <= value.viewport + 1); assert.equal(value.mainOverflow, 'auto'); assert.equal(value.nested, 0);
  assert.ok(value.fonts.every(size => size === '14px'));
  assert.ok(value.buttons.every(button => button.width > 0 && button.left >= 0 && button.right <= value.viewport + 1));
  assert.ok(value.compactCounts.every(count => count.height <= count.line + 1), 'Small counts stay on one line');
  geometryResults.push({ name, ...value, fonts: [...new Set(value.fonts)] });
}
async function context(browser, width = 390) {
  const c = await browser.newContext({ viewport: { width, height: width === 1440 ? 900 : width === 1024 ? 768 : 844 } });
  await c.addInitScript(() => {
    if (!['127.0.0.1', 'localhost'].includes(location.hostname)) return;
    sessionStorage.setItem('quant-foundry.api-token', 's2-d06-isolated-fixture');
    localStorage.setItem('qfo-sidebar', innerWidth > 980 ? 'expanded' : 'collapsed');
    addEventListener('DOMContentLoaded', () => {
      const label = document.createElement('div'); label.textContent = '隔离夹具 · 非真实接口验收'; label.setAttribute('aria-hidden','true');
      label.style.cssText = 'position:fixed;right:8px;bottom:8px;z-index:9999;pointer-events:none;padding:4px 8px;border:1px solid #d3b9a1;border-radius:6px;background:#fffaf2;color:#77511f;font:14px/22px sans-serif'; document.body.append(label);
    });
  });
  await c.route(url => !['127.0.0.1', 'localhost'].includes(url.hostname), route => route.abort());
  c.on('page', p => {
    p.on('pageerror', e => errors.push(e.message));
    p.on('request', r => { const u = new URL(r.url()); if (u.pathname.startsWith('/api/')) requests.push({ method: r.method(), path: u.pathname, query: u.search }); });
  });
  return c;
}
(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium', args: ['--no-sandbox'] });
  try {
    for (const width of [1440, 1024, 390]) {
      const c = await context(browser, width), p = await c.newPage();
      try {
        await check(`${width}px: readable current data and failed update remain distinct`, async () => {
          await open(p);
          const text = await panel(p).innerText();
          assert.ok(text.includes('当前可用（按服务端声明）')); assert.ok(text.includes('本次更新失败'));
          assert.ok(text.includes('业务日期覆盖\n未声明')); assert.ok(text.includes('业务数据截至日\n未声明'));
          assert.ok(text.includes('未获合格确认')); assert.ok(text.includes('旧数据是否可读仍以当前状态和实际查询为准'));
          assert.equal(await p.getByRole('button', { name: '查看当前数据', exact: true }).isEnabled(), true);
          await geometry(p, `normal-${width}`); await shot(p, `status-${width}`);
          await position(p, '.qf-updates > section:nth-child(2)'); await shot(p, `update-${width}`);
        });
        await check(`${width}px: 20 server records, overlapping members, hidden diagnostics and manual tail`, async () => {
          assert.equal(await p.locator('.qf-updates-records > li').count(), 20);
          const values = await p.locator('.qf-updates-counts dd').allTextContents(); assert.deepEqual(values, ['43', '127', '20']);
          assert.equal(await p.locator('.qf-updates-record-heading strong').filter({ hasText: '本次来源刷新失败' }).count(), 4);
          assert.equal(await p.getByText('fixture-fixture.daily/scope-0', { exact: true }).first().isVisible(), false);
          await position(p, '.qf-updates-issues'); await shot(p, `issues-${width}`);
          const disclosure = p.getByText('问题诊断信息（第 1 条）', { exact: true });
          await disclosure.focus(); await disclosure.press('Enter');
          assert.equal(await p.getByText('fixture-fixture.daily/scope-0', { exact: true }).first().isVisible(), true);
          assert.notEqual(await disclosure.evaluate(e => getComputedStyle(e).outlineStyle), 'none');
          await position(p, '.qf-updates-records > li:first-child'); await shot(p, `diagnostics-${width}`);
          await p.getByRole('button', { name: '下一页问题', exact: true }).click(); await ready(p);
          await p.getByText('第 2 页 · 记录 21–40 / 43', { exact: true }).waitFor();
          await p.getByRole('button', { name: '下一页问题', exact: true }).click(); await ready(p);
          assert.equal(await p.locator('.qf-updates-records > li').count(), 3);
          assert.equal(await p.getByRole('button', { name: '下一页问题', exact: true }).isDisabled(), true);
          await p.getByText('第 3 页 · 记录 41–43 / 43', { exact: true }).waitFor();
          await position(p, '.qf-updates-issues'); await shot(p, `tail-${width}`);
          await p.getByRole('button', { name: '上一页问题', exact: true }).click(); await ready(p);
          await p.getByText('第 2 页 · 记录 21–40 / 43', { exact: true }).waitFor();
          await geometry(p, `paged-${width}`);
        });
      } finally { await c.close(); }
    }
    const c = await context(browser), p = await c.newPage();
    try {
      await check('complete, unqualified, qualified, absent and unknown update states never certify coverage', async () => {
        for (const name of ['complete-unqualified', 'qualified', 'no-update', 'unknown']) {
          await open(p, name); const text = await panel(p).innerText();
          assert.ok(text.includes('业务日期覆盖\n未声明'));
          if (name === 'complete-unqualified') { assert.ok(text.includes('已完成（服务端声明）')); assert.ok(text.includes('未获合格确认')); }
          if (name === 'qualified') assert.ok(text.includes('已声明合格（本次处理范围）'));
          if (name === 'no-update') { assert.ok(text.includes('暂无更新记录')); assert.ok(!text.includes('等待重试')); }
          if (name === 'unknown') { assert.ok(text.includes('状态待确认')); assert.ok(text.includes('更新状态待确认')); }
          await position(p, '.qf-updates > section:nth-child(2)'); await shot(p, `${name}-390`);
        }
      });
      await check('empty response and first-read API failure differ; issues failure retains descriptor', async () => {
        await open(p, 'empty'); assert.equal(await p.locator('.qf-updates-records > li').count(), 0);
        await p.getByText('当前筛选没有已登记的问题记录；不代表业务覆盖或质量已经确认。', { exact: true }).waitFor();
        assert.deepEqual(await p.locator('.qf-updates-counts dd').allTextContents(), ['0','0','0']);
        await position(p, '.qf-updates-issues'); await shot(p, 'empty-390');
        await scenario('issues-error'); await refreshIssues(p);
        await p.getByRole('alert').filter({ hasText: '问题读取失败' }).waitFor();
        assert.equal(await p.locator('.qf-updates-counts').count(), 0);
        assert.ok(!(await panel(p).innerText()).includes('没有已登记的问题记录'));
        await position(p, '.qf-updates-issues'); await shot(p, 'empty-refresh-failure-390');
        await open(p, 'issues-error'); await p.getByRole('alert').filter({ hasText: '问题读取失败' }).waitFor();
        assert.equal(await p.locator('.qf-updates-counts').count(), 0);
        assert.equal(await p.getByRole('heading', { name: '最近更新结果', exact: true }).count(), 1);
        assert.ok(!(await panel(p).innerText()).includes('没有已登记的问题记录'));
        await position(p, '.qf-updates-issues'); await shot(p, 'issues-error-390');
        await scenario('normal'); await refreshIssues(p); assert.equal(await p.locator('.qf-updates-records > li').count(), 20);
      });
      await check('ordinary page/refresh failure retains original page and time; refresh resets pagination', async () => {
        await open(p); await p.getByRole('button', { name: '下一页问题', exact: true }).click(); await ready(p);
        const readAt = await p.locator('.qf-updates-note').filter({ hasText: '问题列表读取于' }).innerText();
        await scenario('page-error'); await p.getByRole('button', { name: '下一页问题', exact: true }).click(); await ready(p);
        await p.getByRole('alert').filter({ hasText: '问题读取失败' }).waitFor();
        await p.getByText('第 2 页 · 记录 21–40 / 43', { exact: true }).waitFor();
        assert.equal(await p.locator('.qf-updates-note').filter({ hasText: '问题列表读取于' }).innerText(), readAt);
        await scenario('issues-error'); await refreshIssues(p);
        assert.equal(await p.getByRole('button', { name: '下一页问题', exact: true }).isDisabled(), true);
        assert.equal(await p.getByRole('button', { name: '上一页问题', exact: true }).isDisabled(), true);
        await p.getByText('第 2 页 · 记录 21–40 / 43', { exact: true }).waitFor();
        assert.equal(await p.locator('.qf-updates-note').filter({ hasText: '问题列表读取于' }).innerText(), readAt);
        await position(p, '.qf-updates-issues'); await shot(p, 'refresh-failure-390');
        await scenario('normal'); await refreshIssues(p); await p.getByText('第 1 页 · 记录 1–20 / 43', { exact: true }).waitFor();
      });
      await check('missing member counts and malformed pagination never become zeros or success', async () => {
        await open(p, 'counts-unknown'); assert.deepEqual(await p.locator('.qf-updates-counts dd').allTextContents(), ['43','未声明','20']);
        assert.ok((await p.locator('.qf-updates-records > li').first().innerText()).includes('本条受影响成员数：未声明'));
        await position(p, '.qf-updates-issues'); await shot(p, 'counts-unknown-390');
        await open(p, 'malformed'); await p.getByRole('alert').filter({ hasText: '问题读取失败' }).waitFor();
        assert.equal(await p.locator('.qf-updates-counts').count(), 0);
        await position(p, '.qf-updates-issues'); await shot(p, 'malformed-390');
      });
      await check('changed inventory clears cached page sequence', async () => {
        await open(p); await scenario('page-changed'); await p.getByRole('button', { name: '下一页问题', exact: true }).click(); await ready(p);
        await p.getByRole('alert').filter({ hasText: '旧分页已清除' }).waitFor();
        assert.equal(await p.locator('.qf-updates-records > li').count(), 0);
        await position(p, '.qf-updates-issues'); await shot(p, 'page-changed-390');
      });
      await check('repeated tab switches preserve page without requests; rapid refresh makes one GET', async () => {
        await open(p); await p.getByRole('button', { name: '下一页问题', exact: true }).click(); await ready(p);
        const before = requests.length;
        for (let i = 0; i < 12; i++) { await p.getByRole('tab', { name: '概览', exact: true }).click(); await p.getByRole('tab', { name: '更新与问题', exact: true }).click(); }
        assert.equal(requests.length, before); await p.getByText('第 2 页 · 记录 21–40 / 43', { exact: true }).waitFor();
        const countBefore = requests.filter(r => r.path.endsWith('/issues')).length;
        await scenario('slow'); await p.locator('.qf-updates-actions button').evaluate(button => { for (let i = 0; i < 15; i++) button.click(); });
        await p.getByRole('button', { name: '读取问题中…', exact: true }).waitFor();
        await position(p, '.qf-updates-issues'); await shot(p, 'loading-retained-390'); await ready(p);
        assert.equal(requests.filter(r => r.path.endsWith('/issues')).length - countBefore, 1);
        await scenario('normal'); const start = requests.length;
        await p.getByRole('button', { name: '刷新页面', exact: true }).click(); await ready(p);
        await p.waitForFunction(() => !document.querySelector('.qf-assets').getAttribute('aria-busy') || document.querySelector('.qf-assets').getAttribute('aria-busy') === 'false');
        assert.equal(requests.slice(start).filter(r => r.path.endsWith('/issues')).length, 1);
        assert.equal(requests.slice(start).filter(r => r.path.endsWith('/datasets/fixture.daily')).length, 1);
        await p.getByText('第 1 页 · 记录 1–20 / 43', { exact: true }).waitFor();
      });
      await check('object switch aborts old requests and never mixes late issue pages', async () => {
        await open(p); await scenario('slow'); await p.getByRole('button', { name: '重新读取问题', exact: true }).click();
        await p.getByRole('button', { name: '读取问题中…', exact: true }).waitFor();
        await scenario('normal'); await p.goto(detail('report')); await ready(p);
        await p.waitForTimeout(1700);
        assert.equal(await p.getByRole('heading', { name: '隔离示例：报告更新与问题', exact: true }).count(), 1);
        await p.getByText('问题诊断信息（第 1 条）', { exact: true }).click();
        assert.equal(await p.getByText('fixture-fixture.report/scope-0', { exact: true }).first().isVisible(), true);
        assert.equal(await p.getByText('fixture-fixture.daily/scope-0', { exact: true }).count(), 0);
        await position(p, '.qf-updates-issues'); await shot(p, 'switch-390');
      });
      await check('generation change resets page; current restrictions remain declarations without frontend permission grants', async () => {
        await open(p); await p.getByRole('button', { name: '下一页问题', exact: true }).click(); await ready(p);
        await scenario('generation-changed'); await p.getByRole('button', { name: '刷新页面', exact: true }).click(); await ready(p);
        await p.getByText('第 1 页 · 记录 1–20 / 43', { exact: true }).waitFor();
        for (const [name, label] of [['restricted','存在限制'], ['maintenance','暂不可读取／处理中'], ['rebuild-required','需要重建']]) {
          await open(p, name); assert.ok((await panel(p).innerText()).includes(label)); await shot(p, `${name}-390`);
        }
      });
      await check('panel links navigate only and keep list context in current-query navigation', async () => {
        await open(p); const before = requests.length;
        await p.getByRole('link', { name: '调整当前查询', exact: true }).click();
        await p.waitForURL(u => u.searchParams.get('view') === 'preview');
        const url = new URL(p.url()); assert.equal(url.searchParams.get('search'), '隔离'); assert.equal(url.searchParams.get('page'), '2');
        assert.equal(requests.length, before);
        for (const [label, pathname] of [['查看已有数据源','/admin/data-sources'], ['查看已有采集任务','/admin/tasks']]) {
          await open(p); await p.getByRole('link', { name: label, exact: true }).click(); await p.waitForURL(u => u.pathname === pathname);
        }
      });
      await check('403 revokes cached content across panels; 401 clears session and redirects', async () => {
        await open(p); await scenario('forbidden'); await p.getByRole('button', { name: '重新读取问题', exact: true }).click();
        await p.getByRole('alert').filter({ hasText: '没有当前数据的读取权限' }).waitFor();
        assert.equal(await panel(p).count(), 0); assert.equal(await p.locator('.qf-updates-records > li').count(), 0);
        assert.equal(await p.getByRole('button', { name: '查看当前数据', exact: true }).isDisabled(), true); await shot(p, 'forbidden-390');
        await scenario('normal'); await p.getByRole('button', { name: '刷新页面', exact: true }).click(); await ready(p);
        await scenario('unauthorized'); await p.getByRole('button', { name: '重新读取问题', exact: true }).click(); await p.waitForURL(u => u.pathname === '/login');
        assert.equal(await p.evaluate(() => sessionStorage.getItem('quant-foundry.api-token')), null); await shot(p, 'unauthorized-390');
      });
    } finally { await c.close(); }
    assert.deepEqual(errors, []);
    const dataReads = requests.filter(r => r.path.startsWith('/api/admin/data-store/'));
    assert.ok(dataReads.every(r => r.method === 'GET'));
    assert.ok(dataReads.every(r => /\/(issues|datasets(?:\/[^/]+)?)$/.test(r.path)));
    assert.ok(dataReads.filter(r => r.path.endsWith('/issues')).every(r => new URLSearchParams(r.query).get('limit') === '20'));
    const serverRequests = (await (await fetch(`${fixture}/__requests`)).json()).requests;
    assert.ok(serverRequests.some(r => r.aborted && r.scenario === 'slow'), 'Server observes cancelled old issue reads');
    fs.writeFileSync(path.join(output, 'acceptance.json'), JSON.stringify({ isolated: true, productionValidated: false, results,
      pageErrors: errors, requests: dataReads, geometry: geometryResults, serverRequests, screenshots: fs.readdirSync(output).filter(x => x.endsWith('.jpg')) }, null, 2));
    console.log(`D06 browser acceptance: ${results.length} passed; ${dataReads.length} GETs; ${errors.length} page errors.`);
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
