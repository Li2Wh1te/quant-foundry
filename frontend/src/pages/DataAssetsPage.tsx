import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { RefreshCw } from "lucide-react";
import { useAuth } from "../auth/AuthContext";
import { foundationApi, FoundationApiError, type Dataset, type OfficialResult, type ProcessView, type Lineage } from "../api/foundation";
import "./DataAssets.css";
import { Select } from "../components/controls/Select";

const ROOT = "/admin/data-assets";
const names: Record<string,string> = { available:"满足请求",partial:"部分满足",unavailable:"不满足请求",unknown:"证据不足",queued:"等待处理",running:"运行中",succeeded:"已完成",failed:"失败",cancelled:"已取消",dependency_missing:"依赖缺失",superseded:"依据已更新",evidence_missing:"记录不足",fixed:"输入已固定",evaluated:"已评估",published:"已发布",not_applicable:"不适用",reused:"已复用" };
const steps: Record<string,string> = { input:"读取本地来源",normalization:"清洗与标准化",quality:"校验候选",governance:"对账统一",publication:"正式发布" };
const value = (v: number | null | undefined) => v == null ? "待确认" : v.toLocaleString("zh-CN");
const time = (v: string | null | undefined) => v ? new Date(v).toLocaleString("zh-CN", { hour12:false }) : "暂无记录";

/** All interactions are read-only. Selected releases and query results stay
 * bound together; abort guards prevent an old response replacing new intent. */
