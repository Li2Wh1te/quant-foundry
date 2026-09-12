// Run with PLAYWRIGHT_MODULE pointing to the installed Playwright module.
// Isolated browser context: all business APIs below are test fixtures only.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.connectOverCDP(process.env.CDP_URL||'http://127.0.0.1:9421');
 const context=await browser.newContext({viewport:{width:1440,height:960}});
 await context.addInitScript(()=>sessionStorage.setItem('quant-foundry.api-token','local-fixture-only'));
 const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
 const source='def run(context, parameters):\n    return {"mode": "hold"}\n';
 const make=(id)=>({id,name:'策略 '+id,description:null,state:'active',current_revision_id:null,version:1,created_at:'2026-09-12T08:00:00Z',updated_at:'2026-09-12T08:00:00Z',draft_changed_since_revision:true,draft:{source_code:source,source_hash:'abcdef',parameter_schema:{type:'object'},default_parameters:{lookback:60},version:1,created_at:'2026-09-12T08:00:00Z',updated_at:'2026-09-12T08:00:00Z'},current_revision:null});
 const rows={one:make('one'),two:make('two')};let conflict=false,delaySave=false,saveCount=0,metadataCount=0;const revisions={};
 await context.route(url=>url.pathname.startsWith('/api/'),async route=>{
  const request=route.request(),url=new URL(request.url()),path=url.pathname,method=request.method();
  let body={},status=200;const parts=path.split('/'),id=parts[4],row=rows[id];
  if(path==='/api/admin/strategies'){
   if(method==='GET')body=Object.values(rows);
   else {body=rows.new={...make('new'),...request.postDataJSON()};body.draft.source_code=body.source_code;body.draft.parameter_schema=body.parameter_schema;body.draft.default_parameters=body.default_parameters;status=201;}
  }else if(row){
   if(parts[5]==='draft'){
    saveCount++;if(delaySave)await new Promise(r=>setTimeout(r,350));
    if(conflict){status=409;body={detail:'draft version conflict'};conflict=false;}
    else{const payload=request.postDataJSON();assert.equal(payload.version,row.draft.version);row.draft={...row.draft,...payload,version:row.draft.version+1};body=row.draft;}
   }else if(parts[5]==='validate')body={valid:true,draft_version:row.draft.version,source_hash:row.draft.source_hash,issues:[]};
   else if(parts[5]==='publish'){const r={id:'rev-'+id,revision_number:1,source_hash:'abcdef',runtime_manifest:{},published_at:'2026-09-12T08:01:00Z',...row.draft};revisions[id]=r;row.current_revision=r;row.current_revision_id=r.id;row.draft_changed_since_revision=false;body=r;status=201;}
   else if(parts[5]==='revisions')body=parts[6]?revisions[id]:(revisions[id]?[revisions[id]]:[]);
   else if(parts[5]==='backtests')body={runs:{items:[],has_more:false},published_revisions:[],strategy:row};
   else if(method==='PATCH'){metadataCount++;const payload=request.postDataJSON();assert.equal(payload.version,row.version);Object.assign(row,payload,{version:row.version+1});body=row;}
   else if(method==='DELETE'){row.state='archived';status=204;}
   else body=row;
  }else if(path.includes('auth')){status=204;}else if(path.includes('version'))body={version:'0.2.0'};
  await route.fulfill({status,contentType:'application/json',body:status===204?'':JSON.stringify(body)});
 });
 try {
  await page.goto('http://127.0.0.1:5179/admin/strategies/one');await page.getByRole('textbox',{name:'策略源码',exact:true}).waitFor();
  const code=()=>page.getByRole('textbox',{name:'策略源码',exact:true});
  await code().fill(source+'# unsaved\n');
  await page.getByRole('tab',{name:'params.json',exact:true}).click();
  await page.getByRole('textbox',{name:'默认参数 JSON',exact:true}).fill('{invalid');
  await page.getByRole('tab',{name:'strategy.py',exact:true}).click();
  assert.match(await code().inputValue(),/unsaved/);
  await page.getByRole('button',{name:'保存草稿',exact:true}).click();
  await page.getByRole('alert').filter({hasText:'JSON'}).waitFor();assert.equal(saveCount,0);
  await page.getByRole('tab',{name:'params.json',exact:true}).click();
  await page.getByRole('textbox',{name:'默认参数 JSON',exact:true}).fill('{"lookback": 80, "nested": {"keep": true}}');
  await page.getByRole('tab',{name:'策略说明',exact:true}).click();
  await page.getByRole('textbox',{name:'策略名称',exact:true}).fill('改名策略');
  conflict=true;await page.getByRole('button',{name:'保存草稿',exact:true}).click();
  await page.getByRole('alert').filter({hasText:'版本冲突'}).waitFor();assert.equal(metadataCount,1);
  await page.getByRole('button',{name:'保存草稿',exact:true}).click();
  await page.getByRole('status').filter({hasText:'草稿已保存'}).waitFor();assert.equal(metadataCount,1);assert.equal(rows.one.draft.default_parameters.nested.keep,true);
  await page.getByRole('button',{name:'静态检查',exact:true}).click();await page.getByRole('status').filter({hasText:'静态校验通过'}).waitFor();
  await page.getByRole('tab',{name:'strategy.py',exact:true}).click();await code().fill(source+'# edit again\n');
  assert.match(await page.locator('.qfs-status').innerText(),/检查已过期/);
  await page.evaluate(()=>{window.confirm=()=>false;});await page.locator('.qfs-list button').filter({hasText:'策略 two'}).click();assert.match(page.url(),/one$/);assert.match(await code().inputValue(),/edit again/);
  delaySave=true;await page.getByRole('button',{name:'保存草稿',exact:true}).click();assert.equal(await code().getAttribute('readonly'),'');await page.getByRole('status').filter({hasText:'草稿已保存'}).waitFor();delaySave=false;
  await page.getByRole('button',{name:'发布版本',exact:true}).click();await page.getByRole('status').filter({hasText:'已发布策略版本'}).waitFor();
  await page.getByRole('tab',{name:'版本',exact:true}).click();await page.getByRole('button',{name:/v1 · 当前发布版本/}).click();await page.getByRole('dialog',{name:'发布版本 v1'}).waitFor();assert.match(await page.getByRole('dialog').innerText(),/默认参数/);await page.getByRole('button',{name:'关闭发布版本 v1',exact:true}).click();
  await page.locator('.qfs-list button').filter({hasText:'策略 two'}).click();await page.waitForURL('**/two');await page.locator('.qfs-object strong').filter({hasText:'策略 two'}).waitFor();await code().fill(source+'# protect back\n');await page.locator('.qfs-file-tabs').filter({hasText:'未保存'}).waitFor();
  await page.evaluate(()=>{window.confirm=()=>false;});await page.evaluate(()=>history.back());await page.waitForTimeout(150);assert.match(page.url(),/two$/);assert.match(await code().inputValue(),/protect back/);
  await page.evaluate(()=>{window.confirm=()=>true;});await page.evaluate(()=>history.back());await page.waitForURL('**/one');
  await page.getByRole('button',{name:'新建策略',exact:true}).click();await page.getByRole('dialog',{name:'创建策略'}).waitFor();await page.getByRole('textbox',{name:'策略名称',exact:true}).fill('新模板');await page.getByLabel('初始模板').selectOption('rotation');
  await page.getByRole('button',{name:'创建并进入编辑器',exact:true}).click();await page.waitForURL('**/new');await page.locator('.qfs-object strong').filter({hasText:'新模板'}).waitFor();assert.match(await code().inputValue(),/context.universe.query/);
  await page.getByRole('tab',{name:'版本',exact:true}).click();await page.evaluate(()=>{window.confirm=()=>true;});await page.getByRole('button',{name:'归档策略',exact:true}).click();await page.getByRole('status').filter({hasText:'策略已归档'}).waitFor();assert.equal(await code().getAttribute('readonly'),'');
  for(const width of [1440,1200,900,860,600,390]){
   await page.setViewportSize({width,height:900});await page.waitForTimeout(70);
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,`page overflow ${width}`);
   assert.equal(await page.getByRole('button',{name:'新建策略',exact:true}).isVisible(),true);
   if(width<900){await page.getByRole('button',{name:'参数面板',exact:true}).click();assert.equal(await page.getByRole('complementary',{name:'策略上下文'}).isVisible(),true);await page.keyboard.press('Escape');}
  }
  assert.deepEqual(errors,[]);console.log('PASS: dirty tabs, malformed JSON, partial-save conflict retry, stale checks, locked saves, publish/snapshot, navigation/back protection, create/archive, responsive panels.');
 } catch(e) {console.error('PAGE',page.url(),errors,await page.locator('body').innerText());throw e;} finally {await context.close();await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
