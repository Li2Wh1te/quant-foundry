const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }, fileName: filename
}).outputText, filename);
const draft = require('../src/pages/tasks/taskDraft.ts');
const view = require('../src/pages/tasks/taskPresentation.ts');
const api = require('../src/api/scheduler.ts');
const type = { key:'fixture',name:'日历采集',english_name:'Calendar',parameter_schema:{properties:{exchange:{type:'string'},initial_start_date:{type:'string',format:'date'},request_interval_ms:{anyOf:[{type:'integer',minimum:0,maximum:60000},{type:'null'}],default:null},coverage_confirmed:{type:'boolean',default:false}},required:['exchange','initial_start_date']} };
const original={id:'task-1',name:'旧任务',description:null,task_type:'fixture',parameters:{exchange:'SSE',initial_start_date:'2020-01-01',request_interval_ms:0,future_parameter:{value:7}},schedule:{type:'once',run_at:'2020-01-01T00:00:12.345678+08:00'},concurrency_limit:1,overlap_policy:'skip',queue_limit:1,priority:0,state:'completed',version:3};
test('name-only edit preserves expired once schedule, unknown parameters and source data',()=>{
 const value=draft.draftFromTask(original); value.name='更名';
 assert.deepEqual(draft.taskPatch(value,original),{name:'更名'});
 assert.equal(original.parameters.future_parameter.value,7);
});
test('unchanged cron preserves custom timezone and exact expression; plan changes are explicit',()=>{
 const task={...original,schedule:{type:'cron',expression:'0 3 * * MON-FRI',timezone:'Pacific/Chatham'}};
 const value=draft.draftFromTask(task);
 assert.equal(value.timezone,'Pacific/Chatham'); assert.equal(value.cronMode,'advanced');
 assert.deepEqual(draft.taskPatch(value,task),{});
 value.cronExpression='0 4 * * MON-FRI';
 assert.deepEqual(draft.taskPatch(value,task),{schedule:{...task.schedule,expression:'0 4 * * MON-FRI'}});
});
test('calendar schedules use APScheduler weekday numbers and round-trip preset meanings',()=>{
 const value=draft.newTaskDraft([type]);
 assert.equal(draft.cronExpressionFromDraft(value),'0 18 * * 0-4');
 value.cronMode='weekly'; value.cronWeekdays=['6','0']; assert.equal(draft.cronExpressionFromDraft(value),'0 18 * * 0,6');
 value.cronWeekdays=[]; assert.throws(()=>draft.cronExpressionFromDraft(value),/至少选择/);
 for (const [expression,mode] of [['0 9 * * *','daily'],['10 8 * * 0-4','weekdays'],['0 7 * * 2,5','weekly'],['0 3 31 * *','monthly'],['*/15 * * * *','advanced']]) assert.equal(draft.cronEditorValues(expression).cronMode,mode);
});
test('script parameters retain nullable zero and false and enforce date-window/numeric constraints',()=>{
 const value=JSON.parse(draft.parametersTemplate(type));
 assert.equal(value.request_interval_ms,null); assert.equal(value.coverage_confirmed,false);
 const params={exchange:'SSE',initial_start_date:'2020-01-01',request_interval_ms:0,coverage_confirmed:false};
 assert.deepEqual(draft.validateParameters(params,type),{});
 assert.ok(draft.validateParameters({...params,request_interval_ms:1.5},type).request_interval_ms);
 assert.ok(draft.validateParameters({...params,request_interval_ms:60001},type).request_interval_ms);
 assert.ok(draft.validateParameters({...params,start_date:'2026-02-02',end_date:'2026-01-01'},type).end_date);
 assert.equal(draft.parameterInput('20200101',{type:'date'}),'2020-01-01');
});
test('completed/paused/source gates override stale next-run values and block execution',()=>{
 const task={...original,registered:true,state:'active',source_enabled:true,source_configured:true,next_run_at:'2026-09-08T16:00:00Z'};
 assert.equal(view.canRun(task),true); assert.match(view.nextRunLabel(task),/09\/09 00:00/);
 for(const change of [{state:'completed'},{registered:false},{source_enabled:false},{source_configured:false}]) assert.equal(view.canRun({...task,...change}),false);
 assert.match(view.nextRunLabel({...task,source_enabled:false}),/停用/);
 assert.match(view.nextRunLabel({...task,state:'completed'}),/无后续计划/);
 assert.equal(view.canRun({...task,state:'paused'}),true);
});
test('operator summaries never expose vendor error or raw JSON by default',()=>{
 for(const status of ['queued','running','succeeded','failed','skipped','interrupted','cancelled','timed_out','indeterminate']) {
  const text=view.runSummary({status,error_message:'private vendor exception',result:{private:'internal'}});
  assert.match(text,/[\u4e00-\u9fff]/); assert.doesNotMatch(text,/private|internal/);
 }
});
test('workspace requests encode server filters and pagination and retain mutation versions',async()=>{
 const beforeFetch=global.fetch,beforeWindow=global.window,calls=[];
 global.window={sessionStorage:{getItem:()=> 'fixture-only'}};
 global.fetch=async(path,options)=>{calls.push({path,options});return {ok:true,json:async()=>({items:[],total:0})};};
 try {
  await api.listTaskWorkspace({query:'100%_ 日历',source_key:'a/b',status:'running',offset:40});
  const query=new URL(calls[0].path,'http://example.test').searchParams;
  assert.equal(query.get('query'),'100%_ 日历'); assert.equal(query.get('offset'),'40');assert.equal(query.get('limit'),'20');assert.equal(query.get('source_key'),'a/b');
  await api.updateTask(original,{description:null}); assert.deepEqual(JSON.parse(calls[1].options.body),{version:3,description:null});
  await api.listTaskRuns('fixture/id',20); assert.match(calls[2].path,/fixture%2Fid\/runs\?limit=10&offset=20/);
  for (const call of calls) {assert.equal(call.options.cache,'no-store');assert.equal(call.options.headers.Authorization,'Bearer fixture-only');}
 } finally {global.fetch=beforeFetch;global.window=beforeWindow;}
});
test('errors distinguish conflict, unknown write outcome, authorization and caller cancellation',async()=>{
 const before=global.fetch;
 try{
  global.fetch=async()=>({ok:false,status:409,json:async()=>({detail:'private'})});await assert.rejects(api.updateTask(original,{}),error=>error.status===409&&/版本/.test(error.message));
  global.fetch=async()=>{throw new TypeError('private network');};await assert.rejects(api.createTask({}),error=>error.status===0&&/不要重复提交/.test(error.message));
  global.fetch=async()=>({ok:false,status:401,json:async()=>({detail:'Unauthorized'})});await assert.rejects(api.listTaskWorkspace({}),error=>error.status===401);
  const controller=new AbortController();controller.abort();global.fetch=async()=>{throw new DOMException('','AbortError');};await assert.rejects(api.listTaskWorkspace({},controller.signal),{name:'AbortError'});
 }finally{global.fetch=before;}
});
