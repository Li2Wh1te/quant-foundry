/* Isolated provider fixtures never enter the production app or real backend. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }, fileName: filename
}).outputText, filename);
require.extensions['.css'] = () => {};
global.__QF_VERSION__ = fs.readFileSync(require('node:path').join(__dirname, '../../VERSION'), 'utf8').trim();
const { createElement } = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { MemoryRouter } = require('react-router-dom');
const api = require('../src/api/dataSources.ts');
const { SourceDetail, sourceTime } = require('../src/pages/DataSourcesPage.tsx');
const source = {
  key: 'fixture/source', name: 'Fixture', fields: [
    { key: 'endpoint', label: '服务地址', type: 'url', required: true },
    { key: 'credential', label: '访问凭据', type: 'secret', required: true },
    { key: 'retries', label: '重试次数', type: 'integer', required: true, default: 0 },
    { key: 'sandbox', label: '沙盒', type: 'boolean', required: true, default: false }
  ], values: { endpoint: 'https://example.test', credential: 'must-not-display' },
  secret_fields_configured: ['credential'], configured: true, enabled: true, version: 7,
  checked_at: null, check_status: 'not_checked', check_message: null,
  capabilities: [{ key: 'private_task_key', name: '交易日历采集', english_name: 'Trading Calendar Sync', task_count: 2, available: true,
    last_run: { id: 'run-1', task_id: 'task-1', status: 'failed', created_at: '2026-09-08T16:00:00Z', started_at: null, finished_at: null } }]
};
const render = value => renderToStaticMarkup(createElement(MemoryRouter, null, createElement(SourceDetail, { source: value, busy: false, onConfigure() {}, onToggle() {} })));
test('provider schemas preserve zero and false but never initialize a secret', () => {
  const draft = api.sourceDraft(source);
  assert.deepEqual(draft, { endpoint: 'https://example.test', credential: '', retries: 0, sandbox: false });
  assert.deepEqual(api.validateSourceDraft(source, draft), {});
  assert.match(api.validateSourceDraft({ ...source, secret_fields_configured: [] }, draft).credential, /请填写/);
});
test('invalid drafts fail locally', () => {
  const draft = api.sourceDraft(source);
  for (const endpoint of ['', 'ftp://example.test', 'https://user:pass@example.test', 'https://example.test?q=secret', 'https://example.test/#fragment']) assert.ok(api.validateSourceDraft(source, { ...draft, endpoint }).endpoint);
  for (const retries of [1.5, 'invalid', Infinity]) assert.ok(api.validateSourceDraft(source, { ...draft, retries }).retries);
  assert.deepEqual(api.validateSourceDraft(source, { ...draft, credential: ' ' }), {});
});
test('connection evidence and task history remain distinct from availability', () => {
  for (const [change, label] of [[{}, '未检测'], [{check_status:'connected'}, '连接正常'], [{check_status:'rejected'}, '检查异常'], [{configured:false}, '待配置'], [{enabled:false,check_status:'connected'}, '已停用']]) assert.equal(api.sourceState({...source,...change}).label,label);
  const html = render(source);
  assert.match(html, /关联任务/); assert.match(html, />2<\/span> 个/);
  assert.match(html, /交易日历采集/); assert.match(html, /Trading Calendar Sync/); assert.match(html, /create_type=private_task_key/);
  assert.doesNotMatch(html.replace(/<[^>]*>/g, ''), /private_task_key|must-not-display/);
  assert.match(html, /失败/); assert.match(html, /尚未检查/);
  assert.match(render({ ...source, enabled: false, capabilities: [] }), /此数据源尚无已注册/);
});
test('timestamps use Shanghai', () => {
  assert.equal(sourceTime(null), '尚未检查'); assert.equal(sourceTime('bad'), '时间未知'); assert.match(sourceTime('2026-09-08T16:00:00Z'), /09\/09 00:00/);
});
test('requests carry auth, current version and exact draft without caching', async () => {
  const originalFetch = global.fetch, originalWindow = global.window, calls = [];
  global.window = { sessionStorage: { getItem: () => 'fixture-login-token' } };
  global.fetch = async (path, options) => { calls.push({path,options}); return {ok:true,json:async()=>source}; };
  try {
    const draft = api.sourceDraft(source);
    await api.listDataSources(); await api.testDataSource(source,draft); await api.saveDataSource(source,draft); await api.setDataSourceEnabled(source,false);
    assert.equal(calls[1].path,'/api/data-sources/fixture%2Fsource/test');
    assert.deepEqual(JSON.parse(calls[2].options.body),{version:7,fields:draft}); assert.deepEqual(JSON.parse(calls[3].options.body),{version:7,enabled:false});
    for (const {options} of calls) { assert.equal(options.cache,'no-store'); assert.equal(options.headers.Authorization,'Bearer fixture-login-token'); }
  } finally { global.fetch=originalFetch; global.window=originalWindow; }
});
test('conflicts remain actionable while raw server errors stay hidden', async () => {
  const originalFetch = global.fetch;
  try {
    global.fetch=async()=>({ok:false,status:409,json:async()=>({detail:{message:'配置已更新，请刷新。',field:'endpoint'}})});
    await assert.rejects(api.saveDataSource(source,{}),e=>e.status===409&&e.field==='endpoint');
    global.fetch=async()=>({ok:false,status:502,json:async()=>{throw new Error('private trace');}});
    await assert.rejects(api.listDataSources(),e=>e.status===502&&!e.message.includes('private trace'));
    global.fetch=async()=>({ok:false,status:401,json:async()=>({detail:'Unauthorized'})});
    await assert.rejects(api.listDataSources(),e=>e.status===401);
    global.fetch=async()=>{throw new TypeError('private network detail');};
    await assert.rejects(api.saveDataSource(source,{}),e=>e.status===0&&e.message.includes('不要重复提交'));
  } finally { global.fetch=originalFetch; }
});
test('caller cancellation remains recognizable', async () => {
  const originalFetch=global.fetch;
  global.fetch=async(_path,options)=>{if(options.signal.aborted)throw new DOMException('','AbortError');};
  try { const controller=new AbortController(); controller.abort(); await assert.rejects(api.listDataSources(controller.signal),{name:'AbortError'}); }
  finally { global.fetch=originalFetch; }
});
