/* D09 semantics and contract supplements. Backend/default-app and browser tests
   separately prove real isolated formal reads; these public samples are synthetic. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }, fileName: filename
}).outputText, filename);
const view = require('../src/pages/market/marketPresentation.ts');
const data = require('../src/features/market-data/data/index.ts');
const { createCatalogPager, MAX_CATALOG_PAGES } = require('../src/features/market-data/catalog/paging.ts');
const { MarketTable } = require('../src/pages/market/MarketTable.tsx');
const { MarketCalendarDetail } = require('../src/pages/market/MarketCalendarDetail.tsx');
const { createElement } = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { MemoryRouter } = require('react-router-dom');
const meta = {read_token:'synthetic-current-A',read_at:'2026-08-30T17:05:00Z',business_as_of:null,
  scope:'合成正式资料范围',source_labels:['合成正式夹具'],coverage:{verified:false,state:'unknown',missing_dates:null,note:'未核验'},limitations:[]};
const instrument = (symbol='TEST.SH') => ({instrument_key:'etf-current:'+symbol,symbol,name:'测试ETF',asset_type:'etf',exchange:'SSE',list_status:null,list_date:null,index_name:null,manager_name:null,management_fee:null,management_fee_unit:null,capabilities:[]});
const page = (items=[],total=items.length,cursor=null,token=meta.read_token) => ({catalog_key:'etf-current',items,total,next_cursor:cursor,meta:{...meta,read_token:token}});
const calendar = (day,state='open') => ({items:[{date:day,exchange:'SSE',state,previous_trading_date:null,next_trading_date:null}],meta});
const render = props => renderToStaticMarkup(createElement(MemoryRouter,null,createElement(MarketTable,{result:null,loading:false,error:false,empty:false,returnTo:view.MARKET_PATH,onOpen(){},...props})));
const renderDetail = props => renderToStaticMarkup(createElement(MarketCalendarDetail,{detail:null,date:'2026-08-27',exchange:'SSE',loading:false,error:'',onRetry(){},...props}));
const calendarSnapshot = {key:'SSE:2026-08-27',date:'2026-08-27',exchange:'SSE',value:calendar('2026-08-27')};
const filters = {keyword:'',exchange:'',listStatus:'',limit:50,offset:0};

test('selected date or exchange change immediately withdraws prior facts', () => {
  const html=renderDetail({detail:calendarSnapshot,date:'2026-08-31',exchange:'SZSE',loading:true});
  assert.match(html,/aria-busy="true"/);assert.match(html,/qfm-aside-date">2026\/08\/31</);
  assert.match(html,/深交所/);assert.match(html,/读取中/);
  assert.doesNotMatch(html,/上交所|2026\/08\/27|08\/31 01:05/);
});
test('formal missing dates and unknown neighbors never become closed or invented dates', () => {
  const missing={...calendarSnapshot,value:calendar('2026-08-27','unknown')};
  const html=renderDetail({detail:missing});
  assert.match(html,/未知/);assert.match(html,/不能推定为休市/);assert.match(html,/缺少独立依据/);
  const stale=renderDetail({detail:calendarSnapshot,error:'连接失败',stale:true});
  assert.match(stale,/旧资料/);assert.match(stale,/role="alert"/);assert.match(stale,/交易日/);
  const denied=renderDetail({detail:null,error:'无权读取'});
  assert.doesNotMatch(denied,/08\/31 01:05|>交易日</);assert.match(denied,/无权读取/);
});
test('filters round trip literal search, unsupported status choice and aligned URL positions', () => {
  const value={keyword:'100%_ 指数',exchange:'SZSE',listStatus:'P',limit:100,offset:200};
  assert.deepEqual(view.marketFilters(new URLSearchParams(view.marketQuery(value))),value);
  assert.deepEqual(view.marketFilters(new URLSearchParams('exchange=bad&status=bad&limit=5&offset=-1')),filters);
  assert.equal(view.marketFilters(new URLSearchParams('limit=20&offset=29')).offset,20);
  for(const offset of ['Infinity','NaN','100000000','-20','1.4']) assert.equal(view.marketFilters(new URLSearchParams({offset})).offset,0);
  assert.equal(view.marketFilters(new URLSearchParams({keyword:'a'.repeat(200)})).keyword.length,128);
});
test('return navigation accepts only filter/position context on the original market route', () => {
  for(const value of [null,{},'https://evil.test','//evil.test','/admin/data/etf-basics/elsewhere','/admin/logs']) assert.equal(view.marketReturnTo(value),view.MARKET_PATH);
  assert.equal(view.marketReturnTo(view.MARKET_PATH+'?exchange=SSE&offset=50&cursor=secret&read_token=secret'),view.MARKET_PATH+'?exchange=SSE&offset=50');
});
test('calendar geometry remains Monday-first across leap/year boundaries without session inference', () => {
  for(const month of ['2024-02','2026-09','2026-12','2027-01']) {
    const dates=view.calendarDates(month);assert.equal(dates.length,42);assert.equal(new Set(dates).size,42);
    assert.equal(new Date(dates[0]+'T00:00:00Z').getUTCDay(),1);
    for(let i=1;i<42;i++) assert.equal(Date.parse(dates[i])-Date.parse(dates[i-1]),86400000);
  }
  assert.ok(view.calendarDates('2024-02').includes('2024-02-29'));
  assert.equal(view.shiftMonth('2026-12',1),'2027-01');assert.equal(view.shiftMonth('2026-01',-1),'2025-12');
  assert.equal(view.calendarState(undefined),'unknown');assert.equal(view.calendarState(null),'unknown');
  assert.equal(view.calendarState({state:'closed'}),'closed');assert.equal(view.calendarState({state:'open'}),'open');
  assert.equal(view.shanghaiToday(new Date('2026-09-10T16:01:00Z')),'2026-09-11');
});
test('fee text and declared units remain exact, and read time is not a collection update', () => {
  assert.equal(view.managementFee('0','%'),'0 %');assert.equal(view.managementFee('0.150000000000000001','%'),'0.150000000000000001 %');
  assert.equal(view.managementFee('9007199254740993'),'9007199254740993');
  for(const value of [null,'','invalid']) assert.equal(view.managementFee(value),'—');
  assert.equal(view.syncPresentation(null).value,'—');assert.equal(view.syncPresentation({meta}).label,'读取时点');
  assert.match(view.syncPresentation({meta}).value,/08\/31 01:05/);assert.match(view.syncPresentation({meta}).note,/不是采集更新时间/);
});
test('loading, admitted empty directory, no matches and denied data remain distinct', () => {
  assert.match(render({loading:true}),/qfm-skeleton/);assert.doesNotMatch(render({loading:true}),/为空|没有符合/);
  assert.match(render({empty:true}),/正式 ETF 目录为空/);assert.doesNotMatch(render({empty:true}),/采集任务/);
  assert.match(render({result:page()}),/没有符合当前筛选/);assert.match(render({error:true}),/暂不可用/);
});
test('table preserves eight fields, null business facts and formal profile navigation', () => {
  const html=render({result:page([instrument()])});assert.equal((html.match(/scope="col"/g)||[]).length,8);
  for(const fact of ['TEST.SH','测试ETF','上交所','未知状态','ETF · 正式资料']) assert.ok(html.includes(fact),fact);
  assert.match(html,/href="\/admin\/data\/etf-basics\/TEST.SH\?tab=profile"/);
  assert.doesNotMatch(html,/0%|上市<|data-collections/);
});
test('catalog cursor chain pages forward/back and replays an uncached return position on the server', async () => {
  const calls=[];
  const client=data.createMarketClient(async path=> {
    const q=new URL(path,'http://test').searchParams;calls.push(q);
    const index=q.get('cursor')==='cursor_A'?1:q.get('cursor')==='cursor_B'?2:0;
    return page([instrument('PAGE'+index+'.SH')],121,index===0?'cursor_A':index===1?'cursor_B':null);
  });
  const pager=createCatalogPager(client),signal=new AbortController().signal;
  assert.equal((await pager.load({...filters,offset:100},signal)).page.items[0].symbol,'PAGE2.SH');
  assert.equal(calls.length,3);
  assert.equal((await pager.load({...filters,offset:50},signal)).page.items[0].symbol,'PAGE1.SH');
  assert.equal(calls.length,4);assert.equal(calls[3].get('cursor'),'cursor_A');
  await pager.load({...filters,keyword:'末页ETF'},signal);
  assert.equal(calls[4].get('cursor'),null);assert.equal(calls[4].get('q'),'末页ETF');
  assert.ok(calls.every(q=>!q.has('offset')));
  await assert.rejects(pager.load({...filters,offset:MAX_CATALOG_PAGES*50},signal),e=>e.code==='QUERY_BUDGET_EXCEEDED');
});
test('catalog change during replay rejects old continuation and cancellation fences cursor writes', async () => {
  let token='A',calls=0;
  const client=data.createMarketClient(async()=>{calls++;return page([instrument()],100,'cursor_A',token);});
  const pager=createCatalogPager(client),signal=new AbortController().signal;
  await pager.load(filters,signal);token='B';
  await assert.rejects(pager.load({...filters,offset:50},signal),e=>e.code==='DATA_CHANGED');
  await pager.load(filters,signal);assert.equal(calls,3);
  const controller=new AbortController();controller.abort();
  await assert.rejects(pager.load(filters,controller.signal),{name:'AbortError'});
  assert.equal(calls,3);
});
test('D09 client requests use the authenticated formal routes and preserve exact calendar dates', async () => {
  const previousFetch=global.fetch,previousWindow=global.window,calls=[];
  global.window={sessionStorage:{getItem:()=> 'synthetic-browser-only'}};
  global.fetch=async(path,options)=>{calls.push({path,options});return new Response(JSON.stringify(path.includes('/calendar')?calendar('2026-08-27'):page()),{status:200});};
  try {
    const signal=new AbortController().signal;
    await data.marketClient.instruments({catalog_key:'etf-current',q:'100%_ 指数',exchange:'SSE',limit:50,signal});
    const q=new URL(calls[0].path,'http://test').searchParams;assert.equal(q.get('q'),'100%_ 指数');assert.equal(q.get('catalog_key'),'etf-current');
    await data.marketClient.calendarDay('SSE','2026-08-27',{signal});assert.equal(calls[1].path,'/api/admin/market/calendar/SSE/2026-08-27');
    for(const {path,options} of calls) {assert.match(path,/^\/api\/admin\/market\//);assert.equal(options.cache,'no-store');assert.equal(options.headers.Authorization,'Bearer synthetic-browser-only');assert.equal(options.method,'GET');assert.ok(options.signal instanceof AbortSignal);}
  } finally {global.fetch=previousFetch;global.window=previousWindow;}
});
