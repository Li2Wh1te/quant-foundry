import {useEffect,useRef,useState} from 'react';
import {Link,useLocation,useSearchParams} from 'react-router-dom';
import {FoundationApiError,foundationApi} from '../api/foundation';
import {Select} from '../components/controls/Select';
import {EvidencePanel} from './DataAssetsPage';

type Batch={id:string;created_at:string;start:string;end:string;source_versions:number;
  controls:{pause_a:boolean;pause_b:boolean;allow_publish:boolean};
  stages:Record<'A'|'B',{works:number;completed:number;waiting_publication:number;failed:number}>;
  reconciliation:{status:string;checked_at:string}|null;
  works:{id:string;kind:string;status:string;processed:number;total:number|null}[];
  releases:{id:string;status:string;work_id:string;published_at:string|null}[]};
type Page={items:Batch[];next_cursor:string|null};
type Reconciliation={status:string;total:number|null;next_cursor:string|null;checked_at?:string;
  items:{stage:string;key:string;state:string;reason_code:string;publication_status?:string;subject?:string;business_date?:string;source_report_key?:string;business_key?:string}[]};
const labels:Record<string,string>={explained:'全部差异已有解释',unexplained:'存在未解释差异',pending:'待处理',restricted:'受限',
  not_reconciled:'尚未对账',stale:'输入或执行状态已变化，需重新对账',queued:'等待处理',running:'运行中',succeeded:'已完成',failed:'失败',
  cancelled:'已取消',dependency_missing:'依赖缺失',superseded:'依据已更新',awaiting_publication:'已封存待发布',
  draft:'尚未封存',sealed:'已封存待发布',published:'已正式发布',abandoned:'已放弃',source:'来源到候选',candidate:'候选到决策',decision:'决策到清单',release:'发布清单',
  SOURCE_VALUE_MISMATCH:'来源事实与候选值不一致',SOURCE_CANDIDATE_MATCH:'来源对象与候选逐项匹配',CANDIDATE_MANIFEST_MISMATCH:'候选数量或摘要不一致',CANDIDATE_QUARANTINED:'候选质量或身份未满足要求',
  GOVERNANCE_DECIDED:'已有固定治理结论',HISTORICAL_INPUT_NOT_SELECTED:'历史输入已保留，本次未选用',GOVERNANCE_PENDING:'候选等待治理',
  NORMALIZATION_PENDING:'来源等待标准化',PUBLICATION_NOT_SEALED:'治理尚未形成完整清单',DECISION_MANIFEST_MATCH:'治理决策已纳入清单',
  DECISION_MISSING:'清单缺少治理决策',SEMANTIC_DEPENDENCY_MISSING:'标准语义证据不足，暂未准入',OUT_OF_SCOPE:'不属于本次业务范围'};
const display=(key:string)=>labels[key]||(key.startsWith('NORMALIZATION_')?'标准化尚未完成':'需要核验');
const stamp=(value:string)=>new Date(value).toLocaleString('zh-CN',{hour12:false});

/** Read-only batch navigation. Cursors and ledger records never enter the URL;
 * only the selected immutable batch/work context survives navigation. */
