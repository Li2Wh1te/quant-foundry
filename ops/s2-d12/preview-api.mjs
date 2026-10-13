/** Artificial loopback preview only; never installed in the production app. */
import http from 'node:http';
import { responseFor as detailResponse } from '../s2-d11/mock-api.mjs';
const run = { id:'artificial-run',task_id:'artificial-task',task_name:'人工更新失败样本',task_type:'fixture',
  task_type_name:'人工更新',task_type_english_name:'Fixture',source_key:'tushare',status:'failed',
  created_at:'2026-10-13T00:00:00Z',started_at:'2026-10-13T00:00:00Z',finished_at:'2026-10-13T00:00:03Z',duration_seconds:3 };
const operations = { generated_at:'2026-10-13T08:00:00Z',timezone:'Asia/Shanghai',day_start:'2026-10-12T16:00:00Z',day_end:'2026-10-13T16:00:00Z',
  metrics:{configured_sources:0,total_sources:1,active_tasks:0,queued_runs:0,running_runs:0,today_runs:3,today_succeeded:1,attention_tasks:1},
  sources:[{key:'tushare',name:'人工隔离样本 · 非真实运营',configured:false,connection_status:'not_checked',active_tasks:0,last_success_at:null}],
  recent_runs:[run],attention:[{run,retrying:false}] };
const summary = {catalog_key:'etf-current',total_instruments:7,listed_count:null,exchanges:['SSE'],first_list_date:'2020-01-01',latest_list_date:'2026-01-02',
  meta:{read_token:'a'.repeat(64),read_at:'2026-10-13T08:00:00Z',business_as_of:null,scope:'人工目录全部业务对象',
    source_labels:['人工夹具 · 模拟接口（非真实行情）'],coverage:{verified:false,state:'unknown',missing_dates:null,note:'未核验业务覆盖'},limitations:[]} };

export { operations, summary };
export function fixtureResponse(path, method='GET') {
  const url = new URL(path, 'http://127.0.0.1');
  if (method === 'GET' && url.pathname === '/api/admin/overview') return [200, operations];
  if (method === 'GET' && url.pathname === '/api/admin/market/overview') return [200, summary];
  return detailResponse(path, method);
}
if (process.argv[1]?.endsWith('/s2-d12/preview-api.mjs')) http.createServer((req,res) => {
  const [status,body]=fixtureResponse(req.url,req.method);
  res.writeHead(status, {'Content-Type':'application/json','Cache-Control':'no-store'});
  res.end(body === null ? '' : JSON.stringify(body));
}).listen(8012,'127.0.0.1',()=>console.log('D12 artificial loopback preview on 8012'));
