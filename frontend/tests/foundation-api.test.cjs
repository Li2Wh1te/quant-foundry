/* Transport fixtures never contact the production service or suppliers. */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const ts=require('typescript');
require.extensions['.ts']=(module,filename)=>module._compile(ts.transpileModule(fs.readFileSync(filename,'utf8'),{
  compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022},fileName:filename
}).outputText,filename);
const {foundationApi,FoundationApiError}=require('../src/api/foundation.ts');

test('network failure has a safe Chinese summary',async t=>{
  t.mock.method(global,'fetch',async()=>{throw new TypeError('Failed to fetch private diagnostic');});
  await assert.rejects(foundationApi('/datasets'),error=>error instanceof FoundationApiError&&error.status===0&&error.message==='无法连接数据服务，请检查网络后重试。');
});

test('malformed successful response does not expose parser input',async t=>{
  t.mock.method(global,'fetch',async()=>new Response('<private upstream error>',{status:200}));
  await assert.rejects(foundationApi('/datasets'),error=>error.status===502&&error.message==='数据服务返回格式异常，请稍后重试。');
});

test('permission and changed-context statuses remain actionable',async t=>{
  for(const status of [401,403,409,410,503]){
    t.mock.method(global,'fetch',async()=>new Response(JSON.stringify({detail:{message:'当前依据不可继续使用。'}}),{status}));
    await assert.rejects(foundationApi('/datasets'),error=>error.status===status&&error.message==='当前依据不可继续使用。');
  }
});

test('deadline aborts a hung request while manual cancellation stays distinct',async t=>{
  let expire;
  t.mock.method(global,'setTimeout',callback=>{expire=callback;return 123;});
  t.mock.method(global,'clearTimeout',()=>{});
  t.mock.method(global,'fetch',(_url,{signal})=>new Promise((_resolve,reject)=>signal.addEventListener('abort',()=>reject(signal.reason),{once:true})));
  const timed=foundationApi('/datasets');expire();
  await assert.rejects(timed,error=>error.status===504&&/超时/.test(error.message));
  const caller=new AbortController();const cancelled=foundationApi('/datasets',caller.signal);caller.abort();
  await assert.rejects(cancelled,error=>error.name==='AbortError'&&!(error instanceof FoundationApiError));
});

test('successful requests keep fixed bodies and clear deadlines',async t=>{
  const clean=t.mock.method(global,'clearTimeout');
  const body={release:'fixed-release',fields:['close']};
  t.mock.method(global,'fetch',async(url,options)=>{
    assert.equal(url,'/api/admin/data-foundation/queries');assert.equal(options.method,'POST');
    assert.deepEqual(JSON.parse(options.body),body);
    return new Response(JSON.stringify({items:[{close:'1.2345678901'}]}));
  });
  assert.deepEqual(await foundationApi('/queries',undefined,body),{items:[{close:'1.2345678901'}]});
  assert.equal(clean.mock.callCount(),1);
});
