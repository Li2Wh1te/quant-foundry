import { useEffect, useRef, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, Copy, Download, RefreshCw, X } from "lucide-react";
import { fetchBacktestAnalysisSummary, fetchRunWorkbench, getBacktestRun, isTerminalBacktestStatus, type WorkbenchRun } from "../api/backtestRuns";
import { listStrategyRevisions } from "../api/strategies";
import { EvidenceTable } from "../components/BacktestReport";
import { CreateRunDrawer } from "./backtests/CreateRunDrawer";
import { ResultChart } from "./backtests/ResultChart";
import { ResultTables } from "./backtests/ResultTables";
import { displayNumber, monthlyResults, readAllResults, RESULT_KINDS, type ResultRow } from "./backtests/resultData";
import { copyConfiguration, finite, METRICS, runConfig, STATUS } from "./backtests/workbench";
import { comparisonUrl } from "./backtests/comparisonData";
import "./backtests/Workbench.css";
import "./backtests/Result.css";
const sections = [["overview", "收益概览"], ["curve", "收益曲线"], ["risk", "风险与回撤"], ["monthly", "月度表现"], ["holding", "持仓与交易"], ["diagnostics", "运行诊断"], ["configuration", "冻结配置"]];
const metrics = [...METRICS.slice(0, 5), { key: "cumulative_fees", name: "累计费用", percent: false }];
const message = (e: unknown) => e instanceof Error ? e.message : "请求失败，请重试。";
/** A report has its own URL. Its source workspace is a navigation snapshot,
 * while every financial value is reloaded from the selected run's APIs. */
