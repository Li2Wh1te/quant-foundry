const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
require.extensions['.ts'] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022},fileName:filename}).outputText, filename);
const {makeDraft,newRule,payloadFor,changedPayload,validateDraft,hasStructuredMetadata}=require('../src/pages/accounts/editor.ts');
const profile=()=>({id:'account',name:'原账户',status:'active',version:7,fee_schedule_version:3,fee_schedule:{key:'fees',version:3,metadata:{currency:'CNY',custom:'retained'},fee_rules:[{...newRule(),rows:undefined,rate:'0.0003000000000000000000001',applicability:{asset_class:'etf',blank:''}}]},metadata:{currency:'CNY',owner:' team ',empty:''}});
test('unchanged editor omits all mutations and includes concurrency token',()=>assert.deepEqual(changedPayload(makeDraft(profile()),profile()),{expected_version:7}));
test('metadata edit does not touch fee history and retains fee metadata',()=>{const p=profile(),d=makeDraft(p);d.metadata.push({key:'region',value:'cn'});const patch=changedPayload(d,p);assert.equal(patch.expected_version,7);assert.equal(patch.metadata.region,'cn');assert.equal(patch.metadata.owner,' team ');assert.equal(patch.metadata.empty,'');assert.equal(patch.fee_schedule,undefined);assert.deepEqual(payloadFor(d,p).fee_schedule.metadata,p.fee_schedule.metadata);});
test('copy has no account identity or version fields and keeps exact decimals',()=>{const p=profile(),d=makeDraft(p,true),payload=payloadFor(d,p);assert.equal(d.name,'原账户 副本');assert.equal(payload.id,undefined);assert.equal(payload.version,undefined);assert.equal(payload.fee_schedule.version,undefined);assert.equal(payload.fee_schedule.fee_rules[0].rate,p.fee_schedule.fee_rules[0].rate);assert.deepEqual(payload.fee_schedule.fee_rules[0].applicability,{asset_class:'etf',blank:''});});
test('rule updates do not erase custom category or condition dictionary keys',()=>{const p=profile(),d=makeDraft(p);d.rules[0].category='legacy';d.rules[0].rows=[{key:'__proto__',value:'value'}];const payload=payloadFor(d,p);assert.equal(payload.fee_schedule.fee_rules[0].category,'legacy');assert.equal(payload.fee_schedule.fee_rules[0].applicability.__proto__,'value');});
test('validation identifies the relevant step and rule without numeric coercion',()=>{const d=makeDraft(profile());assert.equal(validateDraft(d),null);d.rules.push(newRule('second'));d.rules[1].rate='-1';assert.deepEqual(validateDraft(d),{step:1,rule:1,field:'rate',message:'费用规则 2：请填写有效的非负十进制数。'});d.rules[1].rate='0';d.rules[1].rounding_precision='0e10';assert.equal(validateDraft(d).field,'rounding_precision');d.rules[1].rounding_precision='0.0000000000000000001';assert.equal(validateDraft(d),null);d.rules[1].rows=[{key:'x',value:''},{key:'x',value:'a'}];assert.equal(validateDraft(d).field,'conditions');});
test('missing historical rounding is shown missing, never silently defaulted',()=>{const p=profile();p.fee_schedule.fee_rules[0].rounding_mode=null;const d=makeDraft(p);assert.equal(d.rules[0].rounding_mode,null);assert.equal(validateDraft(d).field,'rounding_mode');});
test('structured metadata is detected and an unchanged patch preserves it',()=>{const p=profile();p.metadata.extra={x:1};const d=makeDraft(p);d.name='new';assert.equal(hasStructuredMetadata(p),true);assert.deepEqual(changedPayload(d,p),{expected_version:7,name:'new'});});

const api=require('../src/api/accountProfiles.ts');
test('permanent deletion uses its explicit endpoint, version and current token',async()=>{
  const oldFetch=global.fetch,oldWindow=global.window;const calls=[];
  global.window={sessionStorage:{getItem:()=> 'test-token'}};
  global.fetch=async(path,init)=>{calls.push({path,init});return{ok:true,status:204,json:()=>{throw Error('204 must not parse JSON')}}};
  try{await api.permanentlyDeleteAccount('account/one',7);assert.equal(calls[0].path,'/api/admin/backtest-account-profiles/account%2Fone/permanent?expected_version=7');assert.equal(calls[0].init.method,'DELETE');assert.equal(calls[0].init.headers.Authorization,'Bearer test-token');}
  finally{global.fetch=oldFetch;global.window=oldWindow;}
});
test('deletion checks preserve cancellation and conflicts never become success',async()=>{
  const oldFetch=global.fetch;const controller=new AbortController();
  try{global.fetch=async(path,init)=>{assert.match(path,/\/deletion-check$/);assert.equal(init.signal,controller.signal);return{ok:true,status:200,json:async()=>({can_permanently_delete:false,reason:'已引用'})}};
    assert.equal((await api.checkAccountDeletion('account',controller.signal)).can_permanently_delete,false);
    global.fetch=async()=>({ok:false,status:409,json:async()=>({detail:'账户已有回测引用'})});
    await assert.rejects(api.permanentlyDeleteAccount('account',7),e=>e.status===409&&e.message==='账户已有回测引用');
  }finally{global.fetch=oldFetch;}
});
