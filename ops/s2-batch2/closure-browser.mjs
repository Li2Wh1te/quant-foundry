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
const findings=[], errors=[], requests=[], metrics=[], pending=[], faults=[];
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
  page.on('response', response=>{
    if(!new URL(response.url()).pathname.startsWith('/api/admin/market/')) return;
    const capture=(async()=>{
      const body=await response.body().catch(()=>null); // Cancelled superseded reads have no body.
      if(!body) return;
      const bytes=body.length;
      assert.ok(bytes<=4*1024*1024);
      if(response.status()>=400) {
        let code='UNKNOWN';
        try { const value=JSON.parse(body.toString()).detail?.code; if(/^[A-Z_]{1,64}$/.test(value)) code=value; } catch {}
        const path=new URL(response.url()).pathname;
        faults.push({route:path.endsWith('/bars')?'bars':path.endsWith('/series')?'series':'metadata',code});
      }
      metrics.push({bytes,status:response.status(),milliseconds:response.request().timing().responseEnd});
    })();
    capture.catch(()=>{}); // Observed again by Promise.all inside the guarded result path.
    pending.push(capture);
  });
  await page.addInitScript(token=>sessionStorage.setItem('quant-foundry.api-token',token),process.env.QF_S2_TEST_TOKEN);
  const button=name=>page.getByRole('button',{name,exact:true});
  const settled=()=>page.waitForFunction(()=>!document.querySelector('[aria-label="刷新数据"]')?.disabled &&
    /[1-9]\d* \/ 10,000 根/.test(document.querySelector('.qfe-history-actions')?.textContent||''));
  async function bars(action, expected) {
    const response=page.waitForResponse(response=>{
      const url=new URL(response.url());
      return url.pathname.endsWith('/bars') && Object.entries(expected).every(([key,value])=>url.searchParams.get(key)===value);
    });
    await action(); const actual=await response;
    assert.equal(actual.status(),200); const data=await actual.json();
    await settled(); return data;
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
  const factorResponse=page.waitForResponse(response=>{
    const url=new URL(response.url()); return url.pathname.endsWith('/factors')&&url.searchParams.get('series_key')==='isolated-etf-daily';
  });
  await page.getByRole('tab',{name:'复权因子',exact:true}).click();
  const factorResult=await factorResponse; assert.equal(factorResult.status(),200);
  const factors=await factorResult.json();
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
  setStage('resident-boundary');
  await page.goto(base+'/admin/data/etf-basics/'+encodeURIComponent(fixture.history_key)+'?tab=overview');
  await settled();
  await page.waitForFunction(()=>document.querySelector('.qfe-history-actions')?.textContent.includes('512 /'));
  let pages=1;
  while(pages<19) {
    const earlier=await bars(()=>button('加载更早历史').click(),{series_key:'etf-daily-local',adjustment:'none'});
    assert.ok(earlier.next_cursor); pages++;
    await page.waitForFunction(count=>document.querySelector('.qfe-history-actions')?.textContent.includes(`${count} /`),512*pages);
  }
  assert.equal(await button('加载更早历史').isDisabled(),true);
  await button('释放窗口并重读').waitFor();
  assert.equal(pages*512,9728); // One more fixed 512-item cursor page would exceed 10,000.
  const cdp=await page.context().newCDPSession(page);
  await cdp.send('Performance.enable'); const peak=await cdp.send('Performance.getMetrics');
  setStage('release-refresh-zoom');
  await button('释放窗口并重读').click();
  await page.waitForFunction(()=>document.querySelector('.qfe-history-actions')?.textContent.includes('512 /'));
  for(let i=0;i<3;i++) await bars(()=>button('刷新数据').click(),{series_key:'etf-daily-local',adjustment:'none'});
  const canvas=page.locator('.qfe-chart canvas').first();
  await canvas.hover(); for(let i=0;i<3;i++) { await page.mouse.wheel(0,-100); await page.mouse.wheel(0,100); }
  assert.ok(!new URL(page.url()).searchParams.has('cursor'));
  assert.deepEqual(await page.evaluate(()=>Object.keys(localStorage)),[]);
  assert.deepEqual(await page.evaluate(()=>Object.keys(sessionStorage)),['quant-foundry.api-token']);
  findings.push('real multi-decade formal history reaches the fixed-page resident boundary at 9,728/10,000 with continuation still present; next page disabled, release returns to 512; repeated refresh/zoom does not persist bars or cursors');
  await Promise.all(pending);
  assert.deepEqual(errors,[]);
  await writeFile(output,JSON.stringify({passed:true,synthetic:true,live_verified:false,deployed:false,findings,
    requests,metrics,resident_bars:9728,loaded_pages:pages,browser_metrics:peak.metrics,
    milliseconds:Date.now()-started,errors},null,2)+'\n');
} catch(error) {
  const fault=faults.at(-1);
  const code=error.name==='TimeoutError'?'timeout':error.name==='AssertionError'?'assertion':'runtime';
  await writeFile(output,JSON.stringify({passed:false,stage:stage+'-'+code+(fault?'-'+fault.route+'-'+fault.code:''),
    error:String(error),faults,errors,requests},null,2)+'\n');
  throw error;
} finally { await browser?.close(); }