export function FoundationBatchLedger({datasetId,refresh,onError}:{datasetId:string;refresh:number;onError:(error:unknown)=>string}){
  const [params,setParams]=useSearchParams(),location=useLocation();
  const [page,setPage]=useState<Page|null>(null),[batch,setBatch]=useState<Batch|null>(null);
  const [busy,setBusy]=useState(false),[error,setError]=useState(''),[index,setIndex]=useState(0),[cursors,setCursors]=useState<(string|null)[]>([null]);
  const [size,setSize]=useState(50),[reconciliation,setReconciliation]=useState<Reconciliation|null>(null),[panelError,setPanelError]=useState('');
  const [panelOpen,setPanelOpen]=useState(false),[panelBusy,setPanelBusy]=useState(false),[rIndex,setRIndex]=useState(0),[rCursors,setRCursors]=useState<(string|null)[]>([null]);
  const lastSelection=useRef('');
  const controller=useRef<AbortController|null>(null),selected=params.get('batch')||'';
  useEffect(()=>{setIndex(0);setCursors([null]);setPage(null);setBatch(null);},[datasetId]);
  useEffect(()=>{const c=new AbortController();setBusy(true);setError('');
    foundationApi<Page>(`/datasets/${datasetId}/batches?limit=${size}${cursors[index]?`&cursor=${encodeURIComponent(cursors[index]!)}`:''}`,c.signal)
      .then(value=>{if(c.signal.aborted)return;setPage(value);setCursors(old=>{const next=old.slice(0,index+1);if(value.next_cursor)next.push(value.next_cursor);return next;});})
      .catch(e=>{if(!c.signal.aborted){setError(onError(e));if(e instanceof FoundationApiError&&[409,410].includes(e.status)){setIndex(0);setCursors([null]);}}}).finally(()=>{if(!c.signal.aborted)setBusy(false);});
    return()=>c.abort();},[datasetId,index,size,refresh,onError]);
  useEffect(()=>{controller.current?.abort();setPanelOpen(false);setReconciliation(null);setPanelError('');setRIndex(0);setRCursors([null]);
    if(!selected){setBatch(null);lastSelection.current='';return;}const c=new AbortController();if(lastSelection.current!==selected)setBatch(null);lastSelection.current=selected;
    foundationApi<Batch>(`/batches/${selected}?dataset_id=${encodeURIComponent(datasetId)}`,c.signal).then(value=>{if(!c.signal.aborted)setBatch(value);})
      .catch(e=>{if(!c.signal.aborted)setError(onError(e));});return()=>c.abort();},[selected,datasetId,refresh,onError]);
  useEffect(()=>()=>controller.current?.abort(),[]);
  function select(id:string){const next=new URLSearchParams(params);id?next.set('batch',id):next.delete('batch');setParams(next);}
  function target(key:string,id:string){const next=new URLSearchParams(params);next.set(key,id);next.set('view',key==='work'?'process':'versions');return `${location.pathname.replace(/\/processing$/, '')}?${next}`;}
  async function inspect(targetIndex=0){controller.current?.abort();const c=new AbortController();controller.current=c;setPanelOpen(true);setPanelBusy(true);setPanelError('');
    try{const value=await foundationApi<Reconciliation>(`/batches/${selected}/reconciliation?limit=${size}${rCursors[targetIndex]?`&cursor=${encodeURIComponent(rCursors[targetIndex]!)}`:''}`,c.signal);
      if(!c.signal.aborted){setReconciliation(value);setRIndex(targetIndex);setRCursors(old=>{const next=old.slice(0,targetIndex+1);if(value.next_cursor)next.push(value.next_cursor);return next;});}}
    catch(e){if(!c.signal.aborted){setPanelError(onError(e));if(e instanceof FoundationApiError&&[409,410].includes(e.status)){setRIndex(0);setRCursors([null]);}}}finally{if(!c.signal.aborted)setPanelBusy(false);}}
  function close(){controller.current?.abort();setPanelOpen(false);setPanelBusy(false);}
  return <section className="qf-assets-sheet" aria-label="业务批次与对账" aria-busy={busy}>
    <h2>业务批次与对账</h2><p>批次记录与当前正式资产分别展示。差异已有解释，也可能仍有隔离数据或待发布结果。</p>
    {error&&<p role="alert" className="qf-assets-error">{error}{page?' 已保留上次读取的批次列表。':''}</p>}
    {!page&&!error&&<p role="status">正在读取批次…</p>}
    {page&&<><div className="qf-assets-controls"><label>业务批次<Select value={selected} onChange={e=>select(e.target.value)}><option value="">请选择批次</option>
      {selected&&!page.items.some(b=>b.id===selected)&&<option value={selected}>{batch?stamp(batch.created_at):'当前所选批次'} · {selected.slice(0,8)}</option>}
      {page.items.map(b=><option key={b.id} value={b.id}>{stamp(b.created_at)} · {b.id.slice(0,8)}</option>)}</Select></label>
      <label>每页批次数<Select value={String(size)} onChange={e=>{setSize(Number(e.target.value));setIndex(0);setCursors([null]);setRCursors([null]);setRIndex(0);close();}}>{[20,50,100].map(n=><option key={n} value={n}>{n}</option>)}</Select></label></div>
      {!page.items.length&&<p>当前范围尚未登记业务批次，既有正式数据仍可正常读取。</p>}
      <div className="qf-assets-controls"><button className="qfo-secondary-btn" disabled={busy||index===0} onClick={()=>setIndex(v=>v-1)}>上一页批次</button><span>第 {index+1} 页</span><button className="qfo-secondary-btn" disabled={busy||!page.next_cursor} onClick={()=>setIndex(v=>v+1)}>下一页批次</button></div></>}
    {batch&&<><h3>所选批次</h3><p>{batch.start} 至 {batch.end} · 固定来源 {batch.source_versions} 个版本</p>
      <p>标准化：{batch.stages.A.completed} / {batch.stages.A.works} 个工作完成{batch.controls.pause_a?' · 已暂停':''}；治理：{batch.stages.B.completed} / {batch.stages.B.works} 个工作完成{batch.controls.pause_b?' · 已暂停':''}，{batch.stages.B.waiting_publication} 个待发布。</p>
      <p>标准化失败或依赖缺失 {batch.stages.A.failed} 个；治理失败或依赖缺失 {batch.stages.B.failed} 个。</p>
      <p>对账：{display(batch.reconciliation?.status||'not_reconciled')}{batch.reconciliation?` · ${stamp(batch.reconciliation.checked_at)}`:''}。发布门槛：{batch.controls.allow_publish?'已批准本批次发布':'仅影子处理，未批准发布'}。</p>
      <button className="qfo-secondary-btn" onClick={()=>inspect()}>查看逐项对账</button>
      <h3>关联工作</h3>{batch.works.length?<ul>{batch.works.map(w=><li key={w.id}><Link to={target('work',w.id)}>{w.kind==='A'?'标准化':'治理'} · {w.id.slice(0,8)}</Link> · {display(w.status)} · 已提交 {w.processed} / {w.total??'待确认'}</li>)}</ul>:<p>本批次尚未登记执行工作。</p>}
      <h3>本批次产出</h3>{batch.releases.length?<ul>{batch.releases.map(r=><li key={r.id}>{r.status==='published'?<Link to={target('release',r.id)}>正式版本 {r.id.slice(0,8)}</Link>:<code>{r.id.slice(0,8)}</code>} · {display(r.status)}</li>)}</ul>:<p>本批次尚无发布产出。</p>}</>}
    {panelOpen&&<EvidencePanel title="逐项对账" wide onClose={close}>
      {panelError&&<p role="alert" className="qf-assets-error">{panelError}<button className="qfo-secondary-btn" disabled={panelBusy} onClick={()=>inspect(0)}>重新读取对账</button></p>}{panelBusy&&<p role="status">正在读取对账证据…</p>}
      {reconciliation&&<><p>{display(reconciliation.status)} · {reconciliation.total==null?'尚未生成对账记录':`${reconciliation.total} 条核对结论`}</p>
        {reconciliation.checked_at&&<p>证据生成于 {stamp(reconciliation.checked_at)}；分页固定本次证据。</p>}
        {reconciliation.items.map(i=><section key={i.stage+i.key}><h3>{display(i.stage)} · {display(i.state)}</h3><p>{display(i.reason_code)}</p>{(i.subject||i.business_date||i.source_report_key)&&<p>{i.subject} · {i.business_date||i.source_report_key}</p>}<details><summary>查看核对标识</summary><p><code>{i.business_key||i.key}</code></p></details>{i.publication_status&&<p>{display(i.publication_status)}</p>}</section>)}
        <div className="qf-assets-controls"><button className="qfo-secondary-btn" disabled={panelBusy||rIndex===0} onClick={()=>inspect(rIndex-1)}>上一页对账</button><span>第 {rIndex+1} 页</span><button className="qfo-secondary-btn" disabled={panelBusy||!reconciliation.next_cursor} onClick={()=>inspect(rIndex+1)}>下一页对账</button></div></>}
    </EvidencePanel>}
  </section>;
}

