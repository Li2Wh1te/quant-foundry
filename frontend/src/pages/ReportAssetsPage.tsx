import type { RecordDataset } from './RecordAssetsPage';
import { FoundationBatchLedger, FoundationIntakeLedger } from './FoundationBatchLedger';
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { RefreshCw } from 'lucide-react';
import { foundationApi, FoundationApiError, type Dataset, type DetailedProcess, type ReleaseItem } from '../api/foundation';
import { useAuth } from '../auth/AuthContext';
import { Select } from '../components/controls/Select';
import { EvidencePanel, References } from './DataAssetsPage';
import './DataAssets.css';

const ROOT='/admin/data-assets';
const ID='fund.holdings_report';
const labels:Record<string,string>={hold_ratio:'持仓比例',market_value:'持仓金额',period_change_ratio:'报告期增减比例',rank:'来源排名'};
const states:Record<string,string>={SUBJECT_UNAVAILABLE:'基金份额没有符合条件的正式报告',withdrawn:'已撤回',available:'满足请求',partial:'部分满足',unavailable:'不满足请求',unknown:'证据不足',succeeded:'已完成',failed:'失败',running:'运行中',awaiting_publication:'已封存待发布',queued:'等待处理',quarantined:'已隔离',ready:'候选合格',blocked:'已阻断',gap:'缺口',current_issue:'当前问题限制',REPORT_MISSING:'缺少报告',FIELD_UNAVAILABLE:'字段缺项或单位未验证',PORTFOLIO_COMPLETENESS_UNKNOWN:'全组合完整性未验证'};
const stages:Record<string,string>={input:'读取本地来源',normalization:'清洗与标准化',quality:'校验候选',governance:'对账统一',publication:'正式发布'};
const time=(v:string|null|undefined)=>v?new Date(v).toLocaleString('zh-CN',{hour12:false}):'暂无记录';
interface ReportDataset {
  dataset:string;name:string;release_id:string|null;published_at:string|null;candidate_count:number;official_count:number;member_count:number;
  source_report_count:number;official_revision_count:number;quarantined_count:number;candidate_work_id:string|null;business_as_of:string|null;subjects:{instrument_id:string;code:string;name:string}[];range:{from:string|null;to:string|null};limitations:string[];as_of:string;
}
interface ReportRow {
  target_key:string;fund_share_id?:string;period_start?:string;period_end?:string;report_type?:string;member_count?:number;
  member_ordinal?:number;member_instrument_id?:string;source_member_code?:string;official_id:string;decision_id:string;
  hold_ratio?:string;market_value?:string|null;period_change_ratio?:string|null;rank?:number|null;
}
interface ReportResult {
  state:string;request_satisfied:boolean;release_id:string|null;checked_at:string;resolution_token?:string|null;expires_at?:number;
  items:ReportRow[];next_cursor?:string|null;has_more:boolean;requirements:{id:string;result:string;message:string}[];
  scope_summary:{expected_objects?:number|null;official_objects?:number;readable_objects?:number;member_count?:number};
  excluded:{target_key:string;reason:string}[];
}

/** Authorization changes clear every surface; temporary failures retain their
 * original timestamp. Each caller fences stale responses with AbortController. */
function useFailure(clear:()=>void) {
  const {logout}=useAuth(),navigate=useNavigate();
  return useCallback((e:unknown)=>{
    if(e instanceof FoundationApiError&&[401,403].includes(e.status)){clear();logout();navigate('/login',{replace:true});}
    return e instanceof Error?e.message:'读取失败，请重试。';
  },[clear,logout,navigate]);
}

