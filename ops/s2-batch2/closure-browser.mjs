/** Recovered D13 gaps. All successes come from the ordinary image pair. */
import { createRequire } from 'node:module';
import { mkdir, writeFile } from 'node:fs/promises';
import { writeFileSync, renameSync } from 'node:fs';
import { dirname, join } from 'node:path';
import assert from 'node:assert/strict';
const require = createRequire(new URL('../../frontend/package.json', import.meta.url));
const { chromium } = require('playwright');
const base = process.env.QF_S2_TEST_URL, output = process.env.QF_S2_BROWSER_OUTPUT;
assert.match(base, /^http:\/\/127\.0\.0\.1:\d+$/);
assert.equal(process.env.QF_ENVIRONMENT, 'test');
const fixture = JSON.parse(process.env.QF_S2_FIXTURE);
let browser;
const findings=[], errors=[], requests=[], metrics=[], pending=[], faults=[], verification=[];
const started=Date.now();
let stage;
function setStage(value) {
  stage=value;
  writeFileSync(output+'.pending',JSON.stringify({passed:false,stage}));
  renameSync(output+'.pending',output);
}
setStage('launch-browser');
function canonical(value) { return value.includes('.') ? value.replace(/0+$/, '').replace(/\.$/, '') : value; }
function oracle(value, numerator, denominator) {
  const [whole, fraction=''] = value.split('.');
  const scaled=BigInt(whole+fraction.padEnd(12,'0'))*BigInt(numerator)/BigInt(denominator);
  const digits=scaled.toString().padStart(13,'0');
  return canonical(digits.slice(0,-12)+'.'+digits.slice(-12));
}
try {
  browser=await chromium.launch({executablePath:process.env.QF_BROWSER_PATH || '/usr/bin/chromium',headless:true,args:['--no-sandbox']});
  setStage('open-default-detail');
  const page=await browser.newPage({viewport:{width:1440,height:900},timezoneId:'America/Los_Angeles'});
  page.setDefaultTimeout(35000);
  page.on('pageerror', error=>errors.push(error.message));
  page.on('request', request=>{
    const url=new URL(request.url());
    if(!url.pathname.startsWith('/api/')) return;
    assert.equal(request.method(),'GET');
    if(url.searchParams.has('cursor')) url.searchParams.set('cursor','omitted');
    requests.push(url.pathname+url.search);
  });
  const finished=new WeakSet(), failed=new WeakSet(), completions=new Map();
  function finish(request, successful) {
    (successful?finished:failed).add(request);
    for(const resolve of completions.get(request)||[]) resolve(successful);
    completions.delete(request);
  }
  page.on('requestfinished',request=>finish(request,true));
  page.on('requestfailed',request=>finish(request,false));
  page.on('close',()=>{ for(const waiting of completions.values()) for(const resolve of waiting) resolve(false); completions.clear(); });
  function completed(response) {
    const request=response.request();
    if(failed.has(request)||request.failure()) return Promise.resolve(false);
    if(finished.has(request)) return Promise.resolve(true);
    return new Promise(resolve=>{ const waiting=completions.get(request)||[];waiting.push(resolve);completions.set(request,waiting); });
  }
  page.on('response', response=>{
    if(!new URL(response.url()).pathname.startsWith('/api/admin/market/')) return;
    const capture=(async()=>{
      if(!await completed(response)) return; // Superseded reads are cancelled by the real client.
      // Network transfer accounting does not depend on Chromium retaining a
      // second inspector copy of a body already consumed by the application.
      const sizes=await response.request().sizes();
      assert.ok(sizes.responseBodySize>=0&&sizes.responseBodySize<=4*1024*1024);
      assert.ok([undefined,'identity'].includes(response.headers()['content-encoding']));
      if(response.status()>=400) {
        const path=new URL(response.url()).pathname;
        faults.push({route:path.endsWith('/bars')?'bars':path.endsWith('/series')?'series':'metadata',code:'HTTP_'+response.status()});
      }
      metrics.push({bytes:sizes.responseBodySize,status:response.status(),milliseconds:response.request().timing().responseEnd});
    })();
    capture.catch(()=>{}); // Observed again by Promise.all inside the guarded result path.
    pending.push(capture);
  });
  async function readActual(response) {
    assert.equal(response.status(),200);
    // An independent real GET of exactly the observed URL verifies the shown
    // values and repeatable signed cursor. No route/provider/body is replaced.
    // Chromium's best-effort inspector body cache is not a market contract.
    const start=Date.now();
    const actual=await page.request.get(response.url(),{headers:{Authorization:'Bearer '+process.env.QF_S2_TEST_TOKEN},timeout:35000});
    try {
      assert.equal(actual.status(),200);
      const bytes=await actual.body(); assert.ok(bytes.length<=4*1024*1024);
      const url=new URL(response.url());if(url.searchParams.has('cursor')) url.searchParams.set('cursor','omitted');
      verification.push({url:url.pathname+url.search,status:actual.status(),bytes:bytes.length,milliseconds:Date.now()-start});
      return JSON.parse(bytes.toString());
    } finally { await actual.dispose(); }
  }
  await page.addInitScript(token=>sessionStorage.setItem('quant-foundry.api-token',token),process.env.QF_S2_TEST_TOKEN);
  const button=name=>page.getByRole('button',{name,exact:true});
  const settled=()=>page.waitForFunction(()=>!document.querySelector('[aria-label="刷新数据"]')?.disabled &&
    /[1-9]\d* \/ 10,000 根/.test(document.querySelector('.qfe-history-actions')?.textContent||''));
  async function bars(action, expected, continuation=false) {
    const phase=stage;
    const instrument=decodeURIComponent(new URL(page.url()).pathname.split('/').at(-1));
    const response=page.waitForResponse(async response=>{
      const url=new URL(response.url());
      return decodeURIComponent(url.pathname)===`/api/admin/market/instruments/${instrument}/bars` &&
        (!continuation || url.searchParams.has('cursor')) && Object.entries(expected).every(([key,value])=>url.searchParams.get(key)===value) && await completed(response);
    });
    response.catch(()=>{}); // Keep an action failure in the guarded report path.
    setStage(phase+'-request');
    await action(); const actual=await response;
    setStage(phase+'-decode');
    const data=await readActual(actual);
    setStage(phase+'-render');
    await settled(); setStage(phase); return data;
  }
  await page.goto(base+'/admin/data/etf-basics/'+encodeURIComponent(fixture.instrument_key)+'?tab=overview');
  await settled();
  setStage('production-basis-restriction');
  await page.getByRole('combobox',{name:'价格序列',exact:true}).waitFor();
  assert.equal(await page.locator('#qfb-price-basis option[value="pre"]').isDisabled(),true);
  setStage('fixed-window-controls');
  await button('范围').click();
  await page.getByLabel('开始日期',{exact:true}).fill('2025-12-29');
  await page.getByLabel('结束日期',{exact:true}).fill('2026-01-05');
  setStage('fixed-window-read');
  await bars(()=>button('应用').click(),{series_key:'etf-daily-local',adjustment:'none',end:'2026-01-05'});
  setStage('select-declared-series');
  const raw=await bars(()=>page.getByRole('combobox',{name:'价格序列',exact:true}).selectOption('isolated-etf-daily'),{series_key:'isolated-etf-daily',adjustment:'none'});
  await page.waitForFunction(()=>!document.querySelector('#qfb-price-basis option[value="pre"]')?.disabled);
  setStage('pre-post-display');
  const pre=await bars(()=>page.locator('#qfb-price-basis').selectOption('pre'),{series_key:'isolated-etf-daily',adjustment:'pre'});
  assert.deepEqual(pre.basis.anchor,{date:'2026-01-05',factor:'2'});
  assert.equal(canonical(pre.items[0].open),oracle(raw.items[0].open,1,2));
  await button('精确值表').click();
  assert.deepEqual(await page.locator('.qfe-exact-values tbody tr td:nth-child(2)').allTextContents(),pre.items.map(row=>row.open));
  const post=await bars(()=>page.locator('#qfb-price-basis').selectOption('post'),{series_key:'isolated-etf-daily',adjustment:'post'});
  assert.deepEqual(post.basis.anchor,{date:'2024-01-01',factor:'1'});
  assert.equal(canonical(post.items.at(-1).open),oracle(raw.items.at(-1).open,2,1));
  assert.deepEqual(post.units,raw.units);
  await page.waitForFunction(value=>document.querySelector('.qfe-exact-values tbody tr:last-child td:nth-child(2)')?.textContent===value,post.items.at(-1).open);
  setStage('selected-factor-table');
  const side=button('切换资料侧栏');
  if(await side.getAttribute('aria-expanded')!=='true') await side.click();
  const factorResponse=page.waitForResponse(async response=>{
    const url=new URL(response.url()); return url.pathname.endsWith('/factors')&&url.searchParams.get('series_key')==='isolated-etf-daily'&&await completed(response);
  });
  factorResponse.catch(()=>{});
  await page.getByRole('tab',{name:'复权因子',exact:true}).click();
  const factorResult=await factorResponse; assert.equal(factorResult.status(),200);
  const factors=await readActual(factorResult);
  assert.deepEqual(factors.items.map(row=>row.trade_date),raw.items.map(row=>row.last_trade_date));
  assert.deepEqual(factors.items.map(row=>canonical(row.factor)),['1','1','1','2','2','2']);
  await page.locator('.qfb-factors tbody tr').first().waitFor();
  assert.deepEqual(await page.locator('.qfb-factors tbody tr td:nth-child(2)').allTextContents(),factors.items.map(row=>row.factor));
  findings.push('ordinary default images → explicitly selected server-declared test series → real pre/post native arithmetic and exact displayed values; selected factor table uses the same series');
  setStage('three-viewports');
  const screenshots=join(dirname(output),'closure-screenshots'); await mkdir(screenshots,{recursive:true});
  for(const [width,height] of [[1440,900],[1024,768],[390,844]]) {
    await page.setViewportSize({width,height});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    await page.screenshot({path:join(screenshots,`${width}x${height}.png`)});
  }
  await page.setViewportSize({width:1440,height:900});
  setStage('explicit-series-reset');
  await bars(()=>page.getByRole('combobox',{name:'价格序列',exact:true}).selectOption('etf-daily-local'),{series_key:'etf-daily-local',adjustment:'none'});
  assert.equal(await page.locator('#qfb-price-basis').inputValue(),'none');
  assert.equal(await page.locator('#qfb-price-basis option[value="pre"]').isDisabled(),true);
  findings.push('explicit series change resets basis/window; production E69 remains restricted, without implicit source/basis fallback');
  setStage('history-navigation');
  await page.goto(base+'/admin/data/etf-basics/'+encodeURIComponent(fixture.history_key)+'?tab=overview');
  setStage('history-initial-window');
  await settled();
  await page.waitForFunction(()=>document.querySelector('.qfe-history-actions')?.textContent.includes('512 /'));
  let pages=1;
  while(pages<19) {
    setStage('resident-page-load');
    const earlier=await bars(()=>button('加载更早历史').click(),{series_key:'etf-daily-local',adjustment:'none'},true);
    assert.ok(earlier.next_cursor); pages++;
    await page.waitForFunction(count=>document.querySelector('.qfe-history-actions')?.textContent.includes(`${count} /`),512*pages);
  }
  setStage('resident-limit-controls');
  assert.equal(await button('加载更早历史').isDisabled(),true);
  await button('释放窗口并重读').waitFor();
  assert.equal(pages*512,9728); // One more fixed 512-item cursor page would exceed 10,000.
  setStage('resident-metrics');
  const cdp=await page.context().newCDPSession(page);
  await cdp.send('Performance.enable'); const peak=await cdp.send('Performance.getMetrics');
  setStage('release-refresh-zoom');
  await button('释放窗口并重读').click();
  await page.waitForFunction(()=>document.querySelector('.qfe-history-actions')?.textContent.includes('512 /'));
  for(let i=0;i<3;i++) await bars(()=>button('刷新数据').click(),{series_key:'etf-daily-local',adjustment:'none'});
  await button('缩放').click();
  for(let i=0;i<3;i++) {
    for(const name of ['放大图表','缩小图表']) {
      const prior=await page.locator('.qfe-timeline > span').first().textContent();
      await button(name).click();
      await page.waitForFunction(value=>document.querySelector('.qfe-timeline > span')?.textContent!==value,prior);
    }
  }
  assert.ok(!new URL(page.url()).searchParams.has('cursor'));
  assert.deepEqual(await page.evaluate(()=>Object.keys(localStorage)),[]);
  assert.deepEqual(await page.evaluate(()=>Object.keys(sessionStorage)),['quant-foundry.api-token']);
  findings.push('real multi-decade formal history reaches the fixed-page resident boundary at 9,728/10,000 with continuation still present; next page disabled, release returns to 512; repeated refresh/zoom does not persist bars or cursors');
  await Promise.all(pending);
  assert.deepEqual(errors,[]);
  await writeFile(output,JSON.stringify({passed:true,synthetic:true,live_verified:false,deployed:false,findings,
    requests,metrics,verification,resident_bars:9728,loaded_pages:pages,browser_metrics:peak.metrics,
    milliseconds:Date.now()-started,errors},null,2)+'\n');
} catch(error) {
  const fault=faults.at(-1);
  const message=String(error);
  const diagnostic=message.includes('getResponseBody')||message.includes('No resource with given identifier')||message.includes('No data found for resource')?'response-body-unavailable':
    message.includes('Execution context was destroyed')||message.includes('Cannot find context with specified id')?'navigation-context-replaced':
    message.includes('Target page, context or browser has been closed')?'browser-target-closed':
    message.includes('strict mode violation')?'locator-ambiguous':
    message.includes('net::ERR_')?'browser-network':'guarded-failure';
  const code=['TimeoutError','AssertionError','TypeError','SyntaxError','ReferenceError','Error'].includes(error.name)?error.name:'runtime';
  await writeFile(output,JSON.stringify({passed:false,stage:stage+'-'+code+'-'+diagnostic+(fault?'-'+fault.route+'-'+fault.code:''),
    error:String(error),faults,errors,requests,metrics,verification},null,2)+'\n');
  throw error;
} finally { await browser?.close(); }
