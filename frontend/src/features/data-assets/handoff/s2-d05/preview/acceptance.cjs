/* Real browser interactions against the synthetic loopback D05 fixture only. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.FRONTEND_URL || 'http://127.0.0.1:5186';
const fixture = 'http://127.0.0.1:18767';
assert.ok(['127.0.0.1', 'localhost'].includes(new URL(base).hostname));
const output = process.env.SCREENSHOT_DIR || path.resolve(__dirname, '../screenshots');
fs.mkdirSync(output, { recursive: true });
const results = [], pageErrors = [], clientRequests = [], geometryResults = [];
const names = { daily: '隔离示例：日线（来源报告值）', nav: '隔离示例：净值（来源报告值）', report: '隔离示例：报告片段（来源报告值）' };
const url = (kind = 'daily', view = 'preview') => `${base}/admin/data-assets/fixture.${kind}?search=隔离&page=1&view=${view}`;
const panel = page => page.locator('#qf-assets-panel-preview .qf-preview');
const table = page => panel(page).getByRole('region', { name: '当前对象示例表格', exact: true });
const queries = () => clientRequests.filter(item => item.method === 'POST');
async function scenario(name) {
  const response = await fetch(`${fixture}/__scenario?name=${name}`);
  assert.equal(response.status, 200); assert.equal((await response.json()).scenario, name);
}
async function check(name, run) { await run(); results.push(name); console.log(`PASS: ${name}`); }
async function open(page, kind = 'daily', view = 'preview') {
  await page.goto(url(kind, view));
  await page.getByRole('heading', { name: names[kind], exact: true }).waitFor();
}
async function openRead(page, kind = 'daily') { await scenario('normal'); await open(page, kind); await table(page).waitFor(); }
async function reread(page) { await panel(page).getByRole('button', { name: '查看当前数据', exact: true }).click(); }
async function adjust(page) { await panel(page).locator('.qf-preview-adjust > summary').click(); }
async function refresh(page) {
  const read = page.waitForResponse(r => new URL(r.url()).pathname.startsWith('/api/admin/data-store/datasets/'));
  await page.getByRole('button', { name: '刷新页面', exact: true }).click(); await read;
  await page.getByRole('button', { name: '刷新页面', exact: true }).waitFor();
}
async function cleared(page) { await table(page).waitFor({ state: 'detached' }); assert.equal(await panel(page).locator('.qf-assets-pager').count(), 0); }
async function shot(page, name, focus = true) {
  if (focus && await panel(page).count()) await page.evaluate(() => {
    const main = document.querySelector('.qfo-main'), element = document.querySelector('.qf-preview');
    main.scrollTop += element.getBoundingClientRect().top - main.getBoundingClientRect().top - 12;
  });
  await page.screenshot({ path: path.join(output, `${name}.jpg`), type: 'jpeg', quality: 88, animations: 'disabled' });
}
async function context(browser, width = 390) {
  const c = await browser.newContext({ viewport: { width, height: width === 1440 ? 900 : width === 1024 ? 768 : 844 } });
  await c.addInitScript(() => {
    if (!['127.0.0.1', 'localhost'].includes(location.hostname)) return;
    // Exercise the existing authentication flow with a synthetic fixture value only.
    sessionStorage.setItem('quant-foundry.api-token', 's2-d05-isolated-fixture');
    localStorage.setItem('qfo-sidebar', innerWidth > 980 ? 'expanded' : 'collapsed');
    const originalFetch = window.fetch.bind(window);
    window.fetch = (input, options) => {
      if (String(input).endsWith('/query') && options?.body && JSON.parse(options.body).subject === 'fixture-ignore-abort') {
        const ignoredSignal = { ...options }; delete ignoredSignal.signal;
        return originalFetch(input, ignoredSignal);
      }
      return originalFetch(input, options);
    };
    addEventListener('DOMContentLoaded', () => {
      const label = document.createElement('div');
      label.textContent = '隔离夹具 · 非真实接口验收'; label.setAttribute('aria-hidden', 'true');
      label.style.cssText = 'position:fixed;right:8px;bottom:8px;z-index:9999;pointer-events:none;padding:4px 8px;border:1px solid #d3b9a1;border-radius:6px;background:#fffaf2;color:#77511f;font:14px/22px sans-serif';
      document.body.append(label);
    });
  });
  await c.route(url => !['127.0.0.1', 'localhost'].includes(url.hostname), route => route.abort());
  c.on('page', p => {
    p.setDefaultTimeout(8000);
    p.on('pageerror', error => pageErrors.push(error.message));
    p.on('request', request => {
      if (new URL(request.url()).pathname.startsWith('/api/admin/data-store/')) clientRequests.push({
        method: request.method(), path: new URL(request.url()).pathname,
        body: request.method() === 'POST' ? request.postDataJSON() : null
      });
    });
  });
  return c;
}
async function geometry(page, kind) {
  const measured = await page.evaluate(() => {
    const main = document.querySelector('.qfo-main'), element = document.querySelector('.qf-preview');
    const region = element.querySelector('.qf-preview-scroll');
    const button = [...element.querySelectorAll('button')].find(e => e.textContent === '查看当前数据');
    return { viewport: innerWidth, document: document.documentElement.scrollWidth, mainOverflow: getComputedStyle(main).overflowY,
      table: region && { width: region.clientWidth, content: region.scrollWidth, height: region.clientHeight, contentHeight: region.scrollHeight },
      prose: [...element.querySelectorAll('p')].filter(e => !e.closest('details')).map(e => getComputedStyle(e).fontSize),
      action: { width: button.getBoundingClientRect().width, right: button.getBoundingClientRect().right } };
  });
  assert.ok(measured.document <= measured.viewport + 1, 'No document horizontal overflow');
  assert.equal(measured.mainOverflow, 'auto');
  assert.ok(measured.table.content > measured.table.width, 'Dense precision columns use local horizontal scrolling');
  assert.ok(measured.table.contentHeight <= measured.table.height + 1, 'No nested vertical table scrolling');
  assert.ok(measured.action.width > 0 && measured.action.right <= measured.viewport + 1);
  for (const size of measured.prose) assert.equal(size, '14px');
  geometryResults.push({ kind, ...measured });
}
async function storageSafe(page) {
  const state = await page.evaluate(() => ({ url: location.href,
    session: Object.fromEntries(Object.entries(sessionStorage)), local: Object.fromEntries(Object.entries(localStorage)) }));
  const persisted = JSON.stringify({ url: state.url, session: Object.fromEntries(Object.entries(state.session)
    .filter(([key]) => key !== 'quant-foundry.api-token')), local: state.local });
  for (const forbidden of ['fixture-cursor:', '12345678901234567890', '900719925474099312345', 'fixture-daily-object', 'fixture-new-object']) {
    assert.ok(!persisted.includes(forbidden), `${forbidden} must remain in memory`);
  }
  assert.ok(!new URL(state.url).searchParams.has('cursor')); assert.ok(!state.url.includes('s2-d05-isolated-fixture'));
}

(async () => {
  const initialServerCount = (await (await fetch(`${fixture}/__requests`)).json()).requests.length;
  const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium', args: ['--no-sandbox'] });
  try {
    for (const width of [1440, 1024, 390]) {
      const c = await context(browser, width), p = await c.newPage();
      try {
        await check(`${width}px: one header action reads one bounded example using preview_key`, async () => {
          await scenario('normal'); await open(p, 'daily', 'overview');
          const before = queries().length;
          await p.getByRole('button', { name: '查看当前数据', exact: true }).click(); await table(p).waitFor();
          assert.equal(queries().length, before + 1);
          const body = queries().at(-1).body;
          assert.equal(body.representation, 'fixture_daily_reported'); assert.equal(body.subject, 'fixture-daily-object');
          assert.equal(body.allow_partial, false); assert.equal(body.page_size, 20); assert.equal(body.columns.length, 8);
          assert.ok(!body.columns.includes('basis_state')); assert.ok(!body.columns.includes('basis_token'));
          assert.equal(body.cursor, null); assert.equal(await panel(p).locator('.qf-preview-adjust').getAttribute('open'), null);
          assert.ok((await panel(p).innerText()).includes('本页范围：2026-01-02 ～ 2026-01-02'));
          assert.ok((await panel(p).innerText()).includes('业务日期覆盖未验证'));
          await shot(p, `entry-${width}`, false); await shot(p, `daily-${width}`); await geometry(p, `daily-${width}`);
        });
        await check(`${width}px: exact decimal, unquoted huge integer/ns, null, false and zero`, async () => {
          const cells = table(p).locator('tbody tr').first().locator('td');
          const values = await cells.allTextContents();
          assert.deepEqual(values.slice(2), ['12345678901234567890.123456789012345678', '900719925474099312345', '1791619200123456789', '—', 'false', '0']);
          await table(p).evaluate(e => { e.scrollLeft = e.scrollWidth; }); await shot(p, `precision-${width}`);
          await p.keyboard.press('Tab'); await table(p).focus(); assert.equal(await table(p).evaluate(e => e === document.activeElement), true);
          assert.equal(await table(p).evaluate(e => getComputedStyle(e).outlineStyle), 'solid');
        });
        await check(`${width}px: page stack replaces pages and GET refresh/tab revisits never POST`, async () => {
          await panel(p).getByRole('button', { name: '下一页', exact: true }).click();
          await panel(p).getByText('第 2 页 · 本页 1 行', { exact: true }).waitFor();
          assert.equal(await table(p).locator('tbody tr').count(), 1);
          assert.ok((await table(p).innerText()).includes('0.000000000000000001'));
          await panel(p).getByRole('button', { name: '上一页', exact: true }).click();
          await panel(p).getByText('第 1 页 · 本页 2 行', { exact: true }).waitFor();
          const before = queries().length;
          for (let i = 0; i < 3; i++) {
            await p.getByRole('tab', { name: '概览', exact: true }).click();
            await p.getByRole('tab', { name: '数据预览', exact: true }).click();
          }
          await refresh(p); assert.equal(queries().length, before);
          assert.equal(await table(p).locator('tbody tr').count(), 2); await storageSafe(p);
        });
        await check(`${width}px: NAV and report pages keep qualifiers and report fragments`, async () => {
          for (const kind of ['nav', 'report']) {
            await openRead(p, kind); await shot(p, `${kind}-${width}`); await geometry(p, `${kind}-${width}`);
            if (kind === 'report') {
              assert.ok((await panel(p).innerText()).includes('当前报告片段；节点可能跨页'));
              await panel(p).getByRole('button', { name: '下一页', exact: true }).click();
              await panel(p).getByText('第 2 页 · 本页 1 行', { exact: true }).waitFor();
              assert.equal(await table(p).locator('tbody tr').count(), 1); await shot(p, `report-page-2-${width}`);
            }
          }
        });
      } finally { await c.close(); }
    }
    const c = await context(browser), p = await c.newPage();
    try {
      await check('condition edits clear old cursors/results; text keys, page size and declared columns are bounded', async () => {
        await openRead(p); await adjust(p);
        const subject = panel(p).getByRole('textbox', { name: '对象键（文本）', exact: true });
        await subject.fill('fixture-new-object'); await cleared(p);
        assert.equal(await panel(p).getByRole('textbox', { name: '起始业务键（文本）', exact: true }).getAttribute('type'), 'text');
        await panel(p).getByRole('combobox', { name: '每页行数', exact: true }).selectOption('100');
        await reread(p); await table(p).waitFor();
        assert.equal(queries().at(-1).body.cursor, null); assert.equal(queries().at(-1).body.page_size, 100);
        assert.ok((await table(p).innerText()).includes('fixture-new-object'));
        await panel(p).getByRole('textbox', { name: '结束业务键（文本）', exact: true }).fill('2026-01-03'); await cleared(p);
        await reread(p); await table(p).waitFor(); assert.equal(queries().at(-1).body.to_key, '2026-01-03');
        await panel(p).getByText('选择预览列（8 / 32）', { exact: true }).click();
        await panel(p).getByRole('checkbox', { name: 'f6_text', exact: true }).check(); await cleared(p);
        await reread(p); await table(p).waitFor();
        assert.ok((await table(p).innerText()).includes('2026-10-10T08:00:00.123456789Z'));
        assert.equal(queries().at(-1).body.cursor, null);
        await shot(p, 'adjusted-390'); await storageSafe(p);
        await openRead(p, 'report'); await adjust(p); await panel(p).getByText('选择预览列（8 / 32）', { exact: true }).click();
        const boxes = panel(p).locator('.qf-preview-columns input');
        for (let i = 0; i < await boxes.count(); i++) {
          if (await boxes.nth(i).isEnabled() && !await boxes.nth(i).isChecked()) await boxes.nth(i).check();
        }
        assert.equal(await panel(p).locator('.qf-preview-columns input:checked').count(), 32);
        assert.ok(await panel(p).locator('.qf-preview-columns input:not(:checked):disabled').count() > 0);
        await reread(p); await table(p).waitFor(); assert.equal(queries().at(-1).body.columns.length, 32);
      });
      await check('invalid lexical range is rejected locally without a request or old page', async () => {
        await openRead(p); await adjust(p);
        await panel(p).getByRole('textbox', { name: '起始业务键（文本）', exact: true }).fill('2026-12-31'); await cleared(p);
        const before = queries().length; await reread(p);
        await panel(p).getByRole('alert').filter({ hasText: '查询条件不符合' }).waitFor(); assert.equal(queries().length, before);
      });
      await check('cancel loading query and ignore its late completion', async () => {
        await scenario('slow'); await open(p);
        await panel(p).getByRole('button', { name: '取消读取', exact: true }).waitFor(); await shot(p, 'loading-390');
        await panel(p).getByRole('button', { name: '取消读取', exact: true }).click();
        await panel(p).getByText('本次读取已取消，请重新查看当前数据。', { exact: true }).waitFor();
        await p.waitForTimeout(1500); assert.equal(await table(p).count(), 0); await shot(p, 'cancelled-390');
      });
      await check('rapid subject changes reject a transport that ignores AbortSignal', async () => {
        await openRead(p); await adjust(p);
        const subject = panel(p).getByRole('textbox', { name: '对象键（文本）', exact: true });
        await subject.fill('fixture-ignore-abort'); await reread(p);
        await panel(p).getByRole('button', { name: '取消读取', exact: true }).waitFor();
        await subject.fill('fixture-new-object'); await reread(p); await table(p).waitFor();
        await p.waitForTimeout(1500);
        assert.ok((await table(p).innerText()).includes('fixture-new-object')); assert.ok(!(await table(p).innerText()).includes('fixture-ignore-abort'));
        assert.equal(queries().at(-1).body.cursor, null); await storageSafe(p); await adjust(p); await shot(p, 'late-response-390');
      });
      await check('leaving the tab cancels a pending read without automatic retry on return', async () => {
        await scenario('slow'); await open(p); await panel(p).getByRole('button', { name: '取消读取', exact: true }).waitFor();
        await p.getByRole('tab', { name: '概览', exact: true }).click();
        const before = queries().length; await p.getByRole('tab', { name: '数据预览', exact: true }).click();
        await p.waitForTimeout(1500); assert.equal(queries().length, before); assert.equal(await table(p).count(), 0);
        await panel(p).getByText('已取消离开预览前的读取，请重新查看当前数据。', { exact: true }).waitFor();
      });
      await check('unmount/dataset switch cancels old query and rejects late responses', async () => {
        await openRead(p); await adjust(p);
        await panel(p).getByRole('textbox', { name: '对象键（文本）', exact: true }).fill('fixture-ignore-abort'); await reread(p);
        await panel(p).getByRole('button', { name: '取消读取', exact: true }).waitFor();
        await p.getByRole('link', { name: '← 数据资产', exact: true }).click();
        await p.getByRole('link', { name: names.nav, exact: true }).click();
        await p.getByRole('button', { name: '查看当前数据', exact: true }).click(); await table(p).waitFor(); await p.waitForTimeout(1500);
        assert.ok((await table(p).innerText()).includes('fixture-nav-object')); assert.ok(!(await panel(p).innerText()).includes('fixture-ignore-abort'));
      });
      for (const [name, error] of [['generation-next', '当前数据已改变'], ['query-changed', '当前数据已改变'],
        ['restriction-next', '本次读取受到数据限制'], ['query-restricted', '本次读取受到数据限制'],
        ['query-maintenance', '正在维护或重建'], ['query-rebuild', '当前数据需要重建'],
        ['query-file-invalid', '当前数据文件暂不可读取'], ['query-unknown-error', '数据服务暂不可用']]) {
        await check(`${name}: withdraw rows/page stack; no partial-read or retry loop`, async () => {
          await openRead(p); await scenario(name); const before = queries().length;
          await panel(p).getByRole('button', { name: '下一页', exact: true }).click();
          await panel(p).getByRole('alert').filter({ hasText: error }).waitFor(); await cleared(p);
          await p.waitForTimeout(100); assert.equal(queries().length, before + 1); assert.equal(queries().at(-1).body.allow_partial, false);
          await shot(p, `${name}-390`);
          await scenario('normal'); await reread(p); await table(p).waitFor(); assert.equal(queries().at(-1).body.cursor, null);
        });
      }
      for (const name of ['metadata-generation', 'metadata-quality', 'metadata-schema', 'metadata-subject']) {
        await check(`${name}: GET metadata boundary clears old page without auto POST`, async () => {
          await openRead(p); await scenario(name); const before = queries().length;
          await refresh(p); await cleared(p); assert.equal(queries().length, before);
          await panel(p).getByRole('alert').filter({ hasText: '旧结果与分页已清除' }).waitFor(); await shot(p, `${name}-390`);
          await reread(p); await table(p).waitFor(); assert.equal(queries().at(-1).body.cursor, null);
          if (name === 'metadata-subject') assert.equal(queries().at(-1).body.subject, 'fixture-new-default');
        });
      }
      await check('metadata generation changes cancel an in-flight query before it can return', async () => {
        await scenario('slow'); await open(p); await panel(p).getByRole('button', { name: '取消读取', exact: true }).waitFor();
        await scenario('metadata-generation'); const before = queries().length;
        await refresh(p); await panel(p).getByRole('alert').filter({ hasText: '旧结果与分页已清除' }).waitFor();
        await p.waitForTimeout(1500); assert.equal(await table(p).count(), 0); assert.equal(queries().length, before);
      });
      for (const name of ['query-network', 'query-timeout']) await check(`${name}: stale page retains its time and cannot continue pagination`, async () => {
        await openRead(p); const timestamp = await panel(p).locator('.qf-assets-asof').innerText();
        await scenario(name);
        // Vite converts an upstream disconnect to HTTP 500. Abort the browser's
        // loopback request to exercise a true connection failure instead.
        const networkFailure = route => route.abort('connectionfailed');
        if (name === 'query-network') await p.route('**/api/admin/data-store/query', networkFailure);
        await panel(p).getByRole('button', { name: '下一页', exact: true }).click();
        await panel(p).getByText('保留的是上述时刻读取的旧预览，当前读取未验证。分页已停用，请重新查看当前数据。', { exact: true }).waitFor();
        assert.equal(await panel(p).locator('.qf-assets-asof').innerText(), timestamp);
        assert.equal(await table(p).locator('tbody tr').count(), 2);
        assert.equal(await panel(p).getByRole('button', { name: '下一页', exact: true }).isDisabled(), true);
        assert.equal(await panel(p).getByRole('button', { name: '上一页', exact: true }).isDisabled(), true);
        assert.ok((await panel(p).locator('.qf-assets-preview-meta').innerText()).startsWith('上次读取的预览'));
        await shot(p, `${name}-390`);
        await reread(p); await panel(p).getByRole('alert').waitFor();
        assert.equal(await panel(p).locator('.qf-assets-asof').innerText(), timestamp);
        if (name === 'query-network') await p.unroute('**/api/admin/data-store/query', networkFailure);
        await scenario('normal'); await reread(p); await table(p).waitFor(); assert.equal(queries().at(-1).body.cursor, null);
      });
      for (const name of ['query-unknown-status', 'query-schema-mismatch', 'query-partial', 'query-malformed', 'query-dataset-mismatch']) {
        await check(`${name}: malformed/undeclared permission never displays a new page`, async () => {
          await openRead(p); await scenario(name); await reread(p);
          await panel(p).getByRole('alert').filter({ hasText: '返回格式异常' }).waitFor(); await cleared(p);
          assert.ok(!(await panel(p).innerText()).includes('fixture raw upstream text'));
        });
      }
      for (const [name, text] of [['no-preview', '暂无可预填对象'], ['no-columns', '服务端未声明可查询字段'],
        ['dataset-empty', '数据集当前为空（按服务端声明）']]) await check(`${name}: no invented key/columns/query`, async () => {
        await scenario(name); const before = queries().length; await open(p); await panel(p).getByText(text, { exact: false }).first().waitFor();
        assert.equal(queries().length, before); assert.equal(await panel(p).getByRole('button', { name: '查看当前数据', exact: true }).isDisabled(), true);
        await shot(p, `${name}-390`);
        await scenario('normal'); await refresh(p); assert.equal(queries().length, before, 'Adding preview metadata on GET does not auto-query');
        await reread(p); await table(p).waitFor();
      });
      await check('empty page, query miss, undeclared fields and descriptor restrictions stay distinct', async () => {
        await scenario('empty-page'); await open(p); await panel(p).getByText('本页为空，服务端仍提供后续页。', { exact: true }).waitFor();
        await shot(p, 'empty-page-390'); await panel(p).getByRole('button', { name: '下一页', exact: true }).click(); await table(p).waitFor();
        await scenario('query-empty'); await open(p); await panel(p).getByText('查询范围无匹配记录；不能据此判断整个数据集为空。', { exact: true }).waitFor(); await shot(p, 'query-empty-390');
        await scenario('no-fields'); await open(p); await table(p).waitFor(); assert.deepEqual(queries().at(-1).body.columns, ['object_key', 'member_key']);
        assert.ok((await panel(p).innerText()).includes('技术预览')); await shot(p, 'identity-only-390');
        await scenario('metadata-restricted-readable'); await open(p); await table(p).waitFor();
        assert.ok((await panel(p).innerText()).includes('当前对象是否可读以本次服务端判断为准')); await shot(p, 'restricted-readable-390');
      });
      for (const name of ['query-forbidden', 'metadata-forbidden', 'issues-forbidden']) await check(`${name}: 403 clears every mounted preview and its cursors`, async () => {
        if (name.startsWith('issues')) { await scenario('issues-readable'); await open(p); await table(p).waitFor(); }
        else await openRead(p);
        await scenario(name);
        if (name.startsWith('query')) await reread(p); else if (name.startsWith('metadata')) await refresh(p);
        else { await p.getByRole('tab', { name: '更新与问题', exact: true }).click(); await p.getByRole('button', { name: '刷新页面', exact: true }).click(); }
        await p.getByRole('alert').filter({ hasText: '读取权限' }).first().waitFor();
        assert.equal(await p.locator('.qf-preview-result').count(), 0); assert.equal(await p.locator('.qf-assets-pager[aria-label="当前对象预览分页"]').count(), 0);
        await shot(p, `${name}-390`, false); await scenario('normal');
        const beforeRecovery = queries().length; await refresh(p); await p.waitForTimeout(50);
        assert.equal(queries().length, beforeRecovery, 'Permission recovery GET must not issue a preview POST');
        await p.getByRole('tab', { name: '数据预览', exact: true }).click(); await reread(p); await table(p).waitFor();
        assert.equal(queries().at(-1).body.cursor, null);
      });
      for (const name of ['query-unauthorized', 'metadata-unauthorized', 'issues-unauthorized']) await check(`${name}: 401 clears data and follows the existing login flow`, async () => {
        if (name.startsWith('issues')) { await scenario('issues-readable'); await open(p); await table(p).waitFor(); }
        else await openRead(p);
        await scenario(name);
        if (name.startsWith('query')) await reread(p); else if (name.startsWith('metadata')) {
          await p.getByRole('button', { name: '刷新页面', exact: true }).click();
        } else { await p.getByRole('tab', { name: '更新与问题', exact: true }).click(); await p.getByRole('button', { name: '刷新页面', exact: true }).click(); }
        await p.waitForURL(`${base}/login`); assert.equal(await p.locator('.qf-preview-result').count(), 0);
        assert.equal(await p.evaluate(() => sessionStorage.getItem('quant-foundry.api-token')), null);
        await shot(p, `${name}-390`, false);
      });
    } finally { await c.close(); }
    const serverRequests = (await (await fetch(`${fixture}/__requests`)).json()).requests.slice(initialServerCount);
    assert.ok(serverRequests.some(item => item.scenario === 'slow' && item.aborted), 'Actual query cancellation reached the fixture');
    assert.ok(serverRequests.some(item => item.body?.subject === 'fixture-ignore-abort' && !item.aborted), 'An intentionally uncancelled late response was tested');
    assert.equal(pageErrors.length, 0, pageErrors.join('; '));
    assert.ok(clientRequests.every(item => item.method === 'GET' || item.method === 'POST' && item.path === '/api/admin/data-store/query'));
    for (const item of queries()) {
      assert.equal(item.body.allow_partial, false); assert.ok(item.body.page_size <= 100); assert.ok(item.body.columns.length <= 32);
      assert.notEqual(item.body.representation, 'typed-object-nodes-v2');
    }
    fs.writeFileSync(path.join(output, 'acceptance.json'), JSON.stringify({ isolated: true, passed: results.length,
      cases: results, pageErrors, geometry: geometryResults, clientRequests, serverRequests,
      realApiVerified: false, productionOperations: false }, null, 2));
    console.log(`D05 acceptance: ${results.length} passed; ${pageErrors.length} page errors; GET and bounded POST /query only.`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
