/* Real private strategy storage regression, called only in native UI mode. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
module.exports=async function({page,context,manifest,token,base,output,steps}){
 const headers={Authorization:'Bearer '+token};
 const get=async p=>{const r=await context.request.get(manifest.url+p,{headers});assert.equal(r.status(),200);return r.json()};
 const account=await get(`/api/admin/backtest-account-profiles/${manifest.legacy_account_id}`);
 const original=await get(`/api/admin/strategies/${manifest.legacy_strategy_id}/revisions/1`);
 assert.equal(original.id,manifest.legacy_revision_id);
 await page.goto(`${base}/admin/strategies/${manifest.legacy_strategy_id}`);
 const editor=page.getByRole('textbox',{name:'策略源码',exact:true});await editor.waitFor();
 assert.equal(await editor.inputValue(),original.source_code);
 await editor.fill(original.source_code+'\n# isolated D15 save regression\n');
 const saved=page.waitForResponse(r=>new URL(r.url()).pathname.endsWith('/draft')&&r.request().method()==='PATCH');
 await editor.press('Control+s');assert.equal((await saved).status(),200);
 assert.equal((await get(`/api/admin/strategies/${manifest.legacy_strategy_id}/revisions/1`)).source_code,original.source_code);
 steps.push('legacy source saved through browser; immutable published source preserved');

 // Complete a new SDK import with the editor's keyboard API list.
 await editor.fill('from quantfoundry.api import get_pr');await editor.press('Control+Space');
 await page.getByRole('listbox',{name:'S3 API 补全'}).waitFor();await editor.press('Tab');
 assert.equal(await editor.inputValue(),'from quantfoundry.api import get_price');
 const source='from quantfoundry.api import get_price\n\ndef initialize(context):\n    pass\n\ndef handle_data(context, data):\n    pass\n';
 await editor.fill(source);
 await page.getByRole('button',{name:'发布版本',exact:true}).click();await page.getByLabel('版本别名',{exact:true}).fill('隔离迁入 S3');
 const published=page.waitForResponse(r=>new URL(r.url()).pathname.endsWith('/publish')&&r.request().method()==='POST');
 await page.getByRole('button',{name:'确认发布',exact:true}).click();const response=await published;assert.equal(response.status(),201);const revision=await response.json();
 assert.equal(revision.revision_number,2);assert.notEqual(revision.id,original.id);assert.equal(revision.source_code,source);
 assert.deepEqual(await get(`/api/admin/strategies/${manifest.legacy_strategy_id}/revisions/1`),original);
 assert.deepEqual(await get(`/api/admin/backtest-account-profiles/${manifest.legacy_account_id}`),account);
 await page.screenshot({path:path.join(output,'strategy-migrated-revision.png')});steps.push('keyboard completion + S3 migration publishes a new revision; original revision/account unchanged');

 await page.getByRole('button',{name:'新建策略',exact:true}).first().click();
 await page.getByLabel('策略名称',{exact:true}).fill('D15 浏览器新 SDK 模板');
 const created=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/admin/strategies'&&r.request().method()==='POST');
 await page.getByRole('button',{name:'创建并进入编辑器',exact:true}).click();const createResponse=await created;assert.equal(createResponse.status(),201);const strategy=await createResponse.json();
 await page.waitForURL(url=>url.pathname===`/admin/strategies/${strategy.id}`);await editor.waitFor();
 assert.match(await editor.inputValue(),/from quantfoundry\.api import/);assert.match(await editor.inputValue(),/def initialize\(context\)/);assert.doesNotMatch(await editor.inputValue(),/def run\(/);
 const validated=page.waitForResponse(r=>new URL(r.url()).pathname.endsWith('/validate')&&r.request().method()==='POST');
 await page.getByRole('button',{name:'静态检查',exact:true}).click();const validation=await validated;assert.equal(validation.status(),200);assert.equal((await validation.json()).valid,true);
 await page.screenshot({path:path.join(output,'strategy-new-sdk-template.png')});steps.push('new strategy template imports installed S3 SDK; actual nonexecuting static validation');

 await page.goto(`${base}/admin/strategies/${manifest.legacy_strategy_id}/backtests?run=${manifest.legacy_run_id}`);
 await page.getByRole('heading',{name:'旧引擎历史只读',exact:true}).waitFor();assert.equal(await page.getByRole('button',{name:'重新运行',exact:true}).count(),0);
 await page.getByRole('link',{name:'查看原始历史结果',exact:true}).click();await page.waitForURL(url=>url.pathname===`/admin/backtest-runs/${manifest.legacy_run_id}/results`);
 await page.getByRole('heading',{name:'历史结果 · 只读',exact:true}).waitFor();await page.screenshot({path:path.join(output,'legacy-history-readonly.png')});
 const legacy=await get(`/api/admin/backtests/${manifest.legacy_run_id}/results/summary`);assert.equal(legacy.summary.value,'123456789.123456789012345678');assert.equal(legacy.summary.time_ns,'1767225600000000123');
 fs.writeFileSync(path.join(output,'strategy-regression.json'),JSON.stringify({production_executed:false,legacy_revision:original,new_revision:revision,account,legacy,new_strategy_id:strategy.id},null,2));
 steps.push('original history URL remains read-only with exact saved result and nanoseconds');
};