export function FoundationCatalog(){
  const [params,setParams]=useSearchParams(),[rows,setRows]=useState<(Dataset|ReportDataset|RecordDataset)[]|null>(null),[error,setError]=useState(''),[revision,setRevision]=useState(0),[busy,setBusy]=useState(false);
  const clear=useCallback(()=>setRows(null),[]),failure=useFailure(clear);
  useEffect(()=>{const c=new AbortController();setBusy(true);setError('');foundationApi<{items:(Dataset|ReportDataset|RecordDataset)[]}>('/datasets',c.signal)
    .then(r=>{if(!c.signal.aborted)setRows(r.items);}).catch(e=>{if(!c.signal.aborted)setError(failure(e));}).finally(()=>{if(!c.signal.aborted)setBusy(false);});return()=>c.abort();},[revision,failure]);
  const search=params.get('search')||'',status=params.get('status')||'all';
  const published=(r:Dataset|ReportDataset|RecordDataset)=>'kind'in r?r.series.some(s=>!!s.release_id):'current_release'in r?r.current_release:r.release_id;
  const matches=rows?.filter(r=>(r.name+' '+r.dataset).toLowerCase().includes(search.toLowerCase())&&(status==='all'||(status==='published'?!!published(r):!published(r))));
  function update(k:string,v:string){const p=new URLSearchParams(params);p.set(k,v);setParams(p);}
  return <section className="qf-assets" aria-busy={busy}><header className="qf-assets-heading"><div><h1>数据资产</h1><p>查看正式数据的版本、覆盖范围与处理依据。</p></div><button className="qfo-secondary-btn" disabled={busy} onClick={()=>setRevision(r=>r+1)}><RefreshCw size={16}/>刷新</button></header>
    {error&&<p role="alert" className="qf-assets-error">{error}</p>}{!rows&&!error&&<p role="status">正在读取数据资产…</p>}
    <div className="qf-assets-controls"><label>搜索数据集<input value={search} onChange={e=>update('search',e.target.value)}/></label><label>发布状态<Select value={status} onChange={e=>update('status',e.target.value)}><option value="all">全部</option><option value="published">已正式发布</option><option value="pending">暂无正式发布</option></Select></label></div>
    {matches?.length?<div className="qf-assets-sheet qf-assets-scroll"><table><thead><tr><th>数据集</th><th>契约与口径</th><th>业务截至</th><th>正式记录</th><th>状态</th></tr></thead><tbody>{matches.map(r=><tr key={r.dataset}><td><Link to={`${ROOT}/${r.dataset}?${params}`}>{r.name}</Link></td><td>{'kind'in r?'1.0 · 来源观察记录':r.dataset===ID?'1.0 · 供应商报告范围':'1.0 · 未复权 · CNY'}</td><td>{r.business_as_of||('kind'in r&&published(r)?'资料观察版本':'暂无正式数据')}</td><td>{'kind'in r?`${r.official_count} 个对象`:'official_keys'in r?`${r.official_keys} 行`:`${r.official_count} 份报告 · ${r.member_count} 条成员`}</td><td>{published(r)?'已正式发布':('candidate_records'in r?r.candidate_records:r.candidate_count)?'有候选待治理':'暂无正式发布'}</td></tr>)}</tbody></table></div>:rows&&<p>没有匹配的数据集，请调整筛选条件。</p>}
    {rows?.[0]&&<p className="qf-assets-asof">资产摘要读取于 {time(rows[0].as_of)}。来源、候选与正式数量分别统计。</p>}</section>;
}

