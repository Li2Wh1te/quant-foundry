/** Explicit artificial UI transport. Formal default-app proof is a separate test. */
import { createRequire } from 'node:module';
import { mkdir, writeFile } from 'node:fs/promises';
import assert from 'node:assert/strict';
import { responseFor } from '../s2-d11/mock-api.mjs';
const base = process.env.QF_D12_PREVIEW_URL || 'http://127.0.0.1:5173';
const require = createRequire(new URL('../../frontend/package.json', import.meta.url));
const { chromium } = require('playwright');
const browser = await chromium.launch({ executablePath: '/usr/bin/chromium', headless: true, args: ['--no-sandbox'] });
import { operations, summary } from './preview-api.mjs';
let mode='normal', release;
const findings=[], errors=[], requests=[];
const root=new URL('./screenshots/',import.meta.url);await mkdir(root,{recursive:true});
try {
  const page=await browser.newPage({viewport:{width:1440,height:900},timezoneId:'America/Los_Angeles'});
  page.on('pageerror',e=>errors.push(e.message));
  await page.route(url=>url.pathname.startsWith('/api/'),async route=>{
    const path=new URL(route.request().url()).pathname;requests.push(path);
    if(path.includes('/data-collections/')){await route.abort();return;}
    if(path==='/api/admin/market/overview'){
      if(mode==='hold')await new Promise(resolve=>{release=resolve;});
      if(mode==='network'){await route.abort('failed');return;}
      if(['quality','unknown','permission'].includes(mode)){
        await route.fulfill({status:mode==='permission'?403:mode==='quality'?409:500,contentType:'application/json',body:JSON.stringify({detail:{code:mode==='quality'?'DATA_RESTRICTED':'UNRECOGNIZED_PRIVATE_ERROR',message:'private SQL must not render'}})});return;
      }
      await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(summary)});return;
    }
    if(path==='/api/admin/overview'){
      await route.fulfill({status:mode==='operations-failed'?500:200,contentType:'application/json',body:JSON.stringify(operations)});return;
    }
    const[status,body]=responseFor(new URL(route.request().url()).pathname+new URL(route.request().url()).search,route.request().method());
    await route.fulfill({status,contentType:'application/json',body:body===null?'':JSON.stringify(body)});
  });
  await page.addInitScript(()=>sessionStorage.setItem('quant-foundry.api-token','artificial-d12-only'));
  await page.goto(base + '/admin');
  const asset=page.locator('.qfo-asset-row').first();
  await page.waitForFunction(()=>document.querySelector('.qfo-asset-row b')?.textContent==='7');
  assert.ok((await page.locator('.qfo-side-summary').textContent()).includes('业务截至：未知'));
  assert.ok((await page.locator('.qfo-side-summary').textContent()).includes('覆盖未核验'));
  assert.ok((await page.locator('.qfo-source-table').textContent()).includes('未配置'));
  assert.ok((await page.locator('.qfo-attention').textContent()).includes('失败'));
  findings.push('unconfigured source and failed update are separate from the readable artificial formal directory; dates and coverage remain unknown');
  for(const[width,height,file]of[[1440,900,'1440x900.png'],[1024,768,'1024x768.png'],[390,844,'390px.png']]){
    await page.setViewportSize({width,height});await asset.scrollIntoViewIfNeeded();
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    await page.screenshot({path:new URL(file,root).pathname});
  }
  findings.push('three viewport screenshots show no horizontal overflow and preserve the existing shell');
  await page.setViewportSize({width:1440,height:900});
  const refresh=page.getByRole('button',{name:'刷新状态',exact:true});
  mode='network';await refresh.click();
  await page.waitForFunction(()=>document.querySelector('.qfo-side-summary')?.textContent.includes('同条件旧摘要'));
  assert.equal(await asset.locator('b').textContent(),'7');
  findings.push('same-condition network failure marks only the directory stale, without removing operations');
  mode='quality';await refresh.click();
  await page.waitForFunction(()=>document.querySelector('.qfo-side-summary')?.textContent.includes('质量限制'));
  assert.equal(await asset.locator('b').textContent(),'—');
  assert.ok((await page.locator('.qfo-metric-band').textContent()).includes('1 次成功'));
  findings.push('quality refusal withdraws the old directory count immediately; operations remain independent');
  mode='unknown';await refresh.click();
  await page.waitForFunction(()=>document.querySelector('.qfo-side-summary')?.textContent.includes('暂不可用'));
  assert.equal(await asset.locator('b').textContent(),'—');
  assert.ok(!(await page.locator('body').textContent()).includes('private SQL'));
  findings.push('unknown errors clear formal values and render only safe local copy');
  mode='operations-failed';await refresh.click();
  await page.waitForFunction(()=>document.querySelector('.qfo-asset-row b')?.textContent==='7');
  await page.waitForFunction(()=>document.querySelector('.qfo-review-error')?.textContent.includes('运营指标'));
  assert.ok(!(await page.locator('.qfo-metric-band').textContent()).includes('1 次成功'));
  findings.push('operations failure does not close a valid independent formal summary');
  mode='normal';await refresh.focus();await page.keyboard.press('Enter');
  await page.waitForFunction(()=>document.querySelector('.qfo-metric-band')?.textContent.includes('1 次成功'));
  mode='permission';await refresh.click();
  await page.waitForFunction(()=>document.querySelector('.qfo-side-summary')?.textContent.includes('无权'));
  assert.equal(await asset.locator('b').textContent(),'—');
  assert.ok(!(await page.locator('.qfo-metric-band').textContent()).includes('1 次成功'));
  findings.push('permission invalidation withdraws all affected panels; keyboard refresh works');
  assert.equal(requests.filter(p=>p.includes('/data-collections/')).length,0);assert.equal(errors.length,0);
  await writeFile(new URL('./UI_RESULT.json',import.meta.url),JSON.stringify({mode:'artificial preparation preview only',actualAccess:false,findings,errors,requests},null,2));
  console.log(findings.join('\n'));
}finally{release?.();await browser.close();}
