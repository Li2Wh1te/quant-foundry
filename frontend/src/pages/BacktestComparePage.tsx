import { useEffect, useRef, useState } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { ArrowLeft, Plus, RefreshCw, X } from "lucide-react";
import { useAuth } from "../auth/AuthContext";
import { getComparison, type SavedComparison } from "../api/backtestComparisons";
import { compareResults, onResultHidden, type CompareResult } from "../api/backtestResults";
import { onRunAccessDenied } from "../api/runService";
import { EvidenceTable } from "../components/EvidenceTable";
import { Select } from "../components/controls/Select";
import { AddRunsDrawer, SavedListDrawer } from "./backtests/ComparisonDrawers";
import { ComparisonChart } from "./backtests/ComparisonCharts";
import { canonical, comparisonReason, comparisonUrl, metricDifference, parseSelection, UUID_PATTERN } from "./backtests/comparisonData";
import { chartRows, displayNumber, METRICS, summaryMetrics } from "./backtests/resultData";
import { PageFeedback, PageNavigation } from "./backtests/ResultTables";
import { useResultPage } from "./backtests/useResultPage";
import { STATUS } from "./backtests/run-ui/config";
import "./backtests/Result.css";
import "./backtests/Comparison.css";

export function BacktestComparePage() {
  const [query] = useSearchParams(), location = useLocation(), navigate = useNavigate(), { logout } = useAuth();
  const { ids, baseline, error: selectionError } = parseSelection(query), savedId = query.get("saved") || "", batchId = query.get("batch") || "", key = canonical([ids, baseline]);
  const [saved, setSaved] = useState<SavedComparison | null>(null), [result, setResult] = useState<{ key: string; value: CompareResult } | null>(null);
  const [error, setError] = useState(""), [savedError, setSavedError] = useState(""), [loading, setLoading] = useState(false), [refresh, setRefresh] = useState(0), [denied, setDenied] = useState(false);
  const [dialog, setDialog] = useState<"add" | "list" | null>(null), request = useRef<AbortController | null>(null), revoked = useRef(false);
  const filterQuery = useRef(location.search);
  useEffect(() => { filterQuery.current = location.search; }, [location.search]);
  const origin = location.state as { from?: string; reportScroll?: number } | null;
  const from = origin?.from?.match(/^\/admin\/(?:backtest-runs|strategies\/[^/]+\/backtests)(?:\?|$)/) ? origin.from : "/admin/backtest-runs";
  const current = !denied && !savedError && result?.key === key ? result.value : null;
  const valid = !selectionError && ids.length >= 2 && (!savedId || saved?.id === savedId), readOnly = !!savedId;
  function change(next: string[], nextBaseline = next.includes(baseline) ? baseline : next[0] || "") {
    const search = new URLSearchParams(location.search); search.set("runs", next.join(",")); nextBaseline ? search.set("baseline", nextBaseline) : search.delete("baseline"); search.delete("curveA"); search.delete("curveB");
    navigate({ pathname: location.pathname, search: search.toString() }, { state: location.state });
  }
  useEffect(() => onRunAccessDenied(status => { revoked.current = true; setResult(null); setSaved(null); setDialog(null); setDenied(true); setError("权限已撤销，已清除比较结果。"); if (status === 401) { logout(); navigate("/login", { replace: true }); } }), [logout, navigate]);
  useEffect(() => onResultHidden(hiddenIds => { if (hiddenIds.some(id => ids.includes(id))) { revoked.current = true; setResult(null); setDenied(true); setError("所选运行不存在或已不可读取，已清除比较结果。"); } }), [key]);
  useEffect(() => {
    const c = new AbortController(); setSaved(null); setSavedError("");
    if (!savedId || denied) return () => c.abort();
    if (!UUID_PATTERN.test(savedId)) { setSavedError("保存对比链接无效。"); return () => c.abort(); }
    getComparison(savedId, c.signal).then(item => { if (!c.signal.aborted && !revoked.current) { setSaved(item); if (canonical(ids) !== canonical(item.run_ids) || baseline !== item.baseline_run_id) navigate(comparisonUrl(item.run_ids, item.baseline_run_id, item.id), { replace: true, state: location.state }); } }).catch(e => { if (!c.signal.aborted && !revoked.current) { setSavedError(e.message); setResult(null); } });
    return () => c.abort();
  }, [savedId, refresh, denied]);
  useEffect(() => {
    const c = new AbortController(); request.current = c;
    if (denied) { setLoading(false); return () => c.abort(); }
    setError(""); setLoading(valid);
    if (valid && !denied) compareResults(ids, baseline, c.signal).then(value => { if (!c.signal.aborted && !revoked.current) setResult({ key, value }); }).catch(e => { if (!c.signal.aborted && !revoked.current) setError(e.message); }).finally(() => { if (!c.signal.aborted && !revoked.current) setLoading(false); });
    return () => c.abort();
  }, [key, valid, refresh, denied]);
  useEffect(() => { if (current && origin?.reportScroll != null) { const raf = requestAnimationFrame(() => { const container = document.querySelector(".qfo-main"); if (container) container.scrollTop = origin.reportScroll!; }); return () => cancelAnimationFrame(raf); } }, [!!current]);
  const filter = query.get("filter") || "", status = query.get("status") || "";
  const items = current?.items.filter(item => (!status || item.status === status) && `${item.run_id} ${JSON.stringify(item.config.parameters || {})}`.toLowerCase().includes(filter.toLowerCase())) || [];
  const baseItem = current?.items.find(item => item.run_id === baseline);
  const setFilter = (name: string, value: string) => { const q = new URLSearchParams(filterQuery.current); value ? q.set(name, value) : q.delete(name); filterQuery.current = q.toString(); navigate({ search: q.toString() }, { replace: true, state: location.state }); };
  const rows = (item: typeof baseItem) => summaryMetrics(item ? { schema_id: item.engine_kind === "rust" ? "qf.results.v1" : "legacy", run_id: item.run_id, partial: item.partial, performance: item.summary, metrics: item.metrics } : null);
  return <div className="qcmp-page" data-compare-ui="s3-d16"><header className="qcmp-heading"><div><div className="qcmp-eyebrow">BACKTEST COMPARISON</div><h1>{saved?.name || "回测对比"}{readOnly ? " · 历史只读" : ""}</h1><p>最多 16 组参数。完整指标按原公式展示，部分结果明确标注。</p></div><div className="qcmp-actions"><button onClick={() => navigate(from)}><ArrowLeft size={16}/>返回工作台</button><button disabled={denied} onClick={() => setDialog("list")}>已保存的对比</button><button disabled={denied || readOnly || ids.length >= 16} onClick={() => setDialog("add")}><Plus size={16}/>添加运行</button></div></header>
    {(selectionError || savedError || error) && <p className="qcmp-error" role="alert">{selectionError || savedError || error}{current && " 当前保留上次读取的所选结果。"}</p>}
    {readOnly && <p className="qcmp-note">原保存成员与名称只读保留，结果不使用新公式重算。<button onClick={() => navigate(comparisonUrl(ids, baseline), { state: location.state })}>作为新的比较选择打开</button></p>}
    <div className="qcmp-toolbar"><label>比较基准<Select value={baseline} aria-label="比较基准" disabled={readOnly || denied} onChange={e => change(ids, e.target.value)}>{ids.map((id, i) => <option key={id} value={id}>R{i + 1} · {id.slice(0, 8)}</option>)}</Select></label><span role="status">{loading ? "正在读取比较…" : `已选择 ${ids.length} / 16 个运行`}</span><button disabled={denied} onClick={() => setRefresh(v => v + 1)}><RefreshCw size={16}/>刷新</button>{loading && <button onClick={() => { request.current?.abort(); setLoading(false); setError("比较加载已取消，尚未确认的结果不作为空数据。"); }}>取消比较加载</button>}</div>
    <section className="qcmp-pool" aria-label="已选运行">{!denied && ids.map((id, i) => { const item = current?.items.find(r => r.run_id === id); return <article className="qcmp-run" key={id}><div className="qcmp-run-top"><strong>R{i + 1}{id === baseline ? " · 比较基准" : ""}</strong>{!readOnly && <button aria-label={`移除 R${i + 1}`} onClick={() => change(ids.filter(v => v !== id))}><X size={16}/></button>}</div><code>{id}</code><small>{item?.engine_kind || "等待读取"} · {STATUS[item?.status || ""] || "—"}{item?.partial ? " · 部分结果" : ""}</small><button onClick={() => navigate(`/admin/backtest-runs/${id}/results`, { state: { from, comparisonReturn: { url: location.pathname + location.search, state: { ...location.state, reportScroll: document.querySelector(".qfo-main")?.scrollTop || 0 } } } })}>查看结果</button></article>; })}</section>
    {!valid && !selectionError && <p className="qcmp-empty">请选择至少 2 个已终结运行；可从参数批次选择。</p>}
    {current && baseItem && <><p className="qcmp-note">{current.warnings.join("；")}。无基准不生成超额，新旧结果不作伪排名。</p><div className="qcmp-toolbar"><label>筛选参数 / 运行 ID<input aria-label="筛选参数或运行ID" value={filter} maxLength={200} onChange={e => setFilter("filter", e.target.value)}/></label><label>结果状态<Select value={status} aria-label="结果状态" onChange={e => setFilter("status", e.target.value)}><option value="">全部</option><option value="succeeded">已完成</option><option value="failed">失败</option><option value="cancelled">已取消</option></Select></label><span>本次显示 {items.length} / {ids.length} 项</span></div>
      <section className="qcmp-card"><header><h2>关键指标</h2><span>各自完整运行区间 / 原保存值</span></header><div className="qcmp-table-scroll"><table className="qcmp-matrix"><thead><tr><th>指标</th>{items.map(item => <th key={item.run_id}>R{ids.indexOf(item.run_id) + 1}<small>{item.config.start || item.config.spec?.start_date || item.config.start_date || "区间未记录"} — {item.config.end || item.config.spec?.end_date || item.config.end_date || "区间未记录"}</small></th>)}</tr></thead><tbody>{METRICS.map(metric => <tr key={metric.key}><th>{metric.name}</th>{items.map(item => { const matches = rows(item).filter(r => r.metric_key === metric.key), row = matches.length === 1 ? matches[0] : undefined, baseRows = rows(baseItem).filter(r => r.metric_key === metric.key), baseMetric = baseRows.length === 1 ? baseRows[0] : undefined, warning = comparisonReason(baseItem, item, baseMetric, row); return <td key={item.run_id}><strong>{displayNumber(row?.value, metric.percent)}</strong><small>{row?.value == null ? row?.unavailable_reason || "未保存或公式不唯一" : ""}</small><small>{warning || (item.run_id !== baseline ? `相对基准 ${metricDifference(row?.value, baseMetric?.value, metric.percent)}` : "比较基准原值")}</small>{row && <EvidenceTable title="原值与计算口径" value={row}/>}</td>; })}</tr>)}</tbody></table></div></section>
      <CompareCurves key={key} ids={ids} baseline={baseline} refresh={refresh}/>
      <section className="qcmp-card"><header><h2>配置差异与模型假设</h2><span>相对比较基准</span></header><div className="qcmp-evidence">{items.map(item => <section key={item.run_id}><h3>R{ids.indexOf(item.run_id) + 1} · {item.engine_kind}</h3><p>{item.partial ? "部分结果，不构成完整收益比较" : ""}{!(item.config.benchmark || item.policy?.benchmark) && " · 无基准，不提供超额"}</p><EvidenceTable title="参数、模型、公式、费用与日期差异" value={{ config: item.config, policy: item.policy, execution_model: item.execution_model, cost_configuration: item.cost_configuration, differences: item.configuration_differences }}/></section>)}</div></section>
    </>}
    {dialog === "add" && !denied && <AddRunsDrawer selected={ids} batchId={batchId} onToggle={id => change(ids.includes(id) ? ids.filter(v => v !== id) : [...ids, id])} onClose={() => setDialog(null)}/>}
    {dialog === "list" && !denied && <SavedListDrawer onOpen={item => { setDialog(null); navigate(comparisonUrl(item.run_ids, item.baseline_run_id, item.id), { state: location.state }); }} onClose={() => setDialog(null)}/>}
  </div>;
}
function CompareCurves({ ids, baseline, refresh }: { ids: string[]; baseline: string; refresh: number }) {
  const location = useLocation(), navigate = useNavigate(), query = new URLSearchParams(location.search);
  const second = query.get("curveRun"), other = second && ids.includes(second) && second !== baseline ? second : ids.find(id => id !== baseline)!;
  const a = useResultPage(baseline, "equity", query.get("curveA") || "", true, refresh), b = useResultPage(other, "equity", query.get("curveB") || "", true, refresh);
  const runs = [baseline, other].map((id, i) => ({ id, slot: `R${ids.indexOf(id) + 1}`, label: id.slice(0, 8), color: i ? "#4E7C9C" : "#D6542D" }));
  const series = [a.page, b.page].filter((p): p is NonNullable<typeof p> => !!p).map(p => ({ run_id: p.run_id, points: chartRows(p.items) }));
  return <section className="qcmp-card qfr-page" style={{ padding: 0 }}><header><h2>采样曲线比较</h2><label>曲线运行<Select aria-label="曲线运行" value={other} onChange={e => { const q = new URLSearchParams(location.search); q.set("curveRun", e.target.value); q.delete("curveB"); navigate({ search: q.toString() }, { state: location.state }); }}>{ids.filter(id => id !== baseline).map(id => <option key={id} value={id}>R{ids.indexOf(id) + 1} · {id.slice(0, 8)}</option>)}</Select></label></header>
    <p className="qcmp-note">每条序列仅当前页、最多 100 个保存采样点。分页范围可能不同；缺价断线，不插值、不补零，不是完整 Tick 曲线。</p><PageFeedback data={a}/><PageFeedback data={b}/><ComparisonChart runs={runs} series={series} field="cumulative_return" title="累计收益对比"/><ComparisonChart runs={runs} series={series} field="drawdown" title="回撤对比"/><PageNavigation data={a} cursorKey="curveA" title="基准权益"/><PageNavigation data={b} cursorKey="curveB" title="对照权益"/>
  </section>;
}
