const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),ts=require('typescript');
require.extensions['.ts']=(m,f)=>m._compile(ts.transpileModule(fs.readFileSync(f,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022},fileName:f}).outputText,f);
const data=require('../src/pages/backtests/resultData.ts'),api=require('../src/api/backtestResults.ts');
const response=v=>({ok:true,status:200,json:async()=>v});
const page=(items=[],cursor=null)=>({schema_id:'qf.results.v1',run_id:'r',partial:false,items,next_cursor:cursor});
test('Decimal original values, tiny differences and NA survive display and JSON',()=>{
 const amount='7922816251426433759.123456789012345678',tiny='0.000000000000000001';
 assert.equal(data.displayNumber(amount),amount);assert.equal(data.displayNumber(tiny),tiny);assert.equal(data.exactDifference(amount,'7922816251426433759.123456789012345677'),tiny);
 assert.equal(data.displayNumber('.123456789012345678',true),'+12.3456789012345678%');assert.equal(data.displayNumber(0,true),'0.00%');
 for(const v of [null,undefined,'NA','NaN','Infinity',true,false,{status:'unavailable',reason:'MISSING_EQUITY'}])assert.equal(data.displayNumber(v),'—');
 assert.equal(data.metricValue({status:'available',value:0}),0);assert.equal(data.reason({status:'unavailable',reason:'MISSING_EQUITY'}),'MISSING_EQUITY');
});
test('chart projection keeps missing equity and exact nanoseconds; no recomputation of old metrics',()=>{
 const row={time_ns:'1767225600000000123',equity:null,session_return:{status:'unavailable',reason:'MISSING_EQUITY'}};
 const result=data.chartRows([row])[0];assert.equal(result.time_ns,row.time_ns);assert.equal(result.valuation_status,'blocked');assert.equal(result.as_of,'2026-01-01T00:00:00.000Z');
 const metrics=[{metric_key:'total_return',value:'.123456789012345678',formula_version:'legacy'}];assert.equal(data.summaryMetrics({schema_id:'legacy',metrics}),metrics);
 assert.equal(data.summaryMetrics({schema_id:'qf.results.v1',performance:null}).length,0);
 assert.equal(data.localDay('2026-01-31T20:00:00Z'),'2026-02-01');
});
test('reader fetches one bounded page, preserves strings, cursor and partial/truncation notices',async()=>{
 const old=global.fetch;let calls=0;global.fetch=async(path,init)=>{calls++;assert.match(path,/\/backtests\/r\/results\/trades\?limit=100&cursor=a%2Bb/);assert.equal(init.cache,'no-store');return response({...page([{result_sequence:'9007199254740993',execution_time_ns:'1767225600000000123',price:'123456789.123456789012345678',fee:'1.000000000000000001'}],'next'),partial:true,notices:{logs_truncated:true}})};
 try{const value=await api.resultPage('r','trades','a+b');assert.equal(calls,1);assert.equal(value.items[0].price,'123456789.123456789012345678');assert.equal(value.partial,true);assert.equal(value.notices.logs_truncated,true);}finally{global.fetch=old;}
});
test('reader rejects loss of precision, bad identity, sequence and overflowing/repeated pages',async()=>{
 const old=global.fetch;try{for(const v of [{...page(),run_id:'other'},page(Array(101).fill({})),page([{result_sequence:'1',price:1.1}]),page([{result_sequence:'2'},{result_sequence:'1'}]),page([{result_sequence:9007199254740993}]),page([], 'same'),{...page(),has_more:true}]){global.fetch=async()=>response(v);await assert.rejects(api.resultPage('r','orders','same'),e=>e.code==='INVALID_CONTRACT');}}finally{global.fetch=old;}
});
test('hidden 404 invalidates the selected result identity; late aborted success is discarded',async()=>{
 const old=global.fetch,hidden=[];const off=api.onResultHidden(ids=>hidden.push(ids));
 try{global.fetch=async()=>({ok:false,status:404,json:async()=>({detail:{code:'DATA_RESTRICTED'}})});await assert.rejects(api.resultPage('r','equity'));assert.deepEqual(hidden,[['r']]);
 const c=new AbortController();global.fetch=async()=>{c.abort();return response(page())};await assert.rejects(api.resultPage('r','equity',undefined,c.signal),e=>e.name==='AbortError');}finally{off();global.fetch=old;}
});
test('2..16 comparison uses baseline ordering only and refuses fake rankings and unexpected members',async()=>{
 const ids=Array.from({length:16},(_,i)=>'r'+i),items=ids.map(run_id=>({run_id,engine_kind:'rust',status:'succeeded',partial:false,config:{}}));const old=global.fetch;
 try{global.fetch=async(path,init)=>{assert.equal(path,'/api/admin/backtests/compare');const body=JSON.parse(init.body);assert.deepEqual(Object.keys(body),['run_ids']);assert.equal(body.run_ids[0],'r15');return response({items,ranking:null,warnings:[]})};assert.equal((await api.compareResults(ids,'r15')).items.length,16);
 await assert.rejects(api.compareResults([...ids,'r16'],'r15'));global.fetch=async()=>response({items,ranking:['r0'],warnings:[]});await assert.rejects(api.compareResults(ids,'r15'));}finally{global.fetch=old;}
});
