/** Real D09 browser checks against the owned preview's default formal APIs.
 * Success responses are never fabricated. Failure/late-response controls below
 * supplement the backend's actual quality-change tests. All displayed inputs
 * are public synthetic fixtures, not a live-data or deployment acceptance.
 */
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const base = process.env.QF_D09_URL || 'http://127.0.0.1:5173';
assert.ok(/^http:\/\/127\.0\.0\.1:\d+$/.test(base), 'local preview only');
assert.equal(process.env.QF_ENVIRONMENT, 'test');
assert.ok(process.env.QF_API_TOKEN && process.env.QF_TEST_POSTGRES_DSN, 'owned isolated test credential/DB required');
const output = fileURLToPath(new URL('../../ops/s2-d09/screenshots/', import.meta.url));
await mkdir(output, { recursive: true });
for (let n = 0; n < 150; n++) {
  try { if ((await fetch(base)).ok) break; } catch { /* owned Vite startup */ }
  if (n === 149) throw new Error('preview startup timed out');
  await new Promise(r => setTimeout(r, 100));
}
const browser = await chromium.launch({ executablePath: process.env.QF_BROWSER_PATH || '/usr/bin/chromium', headless: true, args: ['--no-sandbox'] });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
await context.addInitScript(token => sessionStorage.setItem('quant-foundry.api-token', token), process.env.QF_API_TOKEN);
const page = await context.newPage();
await page.clock.setFixedTime(new Date('2026-10-13T08:00:00Z'));
const cases = [], screenshots = [], marketRequests = [], legacyRequests = [], pageErrors = [];
page.on('pageerror', error => pageErrors.push(error.message));
page.on('request', request => {
  if (request.url().includes('/api/admin/market/')) marketRequests.push({ path: new URL(request.url()).pathname, method: request.method() });
  if (request.url().includes('/api/admin/data-collections/')) legacyRequests.push(request.url());
});
await page.route('**/api/admin/data-collections/**', route => route.abort());
const name = (text, role = 'button') => page.getByRole(role, { name: text, exact: true });
const table = () => page.getByRole('table', { name: 'ETF 基础资料' });
async function loaded() { await page.waitForFunction(() => document.querySelectorAll('tr[data-code]').length > 0); }
async function save(filename) { await page.screenshot({ path: output + filename }); screenshots.push(filename); }
async function check(label, work) { await work(); cases.push(label); console.log('PASS ' + label); }
async function goMarket(query = '') { await page.goto(base + '/admin/data/etf-basics' + query); await loaded(); }