type Intake={id:string;subject:string;selector:{start:string;end:string};paused:boolean;fixed_versions:number;
  last_full_completed_at:string|null;scan:{mode:string;status:string;seen:number;registered:number;message:string|null}|null};
export function FoundationIntakeLedger({datasetId,refresh,onError}:{datasetId:string;refresh:number;onError:(error:unknown)=>string}){
  const [result,setResult]=useState<{items:Intake[];next_cursor:string|null}|null>(null),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  const [page,setPage]=useState(0),[size,setSize]=useState(50),[cursors,setCursors]=useState<(string|null)[]>([null]);
  useEffect(()=>{setPage(0);setCursors([null]);setResult(null);},[datasetId]);
  useEffect(()=>{const c=new AbortController();setBusy(true);setError('');
    foundationApi<{items:Intake[];next_cursor:string|null}>(`/datasets/${datasetId}/intake?limit=${size}${cursors[page]?`&cursor=${encodeURIComponent(cursors[page]!)}`:''}`,c.signal)
      .then(value=>{if(c.signal.aborted)return;setResult(value);setCursors(old=>{const next=old.slice(0,page+1);if(value.next_cursor)next.push(value.next_cursor);return next;});})
      .catch(e=>{if(!c.signal.aborted){setError(onError(e));if(e instanceof FoundationApiError&&[409,410].includes(e.status)){setPage(0);setCursors([null]);}}}).finally(()=>{if(!c.signal.aborted)setBusy(false);});return()=>c.abort();
  },[datasetId,refresh,page,size,onError]);
  return <section className="qf-assets-sheet" aria-label="本地来源发现" aria-busy={busy}><h2>本地来源发现</h2><p>仅核对已批准范围的本地版本，不请求供应商。近期回看用于追平，完整周期用于补漏；固定来源不代表已正式发布。</p>
    {error&&<p className="qf-assets-error" role="alert">{error}{result?' 已保留上次台账。':''}</p>}{!result&&!error&&<p role="status">正在读取来源台账…</p>}
    {result&&<>{!result.items.length?<p>当前数据集尚未登记持续发现范围。既有来源与正式数据保持可用。</p>:<div className="qf-assets-scroll"><table><thead><tr><th>来源标的与范围</th><th>已固定版本</th><th>本周期</th><th>完整核对</th></tr></thead><tbody>{result.items.map(i=><tr key={i.id}><td>{i.subject}<small>{i.selector.start} 至 {i.selector.end}</small>{i.paused&&<small>发现已暂停</small>}</td><td>{i.fixed_versions} 个版本</td><td>{i.scan?<>{i.scan.mode==='full'?'完整核对':'近期回看'} · {i.scan.status==='completed'?'已完成':display(i.scan.status)}<small>已核对 {i.scan.seen} 个，新登记 {i.scan.registered} 个</small>{i.scan.message&&<small>{i.scan.message}</small>}</>:'尚未开始'}</td><td>{i.last_full_completed_at?stamp(i.last_full_completed_at):'尚无完整周期'}</td></tr>)}</tbody></table></div>}
      <div className="qf-assets-controls"><label>每页来源范围<Select value={String(size)} onChange={e=>{setSize(Number(e.target.value));setPage(0);setCursors([null]);}}>{[20,50,100].map(n=><option key={n} value={n}>{n}</option>)}</Select></label><button className="qfo-secondary-btn" disabled={busy||page===0} onClick={()=>setPage(v=>v-1)}>上一页来源</button><span>第 {page+1} 页</span><button className="qfo-secondary-btn" disabled={busy||!result.next_cursor} onClick={()=>setPage(v=>v+1)}>下一页来源</button></div></>}
  </section>;
}
