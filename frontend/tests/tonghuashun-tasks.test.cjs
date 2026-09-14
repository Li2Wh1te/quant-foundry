const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
require.extensions['.ts'] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }, fileName: filename
}).outputText, filename);
const draft = require('../src/pages/tasks/taskDraft.ts');
const view = require('../src/pages/tasks/taskPresentation.ts');
const type = {key:'data.ths.fund_profile',name:'同花顺基金资料',english_name:'Tonghuashun profiles',parameter_schema:{properties:{
  asset_types:{type:'array',items:{type:'string',enum:['fund-etf','fund-lof']},default:['fund-etf','fund-lof'],minItems:1},
  subjects:{anyOf:[{type:'array',items:{type:'string'},minItems:1,maxItems:10000},{type:'null'}],default:null},
  mode:{type:'string',enum:['incremental','backfill','reconcile'],default:'incremental'},
  batch_size:{type:'integer',minimum:1,maximum:1000,default:100},
  refresh_today:{type:'boolean',default:false},
}}};
test('source-specific arrays preserve machine values and expose Chinese field copy',()=>{
  const params=JSON.parse(draft.parametersTemplate(type));
  assert.deepEqual(params.asset_types,['fund-etf','fund-lof']);
  assert.equal(params.subjects,null);
  const fields=draft.parameterFields(type);
  assert.equal(fields.find(f=>f.key==='asset_types').label,'资产类型');
  assert.deepEqual(fields.find(f=>f.key==='asset_types').itemEnum,['fund-etf','fund-lof']);
  assert.equal(draft.parameterInput(params.asset_types,fields[0]),'ETF, LOF');
  assert.equal(draft.parameterOptionLabel('backfill'),'首次回补与失败补采');
  assert.deepEqual(draft.validateParameters(params,type),{});
  const single={...type,parameter_schema:{properties:{asset_types:{type:'array',items:{type:'string',const:'fund-etf'},default:['fund-etf']}}}};
  assert.deepEqual(draft.parameterFields(single)[0].itemEnum,['fund-etf']);
});
test('invalid scope arrays and incomplete pasted codes fail before submission',()=>{
  const params=JSON.parse(draft.parametersTemplate(type));
  assert.ok(draft.validateParameters({...params,asset_types:[]},type).asset_types);
  assert.ok(draft.validateParameters({...params,asset_types:['a-share']},type).asset_types);
  assert.ok(draft.validateParameters({...params,subjects:['510300.SH','']},type).subjects);
  assert.ok(draft.validateParameters({...params,batch_size:0},type).batch_size);
  assert.deepEqual(draft.validateParameters({...params,subjects:['510300.SH','510500.SH']},type),{});
});
test('collection event fallback never renders its internal event key',()=>{
  const summary=view.runSummary({status:'succeeded',result:{event:'tonghuashun_collection_completed'}});
  assert.match(summary,/同花顺采集完成/);
  assert.doesNotMatch(summary,/tonghuashun_collection/);
  assert.equal(view.runSummary({status:'succeeded',result:{message:'同花顺本批完成，剩余 2 个待续采。'}}),'同花顺本批完成，剩余 2 个待续采。');
  assert.equal(view.runSummary({task_type:'data.ths.fund_profile',status:'failed',error_type:'CollectionError',error_message:'同花顺基金资料部分失败：成功 1 个，失败 1 个。'}),'同花顺基金资料部分失败：成功 1 个，失败 1 个。');
  assert.doesNotMatch(view.runSummary({task_type:'data.ths.fund_profile',status:'failed',error_type:'VendorError',error_message:'上游原文不应显示'}),/上游原文/);
});