try {
  await check('default formal directory and independent overview, with old market requests blocked', async () => {
    await goMarket();
    assert.equal(await table().locator('tbody tr[data-code]').count(), 50);
    assert.match(await page.locator('.qfm-workspace-meta').innerText(), /121/);
    assert.match(await page.locator('.qfm-market-band').innerText(), /上市日期范围/);
    assert.doesNotMatch(await page.locator('.qfm-market-band').innerText(), /数据覆盖|最近同步/);
    assert.equal(legacyRequests.length, 0);
  });
  await check('server search finds final-page ETF and typed searches cancel old conditions', async () => {
    await name('搜索 ETF', 'searchbox').fill('隔离夹具 ETF 120');
    await page.waitForFunction(() => document.querySelectorAll('tr[data-code]').length === 1);
    assert.equal(await table().locator('tr[data-code]').getAttribute('data-code'), '510120.SH');
    await name('搜索 ETF', 'searchbox').fill(''); await loaded();
    await page.waitForFunction(() => document.querySelectorAll('tr[data-code]').length === 50);
  });
  await check('three pages, backward paging and browser return preserve position/focus without persistent cursors', async () => {
    await name('下一页').click(); await page.waitForURL('**offset=50'); await loaded();
    assert.equal(await table().locator('tr[data-code]').first().getAttribute('data-code'), '510050.SH');
    await name('下一页').click(); await page.waitForURL('**offset=100'); await loaded();
    await page.waitForFunction(() => document.querySelector('tr[data-code]')?.getAttribute('data-code') === '510100.SH');
    assert.equal(await table().locator('tr[data-code]').count(), 21);
    await name('上一页').click(); await page.waitForURL('**offset=50'); await loaded();
    await page.waitForFunction(() => document.querySelector('tr[data-code]')?.getAttribute('data-code') === '510050.SH');
    const link=table().getByRole('link').first(); await link.focus(); await link.press('Enter');
    await page.waitForURL('**tab=profile'); await page.getByRole('heading',{name:'基础资料',exact:true}).waitFor();
    await page.waitForFunction(() => document.querySelector('.qfm-profile-grid')?.textContent.includes('510050.SH'));
    await name('返回 ETF 列表','link').click(); await page.waitForURL('**offset=50'); await loaded();
    assert.equal(await table().locator('tr[data-code]').first().getAttribute('data-code'), '510050.SH');
    assert.equal(await page.evaluate(() => document.activeElement?.textContent), '510050.SH');
    assert.ok(!page.url().includes('cursor') && !page.url().includes('read_token'));
    const stored=await page.evaluate(() => Object.entries(sessionStorage).filter(([key]) => key !== 'quant-foundry.api-token'));
    assert.ok(stored.every(([,value]) => !value.includes('read_token') && !value.includes('next_cursor')));
  });
  await check('exchange keyboard filter and unsupported listing filter explain limitations', async () => {
    await name('筛选交易所：全部交易所').focus(); await page.keyboard.press('ArrowDown');
    await page.keyboard.press('End'); await page.keyboard.press('Enter');
    await page.waitForURL('**exchange=SZSE'); await loaded();
    await page.waitForFunction(() => {
      const rows = [...document.querySelectorAll('tr[data-code]')];
      return rows.length > 0 && rows.every(row => row.getAttribute('data-code').endsWith('.SZ'));
    });
    assert.ok((await table().locator('tr[data-code]').evaluateAll(rows => rows.map(row=>row.dataset.code))).every(code => code.endsWith('.SZ')));
    await page.getByRole('group',{name:'上市状态筛选'}).getByRole('button',{name:'上市',exact:true}).click();
    await page.waitForFunction(() => document.querySelectorAll('tr[data-code]').length === 0);
    await page.getByText('当前目录缺少上市状态依据，请选择“全部”。',{exact:false}).waitFor();
    await page.getByRole('group',{name:'上市状态筛选'}).getByRole('button',{name:'全部',exact:true}).click(); await loaded();
  });
  await check('formal calendar distinguishes records and unknown dates; keyboard/modal focus is contained', async () => {
    await name('交易日历').click();
    const dialog=page.getByRole('dialog',{name:'交易日历'}); await dialog.waitFor();
    await dialog.getByRole('button',{name:'2026/10/01 休市',exact:true}).waitFor();
    await dialog.getByRole('button',{name:'2026/10/10 交易日',exact:true}).waitFor();
    await dialog.getByRole('button',{name:'2026/10/13 未知',exact:true}).click();
    await page.getByText('正式日期资料未说明此日交易状态，不能推定为休市。',{exact:false}).waitFor();
    assert.match(await page.locator('.qfm-calendar-aside').innerText(), /前一交易日\n—\n下一交易日\n—/);
    await dialog.getByRole('button',{name:'2026/10/13 未知',exact:true}).focus();
    await page.keyboard.press('ArrowRight');
    assert.equal(await page.evaluate(() => document.activeElement?.getAttribute('data-date')), '2026-10-14');
    for(let n=0;n<15;n++) { await page.keyboard.press('Tab'); assert.equal(await page.evaluate(() => document.querySelector('dialog')?.contains(document.activeElement)),true); }
    await page.keyboard.press('Escape'); await dialog.waitFor({state:'hidden'});
    assert.equal(await page.evaluate(() => document.activeElement?.textContent), '交易日历');
  });
  await check('published future facts survive month/year navigation', async () => {
    await name('交易日历').click();
    for(let n=0;n<3;n++) await name('下个月').click();
    await page.getByRole('button',{name:'2027/01/04 交易日',exact:true}).waitFor();
    await page.getByRole('button',{name:'2027/01/02 未知',exact:true}).waitFor();
    await page.keyboard.press('Escape');
  });
  await check('legacy symbol link resolves through D08 and formal profile without price/factor prerequisites', async () => {
    await page.goto(base+'/admin/data/etf-basics/510000.SH?tab=profile');
    await page.waitForFunction(() => document.querySelector('.qfm-profile-grid')?.textContent.includes('510000.SH'));
    await name('查看 K 线','link').waitFor();
    assert.match(await page.locator('.qfm-profile').innerText(), /可信身份\n可用/);
    assert.match(await page.locator('.qfm-profile').innerText(), /日线读取\n尚未核验/);
    await name('查看 K 线','link').waitFor();
    assert.equal(legacyRequests.length,0);
  });
  await check('metadata profile without a trusted mapping remains useful and never enables cross-code history', async () => {
    await page.goto(base+'/admin/data/etf-basics/510120.SH?tab=profile');
    await page.waitForFunction(() => document.querySelector('.qfm-profile-grid')?.textContent.includes('510120.SH'));
    await page.getByText('缺少可核验的标的代码映射。',{exact:false}).waitFor();
    assert.equal(await name('查看 K 线','link').count(),0);assert.equal(legacyRequests.length,0);
  });
  await check('late older filter response cannot repopulate a changed selection', async () => {
    await goMarket();
    await page.route('**/api/admin/market/instruments?**', async route => {
      if(new URL(route.request().url()).searchParams.get('q')==='隔离夹具 ETF 001') {
        const response=await route.fetch(); await new Promise(r=>setTimeout(r,500));
        await route.fulfill({response}).catch(()=>{});
      } else await route.continue();
    });
    await name('搜索 ETF','searchbox').fill('隔离夹具 ETF 001');
    await page.waitForRequest(request=>new URL(request.url()).searchParams.get('q')==='隔离夹具 ETF 001');
    await name('搜索 ETF','searchbox').fill('隔离夹具 ETF 120');
    await page.waitForFunction(() => document.querySelector('tr[data-code]')?.getAttribute('data-code')==='510120.SH');
    await page.waitForTimeout(650);
    assert.equal(await table().locator('tr[data-code]').getAttribute('data-code'),'510120.SH');
    await page.unroute('**/api/admin/market/instruments?**');
  });
  await check('network failure retains only explicitly stale same-condition data and disables paging', async () => {
    await goMarket();
    await page.route('**/api/admin/market/instruments?**',route=>route.abort());
    await name('重新读取').click();
    await page.getByText('当前显示同条件的旧资料，不能继续翻页。',{exact:false}).waitFor();
    assert.equal(await table().locator('tr[data-code]').count(),50);assert.equal(await name('下一页').isDisabled(),true);
    await page.unroute('**/api/admin/market/instruments?**');
  });
  await check('changed-data error withdraws rows and continuation rather than keeping stale data', async () => {
    await goMarket();
    await page.route('**/api/admin/market/instruments?**',route=>route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({detail:{code:'DATA_CHANGED',message:'unsafe backend text'}})}));
    await name('重新读取').click();await page.waitForFunction(()=>document.querySelectorAll('tr[data-code]').length===0);
    await page.getByText('当前读取依赖已改变，请重新加载，旧分页已清除。',{exact:false}).waitFor();
    assert.equal(await name('下一页').isDisabled(),true);await page.unroute('**/api/admin/market/instruments?**');
  });
  for(const [width,height] of [[1440,900],[1024,768],[390,844]]) {
    await check(`interactive ${width}x${height} list/profile/calendar screenshot set`,async()=>{
      await page.setViewportSize({width,height});await goMarket();
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      await save(`list-${width}.png`);
      if(width===390) {
        await page.locator('.qfm-table-wrap').scrollIntoViewIfNeeded(); await save('list-table-390.png');
      }
      await table().getByRole('link').first().click();
      await page.waitForFunction(()=>document.querySelector('.qfm-profile-grid')?.textContent.includes('510000.SH'));
      await name('查看 K 线','link').waitFor();
      await save(`profile-${width}.png`);assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      const capabilityGrid=page.locator('.qfm-profile-grid').last();
      const lastCapability=await capabilityGrid.locator('div').last().boundingBox();
      const profileBounds=await page.locator('.qfm-profile').boundingBox();
      assert.ok(lastCapability.y+lastCapability.height<=profileBounds.y+profileBounds.height+1,'profile capabilities cannot be clipped');
      if(width!==1440) {
        await capabilityGrid.scrollIntoViewIfNeeded();await save(`profile-capabilities-${width}.png`);
      }
      await name('返回 ETF 列表','link').click();await loaded();await name('交易日历').click();
      await page.getByRole('button',{name:'2026/10/01 休市',exact:true}).waitFor();
      await save(`calendar-${width}.png`);
      if(width===390) {
        await page.locator('.qfm-calendar-aside').scrollIntoViewIfNeeded();await save('calendar-detail-390.png');
      }
      await page.keyboard.press('Escape');
    });
  }
  await check('403 from calendar withdraws data from the already mounted catalog panel too',async()=>{
    await goMarket();await name('交易日历').click();
    await page.getByRole('button',{name:'2026/10/01 休市',exact:true}).waitFor();
    await page.route('**/api/admin/market/calendar?**',route=>route.fulfill({status:403,body:'permission denied'}));
    await name('下个月').click();await page.waitForFunction(()=>document.querySelectorAll('tr[data-code]').length===0);
    await page.getByText('当前账号无权读取这些市场数据。',{exact:false}).first().waitFor();
    assert.equal(await page.locator('.qfm-workspace-meta').innerText(),'目录 ETF — 只');
    await page.unroute('**/api/admin/market/calendar?**');await page.keyboard.press('Escape');
  });
  await check('identity denial withdraws the profile and cannot automatically retry a source-scoped profile',async()=>{
    await page.goto(base+'/admin/data/etf-basics/510000.SH?tab=profile'); await name('查看 K 线','link').waitFor();
    const start=marketRequests.length;
    await page.route('**/api/admin/market/instruments/resolve?**',route=>route.fulfill({status:403,body:'permission denied'}));
    await name('重新读取').click();
    await page.getByText('当前账号无权读取这些市场数据。',{exact:false}).first().waitFor();
    await page.waitForFunction(()=>document.querySelectorAll('.qfm-profile-grid').length===0);
    await page.waitForTimeout(350);
    assert.equal(await page.locator('.qfm-profile-grid').count(),0);
    assert.ok(!marketRequests.slice(start).some(request=>request.path.includes('etf-current')));
    await page.unroute('**/api/admin/market/instruments/resolve?**');
  });
  await check('401 triggers existing login flow and clears market content',async()=>{
    await goMarket();await page.route('**/api/admin/market/instruments?**',route=>route.fulfill({status:401,body:'auth denied'}));
    await name('重新读取').click();await page.waitForURL('**/login');
    assert.equal(await table().count(),0);await page.unroute('**/api/admin/market/instruments?**');
  });
  assert.equal(legacyRequests.length,0);assert.ok(marketRequests.length>30);assert.ok(marketRequests.every(request=>request.method==='GET'));
  assert.deepEqual(pageErrors,[]);
  const report={code_ready_browser:true,live_verified:false,deployed:false,fixture:'121 synthetic E67 records and explicit E68 dates via real formal pipeline',cases,screenshots,market_requests:marketRequests.length,legacy_requests:legacyRequests.length,page_errors:pageErrors};
  await writeFile(new URL('../../ops/s2-d09/UI_RESULT.json',import.meta.url),JSON.stringify(report,null,2)+'\n');
  console.log(`D09 browser: ${cases.length} cases passed, ${screenshots.length} screenshots, ${marketRequests.length} formal GETs, 0 legacy market requests`);
} finally { await context.close(); await browser.close(); }
