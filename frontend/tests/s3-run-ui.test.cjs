const {test, beforeEach}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),ts=require('typescript');
require.extensions['.ts']=(module,file)=>module._compile(ts.transpileModule(fs.readFileSync(file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022},fileName:file}).outputText,file);
const {webcrypto}=require('node:crypto');global.crypto=webcrypto;
const storage=new Map();global.window={sessionStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)}};global.sessionStorage=window.sessionStorage;
const api=require('../src/api/runService.ts'),{validateConfig,compareDecimal,jsonObject}=require('../src/pages/backtests/run-ui/config.ts');
const caps={api_schema:'qf.backtest.v2',engine:{installed:true,frequencies:['1d','1m','tick'],execution_models:['bar_next_interval_v1','trade_tick_v1','quote_tick_v1']},actual_data:[],limits:{batch_runs:256,parameter_bytes:65536,parameter_depth:8}};
const config=()=>({api_schema:'qf.backtest.v2',strategy_revision_id:'revision',account_id:'account',initial_cash:'10000.000000000000001',start:'2026-01-01',end:'2026-01-03',frequency:'1d',universe:['A.SH'],parameters:{weight:'0.123456789012345678'},execution_model:'bar_next_interval_v1',participation_rate:'0.10',slippage_bps:'0',risk_free_rate:'0',annualization_sessions:252,seed:0,result_sampling:'session_close'});
const response=value=>({ok:true,status:202,json:async()=>value});
const run=(value=config())=>({run_id:'run-1',status:'queued',api_schema:'qf.backtest.v2',engine_kind:'rust',normalized_config:value});
beforeEach(()=>storage.set('quant-foundry.api-token','token-'+crypto.randomUUID()));
test('cash, decimal bounds and schema shape stay exact, invalid dates and unsafe JSON are rejected',()=>{
 assert.equal(compareDecimal('0.000000000000000001','0'),1);assert.equal(compareDecimal('-1.00000000000000001','-1'),-1);assert.doesNotThrow(()=>validateConfig(config(),caps));
 for(const change of [v=>v.initial_cash='0',v=>v.initial_cash='1e4',v=>v.initial_cash='NaN',v=>v.start='2026-02-30',v=>v.participation_rate='0',v=>v.participation_rate='1.00000000000000001',v=>v.slippage_bps='10000.01',v=>v.slippage_bps='-0',v=>v.cost_overrides={minimum_commission:'-0'},v=>v.result_sampling='wrong',v=>v.universe=['A.SH','A.SH'],v=>v.seed=true,v=>v.risk_free_rate='-1',v=>v.cost_overrides={commission_rate:'1.01'},v=>v.frequency='tick']){const value=config();change(value);assert.throws(()=>validateConfig(value,caps));}
 assert.throws(()=>jsonObject('{"v":9007199254740993}'));assert.throws(()=>jsonObject('{"v":1e400}'));assert.equal(jsonObject('{"v":"9007199254740993"}').v,'9007199254740993');assert.throws(()=>validateConfig(config(),{...caps,engine:{...caps.engine,installed:false}}));
});
test('double submit shares one HTTP operation and retains exact strings and canonical owner key',async()=>{
 let calls=0,release;global.fetch=async(path,init)=>{calls++;assert.equal(path,'/api/admin/backtests');assert.equal(JSON.parse(init.body).initial_cash,config().initial_cash);assert.ok(init.headers['Idempotency-Key']);await new Promise(r=>release=r);return response(run());};
 const a=api.submit(config()),b=api.submit(config());await new Promise(r=>setTimeout(r,30));assert.equal(calls,1);release();assert.deepEqual(await a,await b);assert.equal(await api.pendingSubmission(),null);
});
test('lost accepted response retries the original key after module reload, and blocks changed config',async()=>{
 let key;global.fetch=async(path,init)=>{key=init.headers['Idempotency-Key'];throw new TypeError('connection lost after acceptance');};await assert.rejects(api.submit(config()));
 const changed=config();changed.initial_cash='2';await assert.rejects(api.submit(changed),e=>e.code==='IDEMPOTENCY_CONFLICT');
 delete require.cache[require.resolve('../src/api/runService.ts')];const reloaded=require('../src/api/runService.ts');let count=0;
 global.fetch=async(path,init)=>{count++;assert.equal(init.headers['Idempotency-Key'],key);return response(run());};assert.equal((await reloaded.retryPending()).run_id,'run-1');assert.equal(count,1);
});
test('definitive rejection permits correction, malformed success remains uncertain',async()=>{
 global.fetch=async()=>({ok:false,status:422,json:async()=>({detail:{code:'INVALID_RUN_CONFIG',message:'do not expose /secret or DSN'}})});await assert.rejects(api.submit(config()),e=>!e.message.includes('/secret'));assert.equal(await api.pendingSubmission(),null);
 global.fetch=async()=>response({status:'queued'});await assert.rejects(api.submit(config()));assert.ok(await api.pendingSubmission());
});
test('unavailable session persistence fails before creating a run',async()=>{
 const original=sessionStorage.setItem;let calls=0;global.fetch=async()=>{calls++;return response(run())};
 sessionStorage.setItem=()=>{throw new Error('quota')};
 try{await assert.rejects(api.submit(config()),e=>e.code==='SUBMISSION_STORAGE_UNAVAILABLE');assert.equal(calls,0);}
 finally{sessionStorage.setItem=original;}
 assert.equal(await api.pendingSubmission(),null);
});
test('a rejection during retry cannot erase a previously uncertain admission',async()=>{
 let key;global.fetch=async(_,init)=>{key=init.headers['Idempotency-Key'];throw new TypeError('lost accepted response')};await assert.rejects(api.submit(config()));
 global.fetch=async()=>({ok:false,status:422,json:async()=>({detail:{code:'DATA_CHANGED'}})});await assert.rejects(api.retryPending());assert.equal((await api.pendingSubmission()).key,key);
 global.fetch=async(_,init)=>{assert.equal(init.headers['Idempotency-Key'],key);return response(run())};assert.equal((await api.retryPending()).run_id,'run-1');assert.equal(await api.pendingSubmission(),null);
});
test('401/403 invalidate immediately without waiting for a stalled body, and abort concurrent reads',async()=>{
 for(const status of [401,403]){
  let invalidated=0;const off=api.onRunAccessDenied(value=>{assert.equal(value,status);invalidated++;});let aborted=false;
  global.fetch=(path,init)=>path.includes('capabilities')?new Promise((_,reject)=>init.signal.addEventListener('abort',()=>{aborted=true;reject(new DOMException('abort','AbortError'));})):Promise.resolve({ok:false,status,body:{cancel:async()=>{}},json:()=>new Promise(()=>{})});
  const read=api.capabilities();const settled=assert.rejects(read);
  await assert.rejects(api.getRun('r'),e=>e.status===status);await settled;assert.equal(invalidated,1);assert.equal(aborted,true);off();
 }
});
test('bounded list/batch and encoded resource paths reject missing cursors and preserve cancellation',async()=>{
 const controller=new AbortController();let path;global.fetch=async(p,init)=>{path=p;assert.ok(init.signal);const value={items:[run()],has_more:true,next_cursor:'signed'};return response(p.includes('/strategies/')?{runs:value}:value);};
 await api.listRuns('a+b/=',controller.signal,'strategy/id');assert.match(path,/strategy%2Fid\/backtests\?limit=20&cursor=a%2Bb%2F%3D/);
 await api.getBatch('b/id','signed',controller.signal);assert.match(path,/b%2Fid\?limit=20&cursor=signed/);
 global.fetch=async()=>response({items:[run()],has_more:true,next_cursor:null});await assert.rejects(api.listRuns());
});