export function ReportAssetsPage(){
  const [params,setParams]=useSearchParams(),[dataset,setDataset]=useState<ReportDataset|null>(null),[error,setError]=useState(''),[revision,setRevision]=useState(0),[loading,setLoading]=useState(false);
  const [result,setResult]=useState<ReportResult|null>(null),[busy,setBusy]=useState(false),[page,setPage]=useState(0),[cursors,setCursors]=useState<(string|null)[]>([null]),[hasRead,setHasRead]=useState(false);
  const [works,setWorks]=useState<DetailedProcess[]>([]),[releases,setReleases]=useState<ReleaseItem[]>([]),[panel,setPanel]=useState<{title:string;content:ReactNode;wide?:boolean}|null>(null),[releaseNext,setReleaseNext]=useState<string|null>(null),[workNext,setWorkNext]=useState<string|null>(null),[historyBusy,setHistoryBusy]=useState(false),[historyLoaded,setHistoryLoaded]=useState(false),[historyLoading,setHistoryLoading]=useState(false),[historyError,setHistoryError]=useState('');
  const active=useRef<AbortController|null>(null),aux=useRef<AbortController|null>(null),[clock,setClock]=useState(Date.now());
  const clear=useCallback(()=>{active.current?.abort();aux.current?.abort();setDataset(null);setResult(null);setWorks([]);setReleases([]);setPanel(null);},[]),failure=useFailure(clear);
  const view=params.get('view')||'data',selected=params.get('report')||'',release=params.get('release')||dataset?.release_id||'latest';
  const subjects=params.has('subjects')?(params.get('subjects')||'').split(',').filter(Boolean):dataset?.subjects.map(s=>s.instrument_id)||[];
  const from=params.get('from')||dataset?.range.from||'',to=params.get('to')||dataset?.range.to||'',kind=params.get('kind')||'quarter';
  const fields=(params.has('fields')?(params.get('fields')||''):'hold_ratio').split(',').filter(Boolean),partial=params.get('partial')==='true',complete=params.get('complete')==='true',portfolio=params.get('portfolio')==='true';
  const timeMode=params.get('time')||'observed',freshness=params.get('freshness')||'';
  const size=[20,50,100].includes(Number(params.get('size')))?Number(params.get('size')):50;
  const intent=JSON.stringify([subjects,from,to,kind,fields,partial,complete,portfolio,selected,release,timeMode,freshness]);
  function update(k:string,v:string){const p=new URLSearchParams(params);p.set(k,v);if(!['view','work'].includes(k)){active.current?.abort();setResult(null);setBusy(false);}if(!['view','report','size','work'].includes(k))p.delete('report');setParams(p);}
  function request(){return {dataset_id:ID,contract_version:'1.0',profile_id:'default',semantic_series_id:'cn-fund-stock-holdings-provider-reported',subjects,business_range:{from,to},report_types:[kind],fields,scope_kind:'provider_reported',release,require_complete:complete,require_portfolio_complete:portfolio,allow_partial:partial,time_mode:timeMode,...(freshness!==''?{max_staleness_days:Number(freshness)}:{}),...(selected?{selected_report:selected}:{}),page_size:size};}
  useEffect(()=>{active.current?.abort();aux.current?.abort();setHistoryBusy(false);setResult(null);setHasRead(false);setPanel(null);setPage(0);setCursors([null]);setBusy(false);setError('');},[intent]);
  useEffect(()=>{const t=setInterval(()=>setClock(Date.now()),1000);return()=>clearInterval(t);},[]);
  useEffect(()=>{const c=new AbortController();setLoading(true);foundationApi<ReportDataset>(`/datasets/${ID}`,c.signal).then(d=>{if(!c.signal.aborted){setDataset(d);setError('');}}).catch(e=>{if(!c.signal.aborted)setError(failure(e));}).finally(()=>{if(!c.signal.aborted)setLoading(false);});return()=>c.abort();},[revision,failure]);
  useEffect(()=>{if(!dataset?.release_id||params.has('release'))return;const p=new URLSearchParams(params);p.set('release',dataset.release_id);setParams(p,{replace:true});},[dataset,params,setParams]);
  useEffect(()=>{const c=new AbortController();setPanel(null);setHistoryLoading(true);setHistoryError('');Promise.all([foundationApi<{items:ReleaseItem[];next_cursor:string|null}>(`/datasets/${ID}/releases`,c.signal),foundationApi<{items:DetailedProcess[];next_cursor:string|null}>(`/datasets/${ID}/processing`,c.signal)])
    .then(([r,w])=>{if(!c.signal.aborted){setReleases(r.items);setWorks(w.items);setReleaseNext(r.next_cursor);setWorkNext(w.next_cursor);setHistoryLoaded(true);}}).catch(e=>{if(!c.signal.aborted)setHistoryError(failure(e));}).finally(()=>{if(!c.signal.aborted)setHistoryLoading(false);});return()=>c.abort();},[revision,failure]);
  useEffect(()=>{const id=params.get('work');if(!id||works.some(w=>w.id===id))return;const c=new AbortController();
    foundationApi<DetailedProcess>(`/work/${id}`,c.signal).then(w=>{if(!c.signal.aborted){if(w.scope.dataset!==ID)throw new Error('所选工作不属于基金报告数据集。');setWorks(old=>old.some(x=>x.id===w.id)?old:[...old,w]);}}).catch(e=>{if(!c.signal.aborted)setError(failure(e));});return()=>c.abort();},[params.get('work'),works,failure]);
  useEffect(()=>()=>{active.current?.abort();aux.current?.abort();},[]);
  async function read(check:boolean,target=0){active.current?.abort();const c=new AbortController();active.current=c;setBusy(true);setError('');try{
    const body=check?request():{resolution_token:result?.resolution_token,cursor:cursors[target],page_size:size};
    const r=await foundationApi<ReportResult>(check?'/capability-checks':'/queries',c.signal,body);if(c.signal.aborted)return;setResult(r);setHasRead(!check);setPage(target);
    if(check)setCursors([null]);else if(r.next_cursor)setCursors(old=>[...old.slice(0,target+1),r.next_cursor!]);
  }catch(e){if(!c.signal.aborted){if(e instanceof FoundationApiError&&[409,410].includes(e.status)){setResult(null);setPanel(null);setCursors([null]);setPage(0);}setError(failure(e));}}finally{if(!c.signal.aborted)setBusy(false);}}
  async function evidence(row:ReportRow){aux.current?.abort();setHistoryBusy(false);const c=new AbortController();aux.current=c;try{const r=await foundationApi<Record<string,unknown>>(`/revisions/${row.official_id}/lineage`,c.signal);if(!c.signal.aborted)setPanel({title:'报告血缘',content:<References values={r}/>});}catch(e){if(!c.signal.aborted)setError(failure(e));}}
  useEffect(()=>{aux.current?.abort();setHistoryBusy(false);setPanel(null);},[params.get('work'),view]);
  async function moreHistory(kind:'releases'|'processing'){
    const next=kind==='releases'?releaseNext:workNext;if(!next)return;
    aux.current?.abort();const c=new AbortController();aux.current=c;setHistoryBusy(true);
    try{if(kind==='releases'){const r=await foundationApi<{items:ReleaseItem[];next_cursor:string|null}>(`/datasets/${ID}/releases?before=${next}`,c.signal);if(!c.signal.aborted){setReleases(old=>[...old,...r.items]);setReleaseNext(r.next_cursor);}}
      else{const r=await foundationApi<{items:DetailedProcess[];next_cursor:string|null}>(`/datasets/${ID}/processing?before=${next}`,c.signal);if(!c.signal.aborted){setWorks(old=>[...old,...r.items]);setWorkNext(r.next_cursor);}}}
    catch(e){if(!c.signal.aborted)setError(failure(e));}finally{if(aux.current===c)setHistoryBusy(false);}
  }
  async function inspect(after?:number){
    const id=works.find(w=>w.id===params.get('work')&&w.kind==='A')?.id||dataset?.candidate_work_id;if(!id)return;
    aux.current?.abort();setHistoryBusy(false);const c=new AbortController();aux.current=c;
    try{const r=await foundationApi<{items:{id:string;readiness:string;quality:{reasons?:string[];source_report_key?:string;member_count?:number}}[];next_occurrence:number|null}>('/candidate-inspections',c.signal,{work_id:id,page_size:20,...(after===undefined?{}:{after_occurrence:after})});
      if(!c.signal.aborted)setPanel({title:'报告候选诊断',content:<><p>仅供诊断，不是正式数据。工作：<code>{id}</code></p>{r.items.map(i=><article key={i.id}><h3>{i.quality.source_report_key||'报告范围未解析'}</h3><p>{states[i.readiness]||'待核验'} · {i.quality.member_count??'未知'} 条成员</p>{i.quality.reasons?.map(reason=><p key={reason}>{({FUND_IDENTITY_UNRESOLVED:'基金份额身份未确认',MEMBER_IDENTITY_UNRESOLVED:'持仓成员身份未确认',REPORT_PAGES_MISSING:'报告分页未收齐',REPORT_REQUEST_FAILED:'报告请求失败',REPORT_VALUE_INVALID:'核心比例无效',REPORT_DIRECTORY_MISSING:'缺少报告目录证据'} as Record<string,string>)[reason]||'报告证据未通过校验'} <code>{reason}</code></p>)}</article>)}{r.next_occurrence!==null&&<button className="qfo-secondary-btn" onClick={()=>inspect(r.next_occurrence!)}>下一页候选</button>}</>});
    }catch(e){if(!c.signal.aborted)setError(failure(e));}
  }
  async function compare(r:ReleaseItem,cursor?:string,comparisonPage=1){
    if(!r.parent_id)return;aux.current?.abort();setHistoryBusy(false);const c=new AbortController();aux.current=c;
    try{const changed=await foundationApi<{items:{target_key:string;period_start:string;period_end:string;kind:string;basis:{before:Record<string,unknown>|null;after:Record<string,unknown>|null}}[];next_cursor:string|null}>(`/datasets/${ID}/release-changes`,c.signal,{...request(),selected_report:undefined,previous_release:r.parent_id,current_release:r.id,page_size:100,...(cursor?{cursor}:{})});
      if(!c.signal.aborted)setPanel({title:'整份报告版本变化',wide:true,content:<><p>比较固定父版本与所选版本。成员按整份报告替换；受当前问题限制的内容不参与比较。</p>{changed.items.map(i=><section key={i.target_key}><p>{i.period_start} 至 {i.period_end} · {({added:'新增报告',whole_object_changed:'整份报告内容或成员依据变化',basis_changed:'治理依据变化',inherited:'沿用原报告',withdrawn:'已撤回',blocked:'已阻断',gap:'缺口',restricted_comparison:'当前限制，无法比较',absent:'对象不在新版本'} as Record<string,string>)[i.kind]||'证据不足'}</p><details><summary>本份报告的实际依据</summary><h3>原版依据</h3><References values={i.basis.before||{说明:'原版不存在该报告'}}/><h3>新版依据</h3><References values={i.basis.after||{说明:'新版不存在该报告'}}/></details></section>)}<p>第 {comparisonPage} 页</p>{comparisonPage>1&&<button className="qfo-secondary-btn" onClick={()=>compare(r)}>返回第一页</button>}{changed.next_cursor&&<button className="qfo-secondary-btn" onClick={()=>compare(r,changed.next_cursor!,comparisonPage+1)}>下一页变化</button>}</>});
    }catch(e){if(!c.signal.aborted)setError(failure(e));}
  }
  const expired=!!result?.expires_at&&clock>=result.expires_at*1000;
  const work=params.get('work')?works.find(w=>w.id===params.get('work')):works[0];
  return <section className="qf-assets" aria-busy={loading||busy}><header className="qf-assets-heading"><div><Link to={`${ROOT}?search=${encodeURIComponent(params.get('search')||'')}&status=${params.get('status')||'all'}`}>数据资产</Link><h1>基金持仓报告</h1><p>按完整报告查看持仓成员、正式版本与处理依据。</p></div><button className="qfo-secondary-btn" disabled={loading} onClick={()=>setRevision(r=>r+1)}><RefreshCw size={16}/>刷新</button></header>
    {error&&<p role="alert" className="qf-assets-error">{error}</p>}{!dataset&&!error&&<p role="status">正在读取报告资产…</p>}
    <nav className="qf-assets-tabs" aria-label="数据集视图">{[['data','概况与数据'],['fields','字段与口径'],['versions','来源与版本'],['process','处理过程']].map(([v,n])=><Link key={v} aria-current={view===v?'page':undefined} to={`?${new URLSearchParams({...Object.fromEntries(params),view:v})}`}>{n}</Link>)}</nav>
    {dataset&&<><div className="qf-assets-metrics">{[['候选报告版本',dataset.candidate_count,'份'],['隔离候选',dataset.quarantined_count,'份'],['正式报告对象',dataset.official_count,'份'],['正式持仓成员',dataset.member_count,'条']].map(([n,v,u])=><div key={String(n)}><span>{n}</span><strong>{v}</strong><small>{u}</small></div>)}</div><p>已固定来源报告 {dataset.source_report_count} 份 · 正式报告修订累计 {dataset.official_revision_count} 份</p><p>当前正式版本：<code>{dataset.release_id||'暂无正式发布'}</code></p><p>报告截至：{dataset.business_as_of||'暂无正式数据'} · 发布于 {time(dataset.published_at)}</p><div className="qf-assets-note">{dataset.limitations.map(x=><p key={x}>{x}</p>)}</div></>}
    {view==='data'&&dataset&&<section className="qf-assets-sheet"><h2>检查与正式读取</h2><p>所选版本：<code>{release==='latest'?'暂无正式发布':release}</code></p>
      <fieldset><legend>基金份额</legend><div className="qf-assets-checks">{dataset.subjects.map(s=><label key={s.instrument_id}><input type="checkbox" checked={subjects.includes(s.instrument_id)} onChange={e=>update('subjects',(e.target.checked?[...subjects,s.instrument_id]:subjects.filter(i=>i!==s.instrument_id)).join(','))}/>{s.code}</label>)}</div>{!dataset.subjects.length&&<p>尚无已确认身份的报告主体。</p>}</fieldset>
      <div className="qf-assets-form"><label>报告期结束日期从<input type="date" value={from} onChange={e=>update('from',e.target.value)}/></label><label>报告期结束日期至<input type="date" value={to} onChange={e=>update('to',e.target.value)}/></label><label>报告类型<Select value={kind} onChange={e=>update('kind',e.target.value)}><option value="quarter">季度报告</option><option value="semiannual">半年度报告</option><option value="annual">年度报告</option></Select></label></div>
      <div className="qf-assets-form"><label>时间语义<Select value={timeMode} onChange={e=>update('time',e.target.value)}><option value="observed">按本地已观察版本读取</option><option value="latest_known">要求历史已知时点</option><option value="strict_public_pit">要求历史公开时点</option></Select></label><label>报告截至距查询结束日天数上限（可选）<input type="number" min="0" step="1" value={freshness} onChange={e=>update('freshness',e.target.value)}/></label></div>
      {selected&&<p>当前查看单份报告的成员。修改基金、日期或字段将返回报告列表。</p>}
      <fieldset><legend>成员字段</legend><div className="qf-assets-checks">{Object.entries(labels).map(([k,n])=><label key={k}><input type="checkbox" checked={fields.includes(k)} onChange={e=>update('fields',(e.target.checked?[...fields,k]:fields.filter(f=>f!==k)).join(','))}/>{n}</label>)}</div></fieldset>
      <div className="qf-assets-checks"><label><input type="checkbox" checked={complete} onChange={e=>update('complete',String(e.target.checked))}/>要求报告范围完整</label><label><input type="checkbox" checked={portfolio} onChange={e=>update('portfolio',String(e.target.checked))}/>要求全投资组合完整</label><label><input type="checkbox" checked={partial} onChange={e=>update('partial',String(e.target.checked))}/>允许读取已证明的完整报告子集</label></div>
      <div className="qf-assets-controls"><button className="qfo-primary-btn" disabled={busy||!subjects.length||!from||!to||from>to||!fields.length||(freshness!==''&&(!Number.isInteger(Number(freshness))||Number(freshness)<0))} onClick={()=>read(true)}>检查是否满足</button><button className="qfo-secondary-btn" disabled={busy||!result?.resolution_token||expired} onClick={()=>read(false)}>读取正式数据</button>{selected&&<button className="qfo-secondary-btn" onClick={()=>update('report','')}>返回报告列表</button>}</div>
      {expired&&<p role="alert">检查已过期，请重新检查原版本。</p>}{result&&<><h3>{states[result.state]}</h3><p>{result.request_satisfied?'原请求已满足。':'原请求未满足；部分读取不会改变此结论。'} 检查于 {time(result.checked_at)}</p>{result.requirements.map(r=><p key={r.id}>{r.message}（{r.result==='pass'?'通过':r.result==='unknown'?'证据不足':'未满足'}）</p>)}
        {result.items.length>0?<div className="qf-assets-scroll"><table><thead><tr>{selected?<><th>成员位置</th><th>来源代码</th>{fields.map(f=><th key={f}>{labels[f]}</th>)}</>:<><th>基金份额</th><th>报告期间</th><th>范围</th><th>成员数</th></>}<th>操作</th></tr></thead><tbody>{result.items.map(row=><tr key={selected?row.member_ordinal:row.target_key}>{selected?<><td>{row.member_ordinal!+1}</td><td>{row.source_member_code}</td>{fields.map(f=><td key={f}>{String(row[f as keyof ReportRow]??'缺项')}</td>)}</>:<><td>{dataset.subjects.find(s=>s.instrument_id===row.fund_share_id)?.code||row.fund_share_id}</td><td>{row.period_start} 至 {row.period_end}</td><td>供应商报告范围<br/>全组合未验证</td><td>{row.member_count}</td></>}<td>{!selected&&<button className="qfo-secondary-btn" onClick={()=>{active.current?.abort();setResult(null);const p=new URLSearchParams(params);p.set('report',row.target_key);if(row.fund_share_id)p.set('subjects',row.fund_share_id);setParams(p);}}>查看成员</button>}<button className="qfo-secondary-btn" onClick={()=>evidence(row)}>血缘</button></td></tr>)}</tbody></table></div>:<p>{!hasRead?'检查完成，点击“读取正式数据”查看结果。':result.request_satisfied&&selected?'该正式报告确认为0条持仓成员。':'本次没有可读取的正式结果，请查看缺口与限制。'}</p>}
        <div className="qf-assets-controls"><label>每页<Select value={String(size)} onChange={e=>{update('size',e.target.value);setResult(null);setPage(0);setCursors([null]);}}>{[20,50,100].map(n=><option key={n} value={n}>{n} 条</option>)}</Select></label><button className="qfo-secondary-btn" disabled={busy||page===0||expired} onClick={()=>read(false,page-1)}>上一页</button><span>第 {page+1} 页</span><button className="qfo-secondary-btn" disabled={busy||!result.next_cursor||expired} onClick={()=>read(false,page+1)}>下一页</button></div>
        {!!result.excluded.length&&<details open><summary>缺口与限制（{result.excluded.length} 项）</summary>{result.excluded.map(x=><p key={x.target_key}><code>{x.target_key}</code> · {states[x.reason]||'报告证据不足'}</p>)}</details>}</>}
    </section>}
    {view==='fields'&&<section className="qf-assets-sheet"><h2>字段与口径</h2><p>比例使用无量纲小数字符串，例如0.0431表示4.31%。排名为空时不按成员位置补齐。</p><p>持仓金额标准单位为人民币元，目前单位证据未闭合，正式值保持缺项。</p><p>报告期、来源观察时间、正式发布时间分别记录；不提供历史公开时点保证。每次最多查询100份报告。</p><p>完整收到一份报告，不代表完整披露基金投资组合。</p></section>}
    {view==='versions'&&<FoundationIntakeLedger datasetId={ID} refresh={revision} onError={failure}/>}
    {['versions','process'].includes(view)&&<FoundationBatchLedger datasetId={ID} refresh={revision} onError={failure}/>}
    {['versions','process'].includes(view)&&<>{historyError&&<p role="alert" className="qf-assets-error">{historyError}</p>}{historyLoading&&<p role="status">正在读取版本与处理记录…</p>}</>}
    {view==='versions'&&<section className="qf-assets-sheet"><h2>正式版本</h2>{dataset?.candidate_work_id&&<button className="qfo-secondary-btn" onClick={()=>inspect()}>查看候选诊断</button>}{historyLoaded&&!historyError&&!releases.length&&<p>暂无正式发布记录。</p>}{releases.map(r=><article key={r.id} className="qf-assets-version"><h3>{time(r.published_at)}</h3><code>{r.id}</code><div className="qf-assets-controls">{r.parent_id&&<button className="qfo-secondary-btn" onClick={()=>compare(r)}>与父版本比较</button>}<button className="qfo-secondary-btn" onClick={()=>{const p=new URLSearchParams(params);p.set('release',r.id);p.set('view','data');p.delete('report');setParams(p);}}>读取此版本</button><button className="qfo-secondary-btn" onClick={()=>{const p=new URLSearchParams(params);p.set('work',r.work_id);p.set('view','process');setParams(p);}}>查看处理依据</button></div></article>)}{releaseNext&&<button className="qfo-secondary-btn" disabled={historyBusy} onClick={()=>moreHistory('releases')}>更多历史版本</button>}</section>}
    {view==='process'&&<section className="qf-assets-sheet"><h2>处理过程</h2>{!works.length?(historyLoaded&&!historyError?<p>暂无报告处理工作；不根据采集成功推断底座完成。</p>:null):<><label>处理工作<Select value={work?.id||''} onChange={e=>update('work',e.target.value)}>{works.map(w=><option key={w.id} value={w.id}>{w.kind==='A'?'标准化':'治理'} · {states[w.status]||w.status} · {w.id.slice(0,8)}</option>)}</Select></label>{workNext&&<button className="qfo-secondary-btn" disabled={historyBusy} onClick={()=>moreHistory('processing')}>更多处理工作</button>}{work?.kind==='A'&&<button className="qfo-secondary-btn" onClick={()=>inspect()}>候选诊断</button>}{work&&<><p>已处理 {work.counters.processed} / {work.counters.total??'总数待确认'} 份报告，涉及 {work.counters.members??'未知'} 条成员；完成不等于请求可用。</p><ol className="qf-assets-steps">{work.steps.map(s=><li key={s.step}><h3>{stages[s.step]}</h3>{s.events.map(e=><p key={`${e.work_id||'legacy'}:${e.sequence}`}>{e.message}</p>)}{!s.events.length&&<p>记录不足</p>}<button className="qfo-secondary-btn" onClick={()=>setPanel({title:stages[s.step],content:<><p>{s.detail.processing}</p>{s.detail.input_works?.length?s.detail.input_works.map(i=><section key={i.work_id}><h3>标准化工作 {i.work_id.slice(0,8)}</h3><References values={i.input}/><References values={i.basis}/></section>):<References values={s.detail.basis}/>}<p>{s.detail.impact}</p></>})}>查看依据</button></li>)}</ol></>}</>}</section>}
    {dataset&&<p className="qf-assets-asof">资产摘要读取于 {time(dataset.as_of)}。报告数与成员数分别计量。</p>}
    {panel&&<EvidencePanel title={panel.title} wide={panel.wide} onClose={()=>{aux.current?.abort();setHistoryBusy(false);setPanel(null);}}>{panel.content}</EvidencePanel>}
  </section>;
}
