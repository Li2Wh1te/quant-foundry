/* Clickable D15 acceptance: fixture transport and real isolated service are distinct modes. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require('playwright');
const root=path.resolve(__dirname,'../..'),base=process.env.FRONTEND_URL||'http://127.0.0.1:5173';
const output=process.env.QF_D15_EVIDENCE||'/tmp/qf-s3-d15-browser';fs.mkdirSync(output,{recursive:true});
const native=process.env.QF_D15_NATIVE==='1',manifest=native?JSON.parse(fs.readFileSync(process.env.QF_D15_MANIFEST,'utf8')):null;
const ids={strategy:manifest?.strategy_id||'10000000-0000-0000-0000-000000000001',revision:manifest?.revision_id||'20000000-0000-0000-0000-000000000001',account:manifest?.account_id||'30000000-0000-0000-0000-000000000001'};
const token=manifest?.token||'isolated-browser-token';
const config=()=>({api_schema:'qf.backtest.v2',strategy_revision_id:ids.revision,account_id:ids.account,initial_cash:'10000.000000000000001',currency:'CNY',start:'2026-01-26',end:'2026-02-06',frequency:'1d',universe:['510300.SH','510500.SH'],parameters:{fail:false,delay:0},execution_model:'bar_next_interval_v1',participation_rate:'0.10',slippage_bps:'0',risk_free_rate:'0',annualization_sessions:252,seed:0,result_sampling:'session_close'});
const caps={api_schema:'qf.backtest.v2',engine:{installed:true,frequencies:['1d','1m','5m','15m','30m','60m','tick'],execution_models:['bar_next_interval_v1','trade_tick_v1','quote_tick_v1'],result_kinds:['summary']},actual_data:[{binding:'isolated_daily',frequency:'1d',available:true,synthetic:true},{binding:'missing_minute',frequency:'1m',available:false},{binding:'missing_tick',frequency:'tick',available:false}],limits:{batch_runs:256,parameter_bytes:65536,parameter_depth:8,compare_runs:16}};
const rows=[],batches=new Map(),keys=new Map(),requests=[],failures=[],steps=[];let mode='daily',drop=false,slow=0,preflightCalls=0,deny=0,delayedId='';
const run=value=>({run_id:crypto.randomUUID(),engine_kind:'rust',api_schema:'qf.backtest.v2',status:'queued',cancel_requested:false,partial:false,normalized_config:structuredClone(value),account_version:1,created_at:'2026-10-11T00:00:00Z'});
const json=(route,value,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(value)});
async function select(page,label,option){await page.getByRole('combobox',{name:label,exact:true}).click();await page.getByRole('option',{name:option,exact:true}).click();}
async function date(page,label,value){await page.getByRole('button',{name:label,exact:true}).click();const calendar=page.getByRole('dialog',{name:label+'日历',exact:true});await select(calendar,'年份',value.slice(0,4)+'年');await select(calendar,'月份',Number(value.slice(5,7))+'月');await calendar.getByRole('button',{name:value,exact:true}).click();}
async function setup(page,options={}){
 await page.goto(`${base}/admin/strategies/${ids.strategy}/backtests?create=1`);
 await page.getByRole('dialog',{name:'创建 Rust 回测',exact:true}).waitFor().catch(async e=>{await page.screenshot({path:path.join(output,'failure.png')});console.error(await page.locator('body').innerText());throw e});
 await page.getByRole('combobox',{name:'回测账户',exact:true}).click();
 await page.getByRole('option',{name:options.accountName||'隔离账户 · 配置 v1',exact:true}).click();
 await page.getByLabel('初始资金（CNY）',{exact:true}).fill(config().initial_cash);
 await date(page,'开始日期','2026-01-26');await date(page,'结束日期','2026-02-06');
 await page.getByLabel('标的代码集合',{exact:true}).fill('510300.SH\n510500.SH');
 await page.getByLabel('策略参数 JSON',{exact:true}).fill(JSON.stringify(options.parameters||{fail:false,delay:0}));
}
async function check(page){await page.getByRole('button',{name:'预检当前配置',exact:true}).click();await page.getByText('当前配置预检通过。提交与执行时服务还会复核数据、权限及隔离条件。',{exact:true}).waitFor();}
async function submit(page){const previous=new URL(page.url()).searchParams.get('run');await page.getByRole('button',{name:'提交运行',exact:true}).dblclick();await page.waitForURL(url=>url.searchParams.has('run')&&url.searchParams.get('run')!==previous);await page.getByRole('dialog').waitFor({state:'hidden'});return new URL(page.url()).searchParams.get('run');}
async function waitState(page,id,status){await page.waitForFunction(({id,status})=>{const current=document.querySelector('.qfb-detail-head code');return current?.textContent===id&&document.querySelector('.qfb-card header [role=status]')?.textContent===status},{id,status},{timeout:60000});}
(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{}),args:['--no-sandbox']});
 const context=await browser.newContext({viewport:{width:1440,height:1000}});await context.addInitScript(value=>sessionStorage.setItem('quant-foundry.api-token',value),token);
 const page=await context.newPage();page.on('pageerror',e=>failures.push(e.message));
 page.on('request',r=>{if(new URL(r.url()).pathname.startsWith('/api/'))requests.push({method:r.method(),path:new URL(r.url()).pathname,key:r.headers()['idempotency-key']||null})});
 if(!native)await context.route(url=>url.pathname.startsWith('/api/'),async route=>{
  const req=route.request(),url=new URL(req.url()),p=url.pathname,m=req.method(),body=req.postDataJSON();
  if(p==='/api/auth/verify')return route.fulfill({status:204});if(p==='/api/system/version')return json(route,{version:fs.readFileSync(path.join(root,'VERSION'),'utf8').trim()});
  if(deny&&(p.startsWith('/api/admin/backtests')||p.endsWith('/backtests')))return json(route,{detail:{code:'DATA_RESTRICTED',message:'must not reveal /private or postgresql://secret'}},deny);
  if(p==='/api/admin/backtests/capabilities')return json(route,caps);
  if(p==='/api/admin/strategies')return json(route,[{id:ids.strategy,name:'隔离策略',state:'active'}]);
  if(p.startsWith('/api/admin/strategies/')&&p.endsWith('/backtests'))return json(route,{published_revisions:[{id:ids.revision,revision_number:1,alias:'S3',parameter_schema:{type:'object',properties:{fail:{type:'boolean'},delay:{type:'number'}}},default_parameters:{fail:false,delay:0}}],runs:{items:rows.slice().reverse(),has_more:false,next_cursor:null}});
  if(p==='/api/admin/backtest-account-profiles/page')return json(route,{items:[{id:ids.account,name:'隔离账户',status:'active',version:1,fee_schedule:{}}],total:1});
  if(p==='/api/admin/backtests/preflight'){
   preflightCalls++; const value=structuredClone(body),current=mode,delay=slow;slow=0;
   if(delay)await new Promise(r=>setTimeout(r,delay));
   return json(route,{errors:current!=='daily'||value.frequency!=='1d'?[{code:current==='restricted'?'DATA_RESTRICTED':'CAPABILITY_UNAVAILABLE'}]:[],warnings:['current_revision_not_historical_snapshot'],normalized_config:{...value,reference_calendar:'synthetic-ui-calendar'}});
  }
  if((p==='/api/admin/backtests'||p==='/api/admin/backtests/batches')&&m==='POST'){
   const key=req.headers()['idempotency-key'];if(keys.has(key))return json(route,keys.get(key),202);
   if(p.endsWith('/batches')){const bid=crypto.randomUUID(),items=body.configs.map(v=>({...run(v),status:v.parameters.delay?'queued':v.parameters.fail?'failed':'succeeded',partial:!!v.parameters.fail,error:v.parameters.fail?{code:'STRATEGY_ERROR'}:null,batch_id:bid}));rows.push(...items);const value={batch_id:bid,items,has_more:false,next_cursor:null};keys.set(key,value);batches.set(bid,value);return json(route,value,202)}
   const value=run(body);rows.push(value);keys.set(key,value);if(drop){drop=false;return route.abort('failed')}return json(route,value,202);
  }
  if(p==='/api/admin/backtests'&&m==='GET')return json(route,{items:rows.slice().reverse(),has_more:false,next_cursor:null});
  if(p.includes('/results/summary'))return json(route,{schema_id:'qf.results.v1',run_id:p.split('/')[4],engine_kind:'rust',partial:false,performance:{initial_cash:config().initial_cash}});
  if(p.includes('/batches/')){const bid=p.split('/')[5],value=batches.get(bid);if(p.endsWith('/retry-failed')){const ids=[];for(const row of value.items)if(row.status==='failed'){const fresh={...run(row.normalized_config),status:'failed',partial:true,error:{code:'STRATEGY_ERROR'},retry_of:row.run_id,batch_id:bid};ids.push(fresh.run_id);rows.push(fresh)}return json(route,{batch_id:bid,run_ids:ids},202)}if(p.endsWith('/cancel'))for(const row of value.items)if(!['succeeded','failed','cancelled'].includes(row.status)){row.status='cancelled';row.partial=true}return json(route,value)}
  const id=p.split('/')[4],item=rows.find(r=>r.run_id===id);
  if(item){if(id===delayedId&&m==='GET'){const saved=structuredClone(item);await new Promise(r=>setTimeout(r,900));return json(route,saved)}if(p.endsWith('/cancel')){if(!['succeeded','failed','cancelled'].includes(item.status)){item.status='cancelled';item.partial=true;item.cancel_requested=true}}return json(route,item)}
  return json(route,{detail:{code:'CAPABILITY_UNAVAILABLE'}},503);
 });
 try{
  let accountName;
  if(native){const r=await context.request.get(manifest.url+'/api/admin/backtest-account-profiles/page?status=active&limit=20&offset=0',{headers:{Authorization:'Bearer '+token}});assert.equal(r.status(),200);accountName=(await r.json()).items.find(a=>a.id===ids.account).name+' · 配置 v1';}
  await setup(page,{accountName,parameters:{fail:false,delay:native?10:0}});await check(page);await page.screenshot({path:path.join(output,'create-preflight-1440.png')});
  const created=await submit(page);if(native)await waitState(page,created,'运行中');await page.getByRole('button',{name:'取消运行',exact:true}).waitFor();await page.getByRole('button',{name:'取消运行',exact:true}).dblclick();await waitState(page,created,'已取消');steps.push('create → preflight → queue/run → cancel; duplicate clicks');
  await page.getByRole('button',{name:'重新运行',exact:true}).click();await page.getByRole('dialog',{name:'按原配置重新预检'}).waitFor();assert.equal(await page.getByLabel('初始资金（CNY）',{exact:true}).inputValue(),config().initial_cash);await check(page);const rerun=await submit(page);assert.notEqual(created,rerun);
  if(native)await waitState(page,rerun,'已完成');else{rows.find(r=>r.run_id===rerun).status='succeeded';await page.getByRole('button',{name:'刷新',exact:true}).click();await waitState(page,rerun,'已完成')}
  if(native){
   const response=await context.request.get(manifest.url+`/api/admin/backtests/${rerun}`,{headers:{Authorization:'Bearer '+token}});assert.equal(response.status(),200);const status=await response.json();assert.ok(status.progress_completed>0);assert.equal(status.progress_completed,status.progress.completed_events);assert.ok(status.progress.completed_sessions>0);assert.equal(typeof status.progress.now_ns,'string');await page.getByText(/已处理 .* 条行情事件，完成 .* 个交易会话/).waitFor();steps.push('real Rust event/session counters appear in HTTP and browser without an invented total');
  }
  steps.push('terminal rerun creates a new run from exact decimal config');
  await page.screenshot({path:path.join(output,'rerun-succeeded-1440.png')});
  await page.goBack();await page.waitForURL(url=>url.searchParams.get('run')===created);await waitState(page,created,'已取消');await page.goForward();await waitState(page,rerun,'已完成');steps.push('browser back/forward restore selected run');
  await setup(page,{accountName});await select(page,'运行方式','参数批次');await page.getByLabel('参数批次 JSON',{exact:true}).fill('[{"fail":false,"delay":0},{"fail":true,"delay":0}]');await check(page);await page.getByRole('button',{name:'创建参数批次',exact:true}).dblclick();await page.waitForURL(url=>url.searchParams.has('batch'));const bid=new URL(page.url()).searchParams.get('batch');await page.getByRole('button',{name:'仅重跑失败项',exact:true}).waitFor();await page.waitForFunction(()=>!document.querySelector('button:disabled')?.textContent?.includes('仅重跑失败项')).catch(()=>{});
  await page.getByRole('button',{name:'仅重跑失败项',exact:true}).click({timeout:60000});await page.waitForURL(url=>url.searchParams.has('run')&&url.searchParams.get('batch')===bid);await page.screenshot({path:path.join(output,'batch-partial-failure-1440.png')});steps.push('parameter batch partial failure; completed item retained; retry failed only');
  if(native){await waitState(page,new URL(page.url()).searchParams.get('run'),'失败');await page.getByText('策略回调 initialize · 第 7 行',{exact:true}).waitFor();await page.screenshot({path:path.join(output,'batch-strategy-error-location.png')});}
  await setup(page,{accountName});await select(page,'运行方式','参数批次');await page.getByLabel('参数批次 JSON',{exact:true}).fill(JSON.stringify(Array.from({length:4},()=>({fail:false,delay:8}))));await check(page);await page.getByRole('button',{name:'创建参数批次',exact:true}).click();await page.waitForURL(url=>url.searchParams.has('batch'));
  if(native)await page.locator('.qf-run-batch-rows').getByText('运行中',{exact:true}).first().waitFor({timeout:60000});
  await page.getByRole('button',{name:'取消未终态项',exact:true}).dblclick();await page.waitForFunction(()=>document.querySelectorAll('.qf-run-batch-rows button').length===4&&[...document.querySelectorAll('.qf-run-batch-rows button span')].every(n=>n.textContent==='已取消'),null,{timeout:60000});steps.push('cancel running/queued batch items; terminal values retained');
  if(!native){
   await setup(page,{accountName});slow=900;await page.getByRole('button',{name:'预检当前配置'}).click();await page.waitForTimeout(100);await page.getByLabel('初始资金（CNY）',{exact:true}).fill('20000');await page.waitForTimeout(1100);assert.equal(await page.getByRole('button',{name:'提交运行',exact:true}).isDisabled(),true);steps.push('late preflight discarded after configuration edit');
   await setup(page,{accountName});slow=900;await page.getByRole('button',{name:'预检当前配置'}).click();await page.getByRole('button',{name:'关闭创建回测',exact:true}).click();await page.waitForTimeout(1000);assert.equal(await page.getByRole('dialog').count(),0);steps.push('preflight abort on drawer unmount');
   await page.goto(`${base}/admin/strategies/${ids.strategy}/backtests?run=${rerun}`);await waitState(page,rerun,'已完成');delayedId=created;await page.locator('.qfb-run-row button').filter({hasText:created}).click();await page.locator('.qfb-run-row button').filter({hasText:rerun}).click();await page.waitForTimeout(1100);await waitState(page,rerun,'已完成');delayedId='';steps.push('late selected-run response cannot replace the newer selection');
  }
  // The native case forwards to the actual server, then drops its accepted
  // response. This tests uncertain admission, without substituting acceptance.
  if(native)await context.route(url=>url.pathname==='/api/admin/backtests',async route=>{if(route.request().method()==='POST'){const accepted=await route.fetch();assert.equal(accepted.status(),202);await route.abort('failed');}else await route.continue()});else drop=true;
  await setup(page,{accountName});await check(page);await page.getByRole('button',{name:'提交运行',exact:true}).click();await page.getByText(/提交失败|Failed to fetch/).waitFor();await page.getByRole('button',{name:'关闭创建回测',exact:true}).click();await page.getByRole('button',{name:'核对原提交',exact:true}).waitFor();
  const beforeLoss=native?(await (await context.request.get(manifest.url+'/api/test/d15/evidence',{headers:{Authorization:'Bearer '+token}})).json()).runs.length:rows.length;
  // Remove the one-shot native response interception by its saved URL matcher.
  if(native)await context.unrouteAll({behavior:'wait'});
  await page.reload();await page.getByRole('button',{name:'核对原提交',exact:true}).click();await page.waitForURL(url=>url.searchParams.has('run'));
  const afterLoss=native?(await (await context.request.get(manifest.url+'/api/test/d15/evidence',{headers:{Authorization:'Bearer '+token}})).json()).runs.length:rows.length;assert.equal(afterLoss,beforeLoss);steps.push('lost accepted response + close/reload/retry reuses key, one run');
  if(native){
   await setup(page,{accountName,parameters:{fail:false,delay:5}});await check(page);const changed=await submit(page);await waitState(page,changed,'运行中');await context.request.post(manifest.url+'/api/test/d15/control',{headers:{Authorization:'Bearer '+token},data:{mode:'restrict'}});await waitState(page,changed,'失败');await page.getByRole('alert').filter({hasText:'DATA_CHANGED'}).waitFor();await page.screenshot({path:path.join(output,'data-changed-terminal.png')});await context.request.post(manifest.url+'/api/test/d15/control',{headers:{Authorization:'Bearer '+token},data:{mode:'clear'}});steps.push('actual CurrentStore quality change terminates active run as DATA_CHANGED');
  }
  for(const f of ['1m','tick']){await setup(page,{accountName});await select(page,'频率',f);await page.getByRole('button',{name:'预检当前配置'}).click();await page.getByRole('alert').filter({hasText:'CAPABILITY_UNAVAILABLE'}).waitFor();assert.equal(await page.getByRole('button',{name:'提交运行',exact:true}).isDisabled(),true);await page.screenshot({path:path.join(output,`missing-${f}-1440.png`)});}steps.push('minute/tick absent: preflight denies; no interpolation or fake data');
  if(native){
   await context.request.post(manifest.url+'/api/test/d15/control',{headers:{Authorization:'Bearer '+token},data:{mode:'restrict'}});
  }else mode='restricted';
  await setup(page,{accountName});await page.getByRole('button',{name:'预检当前配置'}).click();await page.getByRole('alert').filter({hasText:/DATA_RESTRICTED|DATA_CHANGED/}).waitFor();await page.screenshot({path:path.join(output,'r01-gate-1440.png')});steps.push('R01 CurrentStore gate rejected');
  if(native)await context.request.post(manifest.url+'/api/test/d15/control',{headers:{Authorization:'Bearer '+token},data:{mode:'clear'}});else mode='daily';
  for(const width of [1024,390]){await page.setViewportSize({width,height:width===390?844:1000});await page.screenshot({path:path.join(output,`preflight-${width}.png`)});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);const dialog=page.getByRole('dialog',{name:'创建 Rust 回测'});await dialog.locator('[data-form-title]').focus();await page.keyboard.press('Tab');assert.ok(await dialog.evaluate(node=>node.contains(document.activeElement)));await page.keyboard.press('Shift+Tab');assert.ok(await dialog.evaluate(node=>node.contains(document.activeElement)));}steps.push('responsive 1440/1024/390, keyboard focus contained');
  await page.getByRole('button',{name:'关闭创建回测',exact:true}).click();
  if(native){
   const headers={Authorization:'Bearer '+token};const get=async p=>{const r=await context.request.get(manifest.url+p,{headers});assert.equal(r.status(),200);return r.json()};
   const evidence=await get('/api/test/d15/evidence');assert.ok(evidence.started_runs.includes(rerun));assert.equal(evidence.runs.find(r=>r.run_id===rerun).status,'succeeded');assert.equal(evidence.runs.find(r=>r.run_id===rerun).config.initial_cash,config().initial_cash);
   const batchRows=evidence.runs.filter(r=>r.batch_id===bid);assert.equal(batchRows.filter(r=>r.status==='succeeded').length,1);assert.equal(batchRows.filter(r=>r.retry_of).length,1);
   const orders=await get(`/api/admin/backtests/${rerun}/results/orders?limit=100`),trades=await get(`/api/admin/backtests/${rerun}/results/trades?limit=100`),logs=await get(`/api/admin/backtests/${rerun}/results/logs?limit=100`);assert.ok(logs.items.some(l=>l.message.includes('Rejected order: INSUFFICIENT_SELLABLE')));assert.ok(trades.items.length>0);steps.push('real rejected order coexists with successful run and actual trades');
   const legacy=await get(`/api/admin/backtests/${manifest.legacy_run_id}/results/summary`);assert.equal(legacy.summary.value,'123456789.123456789012345678');assert.equal(legacy.summary.time_ns,'1767225600000000123');assert.equal(legacy.engine_kind,'legacy_python');
   fs.writeFileSync(path.join(output,'native-service.json'),JSON.stringify({...evidence,orders,trades,logs,legacy},null,2));
   await require('./s3-strategy-ui.e2e.cjs')({page,context,manifest,token,base,output,steps});
  }
  await page.setViewportSize({width:1440,height:1000});await page.goto(`${base}/admin/strategies/${ids.strategy}/backtests?run=${rerun}`);await waitState(page,rerun,'已完成');
  if(native){
   await page.goto(`${base}/admin/strategies/${ids.strategy}/backtests?run=${crypto.randomUUID()}`);
   await page.getByRole('alert').filter({hasText:'数据、策略、账户或运行不可读取'}).waitFor();assert.equal(await page.locator('.qfb-detail-head').count(),0);await page.screenshot({path:path.join(output,'unavailable-run-404.png')});steps.push('actual D13 unavailable/unowned run is concealed as 404; no selected contents');
  }else{
   deny=403;await page.getByRole('button',{name:'刷新',exact:true}).click();await page.getByRole('alert').filter({hasText:'没有回测操作权限'}).waitFor();assert.equal(await page.locator('.qfb-run-row').count(),0);assert.equal(await page.locator('.qfb-detail-head').count(),0);assert.equal((await page.locator('body').innerText()).includes('postgresql://secret'),false);await page.screenshot({path:path.join(output,'permission-403.png')});steps.push('403 revokes cached history, selected run and form');
  }
  if(!native)deny=0;await page.goto(`${base}/admin/strategies/${ids.strategy}/backtests?run=${rerun}`);await waitState(page,rerun,'已完成');
  if(native)await page.evaluate(()=>sessionStorage.setItem('quant-foundry.api-token','invalid-private-test-token'));else deny=401;
  await page.getByRole('button',{name:'刷新',exact:true}).click();await page.waitForURL(url=>url.pathname==='/login');steps.push('401 expires session and redirects to existing login');
  assert.deepEqual(failures,[]);assert.equal(requests.some(r=>r.method==='POST'&&r.path.startsWith('/api/admin/backtest-runs')),false);
  fs.writeFileSync(path.join(output,'acceptance.json'),JSON.stringify({mode:native?'real_isolated_service':'interface_substitute',browser:browser.version(),production_executed:false,steps,requests,errors:failures},null,2));console.log(JSON.stringify({mode:native?'real_isolated_service':'interface_substitute',steps,output},null,2));
 }catch(error){await page.screenshot({path:path.join(output,'failure.png')});fs.writeFileSync(path.join(output,'failure.json'),JSON.stringify({error:String(error),pageErrors:failures,url:page.url(),steps,requests},null,2));throw error;}finally{await context.close();await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1});
