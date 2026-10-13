/** Real default app + artificial formal files. No successful HTTP body is fabricated. */
import { createRequire } from 'node:module';
import { writeFile } from 'node:fs/promises';
import assert from 'node:assert/strict';
const require=createRequire(new URL('../../frontend/package.json',import.meta.url));
const {chromium}=require('playwright');
const base=process.env.QF_D12_FORMAL_URL;
assert.match(base,/^http:\/\/127\.0\.0\.1:\d+$/);
assert.equal(process.env.QF_ENVIRONMENT,'test');
assert.ok(process.env.QF_D12_TEST_TOKEN);
const browser=await chromium.launch({executablePath:process.env.QF_BROWSER_PATH||'/usr/bin/chromium',headless:true,args:['--no-sandbox']});
try{
 const page=await browser.newPage({viewport:{width:1440,height:900},timezoneId:'America/Los_Angeles'});
 const requests=[],errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 page.on('request',r=>{if(new URL(r.url()).pathname.startsWith('/api/'))requests.push(new URL(r.url()).pathname);});
 await page.route('**/api/admin/data-collections/**',r=>r.abort());
 await page.addInitScript(token=>sessionStorage.setItem('quant-foundry.api-token',token),process.env.QF_D12_TEST_TOKEN);
 await page.goto(base+'/admin');
 await page.waitForFunction(()=>document.querySelector('.qfo-asset-row b')?.textContent==='1');
 assert.ok((await page.locator('.qfo-side-summary').textContent()).includes('业务截至：未知'));
 await page.locator('.qfo-asset-row').first().click();
 await page.locator('tr[data-code="510300.SH"]').waitFor();
 await page.getByRole('button',{name:'交易日历',exact:true}).click();
 await page.locator('.qfm-day').first().waitFor();
 await page.waitForFunction(()=>document.querySelectorAll('.qfm-day').length===42);
 await page.getByRole('button',{name:'关闭交易日历',exact:true}).click();
 await page.locator('tr[data-code="510300.SH"]').getByRole('link',{name:'查看 510300.SH ETF 详情',exact:true}).click();
 await page.getByRole('link',{name:'查看 K 线',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('.qfe-chart-caption')?.textContent.includes('不复权'));
 await page.waitForFunction(()=>document.querySelector('.qfe-history-actions')?.textContent.includes('10 / 10,000'));
 assert.equal(await page.locator('.qfe-chart-empty').count(),0);
 assert.equal(await page.locator('#qfb-price-basis option[value="pre"]').isDisabled(),true);
 assert.equal(await page.locator('#qfb-price-basis option[value="post"]').isDisabled(),true);
 await page.locator('.qfe-watch-row').waitFor();
 assert.ok((await page.locator('.qfe-watch').textContent()).includes('原生日收盘'));
 await page.route('**/api/admin/etf-watchlist?**',route=>route.fulfill({status:403,contentType:'application/json',body:JSON.stringify({detail:'test permission revoked'})}));
 await page.getByRole('button',{name:'刷新数据',exact:true}).click();
 await page.waitForFunction(()=>!document.querySelector('.qfe-watch-row') &&
   [...document.querySelectorAll('.qfe-ohlc strong')].every(node=>node.textContent.trim()==='—') &&
   document.body.textContent.includes('无权'));
 assert.equal(requests.filter(path=>path.includes('/data-collections/')).length,0);
 assert.equal(errors.length,0,JSON.stringify(errors));
 await writeFile(new URL('./FORMAL_BROWSER_RESULT.json',import.meta.url),JSON.stringify({
  mode:'default app / real isolated formal storage / artificial inputs only',live_verified:false,deployed:false,
  findings:['formal summary count matches directory despite additional unselected physical representation',
   'default summary → market → formal calendar → legacy symbol profile → raw K-line works with old market HTTP and SQL reads blocked',
   'raw real response contains 10 bars; production pre/post remain disabled; no successful HTTP response fabricated',
   'shared native watchlist mutation/read still works; injected permission failure withdraws both saved quotes and formal graph'],
  requests,errors},null,2));
}finally{await browser.close();}