export function BacktestResultPage() {
  const { runId = "" } = useParams();
  return <Report key={runId} runId={runId}/>;
}
function Report({ runId }: { runId: string }) {
  const location = useLocation(), navigate = useNavigate();
  const origin = location.state as { from?: string; workbench?: any; comparisonReturn?: { url: string; state?: any } } | null;
  const comparisonReturn = origin?.comparisonReturn?.url?.match(/^\/admin\/backtest-compare(?:\?|$)/) ? origin.comparisonReturn : null;
  const back = origin?.from?.match(/^\/admin\/(backtest-runs|strategies\/[^/]+\/backtests)$/) ? origin.from : "/admin/backtest-runs";
  const [run, setRun] = useState<WorkbenchRun | null>(null), [alias, setAlias] = useState("");
  const [error, setError] = useState(""), [loading, setLoading] = useState(true), [refresh, setRefresh] = useState(0), [active, setActive] = useState("overview");
  const [summary, setSummary] = useState<Record<string, unknown> | null>(null);
  const [data, setData] = useState<Record<string, ResultRow[]>>({}), [dataErrors, setDataErrors] = useState<Record<string, string>>({}), [pending, setPending] = useState(false);
  const [copyOpen, setCopyOpen] = useState(false), [exporting, setExporting] = useState(false), [exportStatus, setExportStatus] = useState(""), [toast, setToast] = useState("");
  const exportController = useRef<AbortController | null>(null), generation = useRef(0);
  useEffect(() => () => exportController.current?.abort(), []);
  useEffect(() => { if (!toast) return; const timer = setTimeout(() => setToast(""), 4000); return () => clearTimeout(timer); }, [toast]);
  useEffect(() => {
    const controller = new AbortController(); setLoading(true); setError("");
    Promise.all([getBacktestRun(runId, controller.signal), fetchRunWorkbench({ search: runId, status: "", offset: 0 }, controller.signal)]).then(async ([detail, workspace]) => {
      if (controller.signal.aborted) return;
      const context = workspace.items.find(item => item.run_id === runId);
      const next = { strategy_id: null, strategy_name: null, revision_number: null, ...context, ...detail };
      setRun(next); setLoading(false);
      if (next.strategy_id) {
        try { const revisions = await listStrategyRevisions(next.strategy_id); if (!controller.signal.aborted) setAlias(revisions.find(revision => revision.id === next.strategy_revision_id)?.alias || "未命名版本"); }
        catch { if (!controller.signal.aborted) setAlias("版本别名暂不可用"); }
      }
    }).catch(e => { if (!controller.signal.aborted) setError(message(e)); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [runId, refresh]);
  useEffect(() => {
    if (!run || isTerminalBacktestStatus(run.status) || loading) return;
    // Poll through the same abortable refresh effect, rather than allowing a
    // second detail request to race manual refresh and overwrite a newer run.
    let timer: ReturnType<typeof setTimeout>;
    const schedule = () => { clearTimeout(timer); if (document.visibilityState === "visible") timer = setTimeout(() => setRefresh(v => v + 1), 5000); };
    schedule(); document.addEventListener("visibilitychange", schedule);
    return () => { clearTimeout(timer); document.removeEventListener("visibilitychange", schedule); };
  }, [run?.status, loading, refresh]);
  useEffect(() => {
    if (!run) return;
    const controller = new AbortController(), current = ++generation.current;
    if (run.status === "indeterminate") { setData({}); setSummary(null); setDataErrors({}); setPending(false); return; }
    setPending(true);
    // Independent sections keep usable evidence visible when another API fails.
    const kinds = ["equity", "metrics", "data-preflight"];
    void fetchBacktestAnalysisSummary(runId, controller.signal).then(value => { if (!controller.signal.aborted) { setSummary(value); setDataErrors(e => ({ ...e, summary: "" })); } }).catch(e => { if (!controller.signal.aborted) setDataErrors(previous => ({ ...previous, summary: message(e) })); });
    void Promise.allSettled(kinds.map(async kind => {
      try { const rows = await readAllResults(runId, kind, controller.signal); if (current === generation.current && !controller.signal.aborted) { setData(d => ({ ...d, [kind]: rows })); setDataErrors(e => ({ ...e, [kind]: "" })); } }
      catch (e) { if (!controller.signal.aborted) setDataErrors(previous => ({ ...previous, [kind]: message(e) })); }
    })).then(() => { if (!controller.signal.aborted) setPending(false); });
    return () => controller.abort();
  }, [runId, run?.status, run?.completed_steps, refresh]);
  useEffect(() => {
    const observer = new IntersectionObserver(entries => { const visible = entries.filter(e => e.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top); if (visible[0]) setActive(visible[0].target.id); }, { rootMargin: "-90px 0px -60% 0px", threshold: 0 });
    sections.forEach(([id]) => { const node = document.getElementById(id); if (node) observer.observe(node); });
    return () => observer.disconnect();
  }, [!!run]);
  function returnToWorkbench(compare = false) {
    if (comparisonReturn) { navigate(comparisonReturn.url, { state: comparisonReturn.state }); return; }
    const state = origin?.workbench || {};
    const ids: string[] = state.compareIds || [];
    if (compare && !ids.includes(runId) && ids.length >= 10) { setError("已选择 10 个运行，请先返回工作台移除一项。"); return; }
    if (compare) { navigate(comparisonUrl([...new Set([...ids, runId])]), { state: { from: back, workbench: { ...state, selected: run || state.selected } } }); return; }
    navigate(back, { state: { workbench: { ...state, selected: run || state.selected, compareMode: compare || state.compareMode, compareIds: compare ? [...new Set([...ids, runId])] : ids } } });
  }
  async function download() {
    if (!run || exportController.current || run.status === "indeterminate") return;
    const controller = new AbortController(); exportController.current = controller; setExporting(true); setError("");
    try {
      const before = await getBacktestRun(runId, controller.signal), results: Record<string, ResultRow[]> = {};
      for (const [kind, name] of Object.entries(RESULT_KINDS)) {
        setExportStatus(`正在导出${name}…`);
        results[kind] = await readAllResults(runId, kind, controller.signal, n => setExportStatus(`${name}已读取 ${n} 条`));
      }
      const analysis_summary = await fetchBacktestAnalysisSummary(runId, controller.signal);
      const after = await getBacktestRun(runId, controller.signal); controller.signal.throwIfAborted();
      // Active runs can grow while paginating. Label those exports explicitly;
      // a terminal run changing under the reader is not a consistent snapshot.
      const stable = isTerminalBacktestStatus(before.status) && before.status === after.status && JSON.stringify(before.result_counts) === JSON.stringify(after.result_counts);
      if (isTerminalBacktestStatus(before.status) && !stable) throw new Error("导出期间运行结果发生变化，请刷新后重试。");
      const blob = new Blob([JSON.stringify({ format: "quant-foundry.backtest-result@1", exported_at: new Date().toISOString(), complete_successful_run: stable && after.status === "succeeded" && ["complete", "passed"].includes(after.result_integrity_status || ""), scope: stable ? "已持久化结果；完整性以运行证据为准" : "读取期间运行仍在变化，属于部分结果，不是原子快照", run: after, run_before_export: before, analysis_summary, results }, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob), anchor = document.createElement("a"); anchor.href = url; anchor.download = `backtest-${runId}.json`; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); setToast("结果已导出，文件包含运行状态和完整性说明。");
    } catch (e) { if (!controller.signal.aborted) setError(message(e)); }
    finally { exportController.current = null; setExporting(false); setExportStatus(""); }
  }
  const config = run ? runConfig(run) : {}, timezone = String(config.timezone || "Asia/Shanghai");
  const equity = data.equity || [], monthlies = monthlyResults(equity, String(config.start_date || ""), String(config.end_date || ""), timezone);
  const dates = [...new Set(equity.map(row => String(row.as_of)))];
  const years = [...new Set(monthlies.map(row => row.month.slice(0, 4)))];
  const currentMetrics = data.metrics || [], disabled = run?.status === "indeterminate";
  return <div className="qfr-page">
    <div className="qfr-heading"><div><div className="qfb-eyebrow">BACKTEST RESULT</div><h1>{run?.strategy_name || "单次回测结果"}{run?.revision_number ? ` · v${run.revision_number}` : ""}</h1><p>{alias} {alias && "·"} {run ? STATUS[run.status] : "正在读取运行"}</p><code>{runId}</code></div><div className="qfr-actions"><button onClick={() => returnToWorkbench()}><ArrowLeft size={16}/>{comparisonReturn ? "返回对比" : "返回工作台"}</button><button disabled={loading || pending} onClick={() => setRefresh(v => v + 1)}><RefreshCw size={16}/>刷新</button><button disabled={!run || disabled || exporting} className="qfb-primary" onClick={() => void download()}><Download size={16}/>{exporting ? "导出中…" : "导出结果"}</button></div></div>
    {toast && <div className="qfb-toast" role="status">{toast}<button aria-label="关闭提示" onClick={() => setToast("")}><X size={16}/></button></div>}
    {error && <p className="qfr-error" role="alert">{error}<button onClick={() => setRefresh(v => v + 1)}>重试</button></p>}
    {exporting && <p role="status">{exportStatus}<button onClick={() => exportController.current?.abort()}>取消导出</button></p>}
    {!run ? <div className="qfr-empty">{loading ? "正在读取回测结果…" : "无法读取本次运行，请检查链接或重试。"}</div> : <div className="qfr-layout"><nav className="qfr-nav" aria-label="结果章节">{sections.map(([id, title]) => <a key={id} href={`#${id}`} aria-current={active === id ? "location" : undefined} onClick={() => setActive(id)}>{title}</a>)}</nav><main className="qfr-main">
      <section className="qfr-card qfr-run-strip"><div><strong>{run.strategy_name || "策略名称未记录"}</strong><span className={`qfb-badge qfb-${run.status}`}>{STATUS[run.status]}</span><small>{config.start_date || "—"} — {config.end_date || "—"} · {config.frequency === "1d" ? "日线" : config.frequency || "频率未记录"}</small></div><div><small>初始现金</small><strong>{displayNumber(config.initial_cash)} {config.currency || ""}</strong></div><div className="qfr-actions"><button onClick={() => { try { copyConfiguration(run); if (!run.strategy_id) throw new Error("策略身份暂不可用，无法复制配置。"); setCopyOpen(true); } catch (e) { setError(message(e)); } }}><Copy size={16}/>复制配置</button><button disabled={run.status !== "succeeded"} onClick={() => returnToWorkbench(true)}>加入对比</button></div></section>
      <p className="qfr-integrity">{run.status === "succeeded" ? "运行已完成" : "部分结果：仅展示已持久化数据"} · 完整性：{({ passed: "验证通过", unavailable: "无法核实", complete: "完整", valid: "验证通过", incomplete: "不完整", pending: "待核实", failed: "验证失败", unknown: "未知" } as Record<string,string>)[run.result_integrity_status || ""] || "以运行诊断中的原始证据为准"}<span className="qfr-loading" role="status">{pending ? "正在更新结果…" : ""}</span></p>
      {Object.entries(dataErrors).filter(([, e]) => e).map(([kind, e]) => <p className="qfr-error" role="alert" key={kind}>{RESULT_KINDS[kind as keyof typeof RESULT_KINDS] || "分析摘要"}读取失败：{e}；已有展示保留上次读取结果。<button onClick={() => setRefresh(v => v + 1)}>重试</button></p>)}
      <section className="qfr-kpis" id="overview">{metrics.map(metric => { const row = currentMetrics.find(r => r.metric_key === metric.key), value = finite(row?.value); return <div key={metric.key}><span>{metric.name}</span><strong className={metric.key.includes("return") && value !== null ? value > 0 ? "qfr-up" : value < 0 ? "qfr-down" : "" : ""}>{displayNumber(row?.value, metric.percent)}</strong><small>{row ? value === null ? row.unavailable_reason || "计算依据不足" : metric.key === "cumulative_fees" ? config.currency || "按账户币种" : "本次运行实际结果" : disabled ? "结果待判定" : "尚未产出"}</small>{row && <EvidenceTable title="查看计算口径" value={row}/>}</div>; })}</section>
      <section className="qfr-card" id="curve"><header><h2>累计收益</h2><span>当前运行 · 缺失估值保留断点</span></header><ResultChart rows={equity} field="cumulative_return" title="累计收益" timezone={timezone}/></section>
      <div className="qfr-two" id="risk"><section className="qfr-card"><header><h2>回撤</h2><span>相对历史峰值</span></header><ResultChart rows={equity} field="drawdown" title="回撤" timezone={timezone}/></section><section className="qfr-card"><header><h2>逐期收益</h2><span>相对上一有效估值</span></header><ResultChart rows={equity} field="period_return" title="逐期收益" bars timezone={timezone}/><p className="qfr-note">缺失估值后的收益可能覆盖多日，不能直接视为单日收益。</p></section></div>
      <section className="qfr-card" id="monthly"><header><h2>月度表现</h2><span>仅当前运行区间</span></header>{years.length ? <div className="qfr-table-scroll"><table className="qfr-months"><thead><tr><th>年份</th>{Array.from({ length: 12 }, (_, i) => <th key={i}>{i + 1}月</th>)}</tr></thead><tbody>{years.map(year => <tr key={year}><th>{year}</th>{Array.from({ length: 12 }, (_, i) => { const row = monthlies.find(m => m.month === `${year}-${String(i + 1).padStart(2, "0")}`); return <td key={i} className={row?.value == null ? "" : row.value > 0 ? "qfr-month-up" : row.value < 0 ? "qfr-month-down" : ""}>{displayNumber(row?.value, true)}{row && <small>{row.reason || (row.partial ? "区间内表现" : "")}</small>}</td>; })}</tr>)}</tbody></table></div> : <p className="qfr-empty">暂无月度收益数据。</p>}<p className="qfr-note">按月内逐期收益复合计算；存在缺失估值或跨月区间不明时不计算。首尾不足整月仅代表回测区间内表现。</p></section>
      <ResultTables key={runId} runId={runId} dates={dates} disabled={disabled} version={`${run.status}:${run.completed_steps}:${refresh}`}/>
      <section className="qfr-card" id="diagnostics"><header><h2>运行诊断</h2><span>仅当前运行证据</span></header><div className="qfr-body"><div className="qfr-diagnostic"><strong>运行状态：{STATUS[run.status]}</strong><p>{run.error_message || run.message || "未提供额外运行说明。"}</p><small>结果待判定时不读取受保护的结果明细；没有证据不代表检查通过。</small></div>
      {(data["data-preflight"] || []).map((row, i) => <div className="qfr-diagnostic" key={i}><strong>{row.title || (row.phase === "session" ? "会话内数据检查" : "数据检查证据")}</strong><p>{row.message || "已记录数据检查，请展开查看检查阶段、状态及覆盖范围。"}</p><EvidenceTable title="数据检查详情" value={row}/></div>)}
      <EvidenceTable title="失败定位与资源诊断" value={{ failure_evidence: run.failure_evidence, failure_phase: run.failure_phase, failure_type: run.failure_type, source_line: run.source_line, technical_detail: run.technical_detail, heartbeat: run.last_heartbeat_at, worker: run.worker_id, recovery: run.recovery_process_state, stdout: run.stdout_evidence, resources: run.resource_limit_evidence }}/>
      {summary && <EvidenceTable title="分析摘要与缺失区间" value={summary}/>}
      <EvidenceTable title="退出码、完成标记与完整性" value={{ exit_code: run.child_exit_code, completion_marker: run.completion_marker, integrity_status: run.result_integrity_status, integrity: run.result_integrity_evidence, decision: run.terminal_decision_reason, result_counts: run.result_counts }}/></div></section>
      <section className="qfr-card" id="configuration"><header><h2>冻结配置</h2><span>运行创建时记录</span></header><div className="qfr-body"><dl className="qfr-config"><div><dt>策略版本</dt><dd>v{run.revision_number || "—"} · {alias || "版本别名未记录"}</dd></div><div><dt>回测账户 UUID / 版本</dt><dd>{run.account_profile_id || "未记录"} / v{run.account_profile_version || "—"}</dd></div><div><dt>费用方案 / 版本</dt><dd>{run.fee_schedule_key || "未记录"} / v{run.fee_schedule_version || "—"}</dd></div><div><dt>时区 / 币种</dt><dd>{timezone} / {config.currency || "未记录"}</dd></div></dl><EvidenceTable title="策略参数、账户与运行配置" value={{ revision: run.strategy_revision_id, parameters: run.parameters, config: run.backtest_config, data_request: run.data_request }}/><EvidenceTable title="组件快照、随机种子与行为版本" value={{ components: run.component_snapshot, behavior_versions: run.behavior_versions, random_seed: run.random_seed }}/></div></section>
    </main></div>}
    {copyOpen && run && <div className="qfb-page"><CreateRunDrawer initialStrategyId={run.strategy_id || undefined} source={run} onClose={() => setCopyOpen(false)} onCreated={created => { setCopyOpen(false); navigate(back, { state: { workbench: { ...origin?.workbench, selected: { ...created, strategy_id: run.strategy_id, strategy_name: run.strategy_name, revision_number: run.revision_number } } } }); }}/></div>} 
  </div>;
}