export function DataAssetsPage() {
  const { datasetId } = useParams();
  const location = useLocation();
  const processing = location.pathname.endsWith("/processing");
  const [params, setParams] = useSearchParams();
  const [dataset, setDataset] = useState<Dataset | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [result, setResult] = useState<OfficialResult | null>(null);
  const [queryBusy, setQueryBusy] = useState(false);
  const [queryError, setQueryError] = useState("");
  const [process, setProcess] = useState<ProcessView | null>(null);
  const [evidence, setEvidence] = useState<Lineage | null>(null);
  const [lineageError, setLineageError] = useState("");
  const queryController = useRef<AbortController | null>(null);
  const lineageController = useRef<AbortController | null>(null);
  const { logout } = useAuth();
  const navigate = useNavigate();
  const fields = params.get("fields") === "volume" ? "volume" : params.get("fields") === "turnover" ? "turnover" : "close";
  const selectedRelease = params.get("release") || dataset?.current_release;
  const selectedWork = params.get("work") || dataset?.work_ids[0];
  const handleError = useCallback((caught: unknown) => {
    if (caught instanceof FoundationApiError && [401,403].includes(caught.status)) {
      setDataset(null);setResult(null);setProcess(null);setEvidence(null);logout();navigate("/login",{replace:true});
    }
    return caught instanceof Error ? caught.message : "读取失败，请重试。";
  }, [logout,navigate]);
  useEffect(() => {
    const controller = new AbortController();setBusy(true);setError("");
    const path = datasetId ? `/datasets/${encodeURIComponent(datasetId)}` : "/datasets/market.bar.daily";
    foundationApi<Dataset>(path,controller.signal).then(data => { if(!controller.signal.aborted) setDataset(data); })
      .catch(e => { if(!controller.signal.aborted) setError(handleError(e)); })
      .finally(() => { if(!controller.signal.aborted) setBusy(false); });
    return () => controller.abort();
  }, [datasetId,refresh,handleError]);
  useEffect(() => {
    // Preserve fixed release selection even when a refresh observes a new head.
    if (datasetId && dataset?.current_release && !params.get("release")) {
      const next = new URLSearchParams(params);next.set("release",dataset.current_release);setParams(next,{replace:true});
    }
  }, [dataset,datasetId,params,setParams]);
  useEffect(() => {
    queryController.current?.abort();lineageController.current?.abort();
    setResult(null);setEvidence(null);setQueryBusy(false);setQueryError("");setLineageError("");
    return () => { queryController.current?.abort();lineageController.current?.abort(); };
  }, [fields,selectedRelease,datasetId,processing]);
  useEffect(() => {
    if(!processing || !selectedWork) { setProcess(null);return; }
    const controller = new AbortController();if(process?.id!==selectedWork)setProcess(null);setQueryError("");
    foundationApi<ProcessView>(`/work/${selectedWork}`,controller.signal).then(data => { if(!controller.signal.aborted)setProcess(data); })
      .catch(e => { if(!controller.signal.aborted)setQueryError(handleError(e)); });
    return () => controller.abort();
  }, [selectedWork,processing,refresh,handleError]);
  async function readOfficial() {
    if(!dataset || !selectedRelease || !dataset.range.from || !dataset.range.to)return;
    queryController.current?.abort();const controller=new AbortController();queryController.current=controller;
    setQueryBusy(true);setQueryError("");setEvidence(null);
    try {
      const data=await foundationApi<OfficialResult>("/queries",controller.signal,{
        dataset_id:dataset.dataset,contract_version:dataset.version,profile_id:dataset.profile,semantic_series_id:dataset.series,
        subjects:dataset.subjects.map(s=>s.instrument_id),business_range:dataset.range,
        fields:fields==="close"?["close"]:["close",fields],release:selectedRelease,require_complete:true,allow_partial:false,time_mode:"observed"
      });
      if(!controller.signal.aborted)setResult(data);
    } catch(e) { if(!controller.signal.aborted)setQueryError(handleError(e)); }
    finally { if(!controller.signal.aborted)setQueryBusy(false); }
  }
  async function readLineage(id: string) {
    lineageController.current?.abort();const controller=new AbortController();lineageController.current=controller;
    setEvidence(null);setLineageError("");
    try {const data=await foundationApi<Lineage>(`/revisions/${id}/lineage`,controller.signal);if(!controller.signal.aborted)setEvidence(data);}
    catch(e){if(!controller.signal.aborted)setLineageError(handleError(e));}
  }
  const detail = `${ROOT}/${encodeURIComponent(dataset?.dataset || "market.bar.daily")}`;
  const suffix = params.toString() ? `?${params}` : "";
  return <section className="qf-assets" aria-busy={busy}>
    <header className="qf-assets-heading"><div>{datasetId && <Link to={ROOT}>数据资产</Link>}<h1>{processing?"处理过程":datasetId?"ETF 未复权日线":"数据资产"}</h1><p>查看正式数据的版本、覆盖范围与处理依据。</p></div><button className="qfo-secondary-btn" disabled={busy} onClick={()=>setRefresh(n=>n+1)}><RefreshCw size={16}/>{busy?"读取中…":"刷新"}</button></header>
    {error && <div className="qf-assets-error" role="alert">{error}{dataset && " 当前保留上次读取内容。"}</div>}
    {!dataset && !error && <p role="status">正在读取数据资产…</p>}
    {dataset && <>
      {!datasetId ? <div className="qf-assets-sheet"><table><thead><tr><th>数据集</th><th>正式口径</th><th>业务截至</th><th>正式记录</th><th>状态</th></tr></thead><tbody><tr><td><Link to={detail}>{dataset.name}</Link><small>日线 · CNY</small></td><td>未复权</td><td>{dataset.business_as_of||"暂无正式数据"}</td><td>{value(dataset.official_keys)} 行</td><td>{dataset.current_release?"已正式发布":"暂无正式发布"}<small>成交量单位待验证</small></td></tr></tbody></table></div> : <>
        <nav className="qf-assets-tabs" aria-label="数据集视图"><Link aria-current={!processing?"page":undefined} to={detail+suffix}>概况与数据</Link><Link aria-current={processing?"page":undefined} to={`${detail}/processing${suffix}`}>处理过程</Link></nav>
        <div className="qf-assets-metrics">{[["来源记录",value(dataset.source_records)],["候选记录",value(dataset.candidate_records)],["正式业务键",value(dataset.official_keys)],["应有业务键",value(dataset.expected_business_keys)]].map(([label,v])=><div key={label}><span>{label}</span><strong>{v}</strong><small>行</small></div>)}</div>
        <div className="qf-assets-context">{dataset.pending_governance && <p>最新标准化工作尚未形成当前正式发布；来源和候选数量属于最新标准化工作，正式数量属于当前版本。</p>}<p>当前正式版本：<code>{dataset.current_release||"暂无正式发布"}</code></p><p>业务截至：{dataset.business_as_of||"暂无正式数据"} · 发布于 {time(dataset.published_at)}</p><p>来源观察：{time(dataset.source_observed_at)} · 来源至候选 {value(dataset.normalization_delay_seconds)} 秒 · 候选至正式 {value(dataset.publication_delay_seconds)} 秒</p></div>
        {!processing ? <>
          <div className="qf-assets-note">{dataset.limitations.map(s=><p key={s}>{s}</p>)}</div>
          <section className="qf-assets-sheet"><h2>固定范围正式读取</h2><p>{dataset.range.from||"待确认"} 至 {dataset.range.to||"待确认"} · {dataset.subjects.map(s=>s.code).join("、")||"身份范围待确认"}</p><p>所选版本：<code>{selectedRelease||"暂无正式发布"}</code></p>
            {selectedRelease && dataset.current_release!==selectedRelease && <p role="status">当前已出现新发布，本次读取仍固定原版本。</p>}
            <div className="qf-assets-controls"><label>读取字段<Select aria-label="读取字段" value={fields} onChange={e=>{const next=new URLSearchParams(params);next.set("fields",e.target.value);setParams(next);}}><option value="close">收盘价</option><option value="turnover">收盘价＋成交额</option><option value="volume">收盘价＋成交量</option></Select></label><button className="qfo-primary-btn" disabled={queryBusy||!selectedRelease||!dataset.subjects.length} onClick={readOfficial}>{queryBusy?"读取中…":"读取正式数据"}</button></div>
            {queryError && <p className="qf-assets-error" role="alert">{queryError}</p>}
            {result && <div><h3>{names[result.state]||"状态待核实"}</h3><p>正式可读 {result.scope_summary.currently_readable_keys} 行 / 应有 {value(result.scope_summary.expected_business_keys)} 行 · 检查于 {time(result.checked_at)}</p>{result.requirements.map(r=><p key={r.id}>{r.message}</p>)}
              {result.items.length>0 && <div className="qf-assets-scroll"><table><thead><tr><th>日期</th><th>标的</th><th>收盘价（元/份）</th>{fields!=="close"&&<th>{fields==="turnover"?"成交额（元）":"成交量（份）"}</th>}<th>依据</th></tr></thead><tbody>{result.items.map(row=><tr key={row.official_id}><td>{row.trade_date}</td><td>{dataset.subjects.find(s=>s.instrument_id===row.instrument_id)?.code||row.instrument_id}</td><td>{row.close}</td>{fields!=="close"&&<td>{fields==="turnover"?row.turnover:row.volume}</td>}<td><button className="qfo-secondary-btn" onClick={()=>readLineage(row.official_id)}>查看血缘</button></td></tr>)}</tbody></table></div>}
            </div>}
          </section>
          {lineageError&&<p role="alert" className="qf-assets-error">{lineageError}</p>}
          {evidence&&<section className="qf-assets-sheet" aria-label="血缘依据"><h2>血缘依据</h2><p>本地 Tushare 单源治理；多源比较不适用。成交量换算未证实。</p><dl>{[["正式修订",evidence.official_id],["治理决策",evidence.decision_id],["候选修订",evidence.candidate_id],["固定来源",evidence.source_ref_id],["来源内容摘要",evidence.source_hash],["身份绑定",evidence.binding_id],["质量评估",evidence.assessment_id]].map(([k,v])=><div key={k}><dt>{k}</dt><dd><code>{v}</code></dd></div>)}</dl></section>}
        </> : <section className="qf-assets-sheet"><h2>所选处理工作</h2>{dataset.work_ids.length===0?<p>尚无底座处理工作；来源已登记不代表完成标准化或正式发布。</p>:<>
          <label>工作记录<Select aria-label="工作记录" value={selectedWork || ""} onChange={e=>{const next=new URLSearchParams(params);next.set("work",e.target.value);setParams(next);}}>{dataset.work_ids.map((id,index)=><option key={id} value={id}>{dataset.work_summaries?.find(w=>w.id===id)?.label || `工作 ${index+1}`}</option>)}</Select></label>
          {queryError&&<p role="alert" className="qf-assets-error">{queryError}</p>}
          {process?<><p>{process.kind==="A"?"标准化":"治理发布"} · {names[process.status]||"状态待核实"} · 已提交 {value(process.counters.processed)} / {value(process.counters.total)} 行</p><p>本工作输出版本：<code>{process.output_releases.join("、")||"暂无正式输出"}</code></p><ol className="qf-assets-steps">{process.steps.map(step=><li key={step.step}><h3>{steps[step.step]} <span>{names[step.status]||"处理中"}</span></h3>{step.events.length?step.events.map(event=><p key={event.sequence}>{event.message}<small>{time(event.at)}</small></p>):<p>该工作没有此节点的已提交记录，不能据此判断已完成。</p>}</li>)}</ol><details><summary>固定执行依据</summary><dl>{[["来源引用",process.input_manifest.source_ref_id],["候选清单",process.input_manifest.candidate_manifest_id],["依赖清单",process.input_manifest.dependency_id],["执行版本",process.input_manifest.execution_id],["输入摘要",process.input_manifest.fingerprint]].map(([k,v])=><div key={k}><dt>{k}</dt><dd><code>{v||"不适用"}</code></dd></div>)}</dl></details></>:!queryError&&<p role="status">正在读取工作记录…</p>}
        </>}</section>}
      </>}
      <p className="qf-assets-asof">资产摘要读取于 {time(dataset.as_of)}。数量分别统计，不合并为成功率。</p>
    </>}
  </section>;
}
