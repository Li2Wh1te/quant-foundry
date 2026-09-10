/* Fixtures exercise data semantics and wire contracts only. Production views
   always load authenticated backend data instead of importing these samples. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }, fileName: filename
}).outputText, filename);
const view = require('../src/pages/market/marketPresentation.ts');
const api = require('../src/api/dataCollections.ts');
const { MarketTable } = require('../src/pages/market/MarketTable.tsx');
const { MarketCalendarDetail } = require('../src/pages/market/MarketCalendarDetail.tsx');
const { createElement } = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { MemoryRouter } = require('react-router-dom');
const render = props => renderToStaticMarkup(createElement(MemoryRouter,null,createElement(MarketTable,{result:null,loading:false,error:false,empty:false,returnTo:view.MARKET_PATH,onOpen(){},...props})));
const renderDetail = props => renderToStaticMarkup(createElement(MarketCalendarDetail,{detail:null,date:'2026-08-27',exchange:'SSE',loading:false,error:'',onRetry(){},...props}));
const calendarSnapshot = {key:'SSE:2026-08-27',date:'2026-08-27',exchange:'SSE',value:{is_open:true,previous_trading_date:'2026-08-26',next_trading_date:'2026-08-28',updated_at:'2026-08-30T17:05:00Z'}};

test('pending date and exchange changes retain a coherent detail without flashing placeholders', () => {
  const html=renderDetail({detail:calendarSnapshot,date:'2026-08-31',exchange:'SZSE',loading:true});
  assert.match(html,/aria-busy="true"/);
  assert.match(html,/qfm-aside-date">2026\/08\/27</);
  for(const fact of ['交易日','上交所','2026/08/26','2026/08/28','08/31 01:05']) assert.ok(html.includes(fact),fact);
  assert.doesNotMatch(html,/加载中|—|深交所|2026\/08\/31/);
});
test('settled, missing and failed dates never borrow another date or exchange label', () => {
  const next={key:'SZSE:2026-08-31',date:'2026-08-31',exchange:'SZSE',value:{...calendarSnapshot.value,previous_trading_date:'2026-08-28',next_trading_date:null}};
  const settled=renderDetail({detail:next,date:next.date,exchange:next.exchange});
  assert.match(settled,/qfm-aside-date">2026\/08\/31</);assert.match(settled,/深交所/);assert.doesNotMatch(settled,/上交所|2026\/08\/27/);
  assert.match(settled,/后续日期覆盖不足/);
  const missing=renderDetail({detail:{...next,value:null},date:next.date,exchange:next.exchange});
  assert.match(missing,/未采集/);assert.match(missing,/此日期尚未采集/);assert.doesNotMatch(missing,/2026\/08\/28/);
  const failed=renderDetail({detail:calendarSnapshot,date:next.date,exchange:next.exchange,error:'深交所 2026/08/31 的日期详情加载失败，请重试。'});
  assert.match(failed,/role="alert"/);assert.match(failed,/当前显示 上交所 2026\/08\/27 的详情/);
  assert.match(failed,/qfm-aside-date">2026\/08\/27</);assert.match(failed,/2026\/08\/26/);
});

test('market filters round trip literal search, state and aligned page positions', () => {
  const filters={keyword:'100%_ 指数',exchange:'SZSE',listStatus:'P',limit:100,offset:200};
  assert.deepEqual(view.marketFilters(new URLSearchParams(view.marketQuery(filters))),filters);
  const invalid=view.marketFilters(new URLSearchParams('exchange=bad&status=bad&limit=5&offset=-1'));
  assert.deepEqual(invalid,{keyword:'',exchange:'',listStatus:'',limit:50,offset:0});
  assert.equal(view.marketFilters(new URLSearchParams('limit=20&offset=29')).offset,20);
  for(const offset of ['Infinity','NaN','100000000','-20','1.4']) assert.equal(view.marketFilters(new URLSearchParams({offset})).offset,0);
});
test('detail return navigation is restricted to the market route', () => {
  for (const value of [null,{},'https://evil.test','//evil.test','/admin/data/etf-basics/elsewhere','/admin/logs']) assert.equal(view.marketReturnTo(value),view.MARKET_PATH);
  assert.equal(view.marketReturnTo(view.MARKET_PATH+'?exchange=SSE&offset=50'),view.MARKET_PATH+'?exchange=SSE&offset=50');
});
test('calendar date geometry is Monday-first with six weeks across leap years', () => {
  for (const month of ['2024-02','2026-09','2026-12','2027-01']) {
    const dates=view.calendarDates(month);assert.equal(dates.length,42);assert.equal(new Set(dates).size,42);
    assert.equal(new Date(dates[0]+'T00:00:00Z').getUTCDay(),1);
    for(let i=1;i<42;i++) assert.equal(Date.parse(dates[i])-Date.parse(dates[i-1]),86400000);
  }
  assert.ok(view.calendarDates('2024-02').includes('2024-02-29'));
  assert.equal(view.shiftMonth('2026-12',1),'2027-01');assert.equal(view.shiftMonth('2026-01',-1),'2025-12');
});
test('missing days never become closed or open through weekday inference', () => {
  assert.equal(view.calendarState(undefined),'unknown');assert.equal(view.calendarState(null),'unknown');
  assert.equal(view.calendarState({calendar_date:'2026-10-01',is_open:false}),'closed');
  assert.equal(view.calendarState({calendar_date:'2026-08-31',is_open:true}),'open');
  assert.equal(view.shanghaiToday(new Date('2026-09-10T16:01:00Z')),'2026-09-11');
});
test('fee zero, missing values and sync timestamps keep distinct meanings', () => {
  assert.equal(view.managementFee('0'),'0%');assert.equal(view.managementFee('0.1500'),'0.15%');
  for(const value of [null,'','invalid']) assert.equal(view.managementFee(value),'—');
  assert.equal(view.syncPresentation(null).value,'—');
  const value={refreshed_at:null,last_updated_at:'2026-09-10T16:01:00Z'};
  assert.equal(view.syncPresentation(value).label,'记录更新');
  assert.match(view.syncPresentation(value).value,/09\/11 00:01/);
  assert.equal(view.syncPresentation({...value,refreshed_at:'2026-09-11T16:00:00Z'}).label,'最近同步');
});
test('initial loading, empty collection, no matches and unavailable are distinct', () => {
  assert.match(render({loading:true}),/qfm-skeleton/);assert.doesNotMatch(render({loading:true}),/尚未采集|没有符合/);
  assert.match(render({empty:true}),/尚未采集 ETF/);assert.match(render({empty:true}),/href="\/admin\/tasks"/);
  assert.match(render({result:{items:[],total:0}}),/没有符合当前筛选/);
  assert.match(render({error:true}),/暂不可用/);assert.doesNotMatch(render({error:true}),/尚未采集/);
});
test('table retains eight facts, status text, complete names and real detail links', () => {
  const item={ts_code:'TEST.SH',csname:'测试ETF',cname:'基金完整名称',extname:null,etf_type:'纯境内',exchange:'SH',list_status:'P',list_date:null,index_name:'跟踪指数',index_code:'123.CSI',mgr_name:'管理人',mgt_fee:'0'};
  const html=render({result:{items:[item],total:1,limit:50,offset:0}});
  assert.equal((html.match(/scope="col"/g)||[]).length,8);
  for(const fact of ['TEST.SH','测试ETF','基金完整名称','上交所','待上市','跟踪指数','123.CSI','管理人','0%']) assert.ok(html.includes(fact),fact);
  assert.match(html,/href="\/admin\/data\/etf-basics\/TEST.SH"/);assert.doesNotMatch(html,/更新时间/);
});
test('market requests encode exact filters, 42-day ranges, auth and cancellation', async () => {
  const originalFetch=global.fetch,originalWindow=global.window,calls=[];
  global.window={sessionStorage:{getItem:()=> 'fixture-only'}};
  global.fetch=async (path,options)=>{calls.push({path,options});return {ok:true,json:async()=>({items:[]})};};
  try {
    const signal=new AbortController().signal;
    await api.listEtfs({keyword:'100%_ 指数',exchange:'SSE',listStatus:'P',limit:50,offset:100},signal);
    const params=new URL(calls[0].path,'http://example.test').searchParams;
    assert.equal(params.get('keyword'),'100%_ 指数');assert.equal(params.get('list_status'),'P');assert.equal(params.get('offset'),'100');
    await api.listTradingCalendarDays({exchange:'SSE',startDate:'2026-08-31',endDate:'2026-10-11',limit:42},signal);
    assert.match(calls[1].path,/limit=42/);assert.match(calls[1].path,/end_date=2026-10-11/);
    await api.getTradingCalendarDay('SSE','2026-09-01',signal);assert.equal(calls[2].path,'/api/admin/data-collections/trading-calendar/SSE/2026-09-01');
    await api.getEtfOverview(signal);
    for(const {options} of calls) {assert.equal(options.signal,signal);assert.equal(options.cache,'no-store');assert.equal(options.headers.Authorization,'Bearer fixture-only');assert.equal(options.method,undefined);}
  } finally {global.fetch=originalFetch;global.window=originalWindow;}
});
test('404 date coverage, expired auth and late cancelled bodies stay distinguishable', async () => {
  const original=global.fetch;
  try {
    for(const status of [404,401,503]) {
      global.fetch=async()=>({ok:false,status,json:async()=>({detail:'Unavailable'})});
      await assert.rejects(api.getTradingCalendarDay('SSE','2026-09-01'),error=>error instanceof api.DataCollectionApiError && error.status===status);
    }
    const controller=new AbortController();
    global.fetch=async()=>({ok:true,json:async()=>{controller.abort();return {items:[]};}});
    await assert.rejects(api.listEtfs({},controller.signal),{name:'AbortError'});
  } finally {global.fetch=original;}
});
