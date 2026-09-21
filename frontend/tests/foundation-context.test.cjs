/* Real React rendering; these fixtures contain no provider or production data. */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const ts=require('typescript');
for(const extension of ['.ts','.tsx'])require.extensions[extension]=(module,filename)=>module._compile(ts.transpileModule(fs.readFileSync(filename,'utf8'),{
  compilerOptions:{jsx:ts.JsxEmit.ReactJSX,module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022},fileName:filename
}).outputText,filename);
require.extensions['.css']=()=>{};
global.__QF_VERSION__=fs.readFileSync(require('node:path').join(__dirname,'../../VERSION'),'utf8').trim();
const {createElement}=require('react');
const {renderToStaticMarkup}=require('react-dom/server');
const {DatasetContext}=require('../src/pages/DataAssetsPage.tsx');
const fixed={current_release:'fixed-multi-input-release',business_as_of:'2026-06-09',published_at:null,source_observed_at:null,
  normalization_delay_seconds:null,publication_delay_seconds:null,pending_governance:false,candidate_records:99,official_keys:3};
const render=dataset=>renderToStaticMarkup(createElement(DatasetContext,{dataset}));
test('settled multi-input and unselected candidates do not show a pending warning',()=>{
  const html=render(fixed);
  assert.match(html,/fixed-multi-input-release/);
  assert.doesNotMatch(html,/最新候选尚未形成当前正式发布/);
  assert.match(html,/待确认/);
});
test('a newly unprocessed input shows pending without replacing the fixed official release',()=>{
  const html=render({...fixed,pending_governance:true});
  assert.match(html,/最新候选尚未形成当前正式发布/);
  assert.match(html,/fixed-multi-input-release/);
});
