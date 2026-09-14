import { useEffect, useRef, useState } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { ArrowLeft, Plus, RefreshCw, Save, X } from "lucide-react";
import { deleteComparison, fetchComparison, getComparison, saveComparison, updateComparison, type ComparisonResult, type SavedComparison } from "../api/backtestComparisons";
import { fetchRunWorkbench } from "../api/backtestRuns";
import { listStrategyRevisions } from "../api/strategies";
import { EvidenceTable } from "../components/BacktestReport";
import { Select } from "../components/controls/Select";
import { AddRunsDrawer, ComparisonDrawer, NameDrawer, SavedListDrawer } from "./backtests/ComparisonDrawers";
import { ComparisonChart, RiskScatter } from "./backtests/ComparisonCharts";
import { canonical, COMPARISON_METRICS, comparisonConfig, comparisonReason, comparisonUrl, metricDifference, parseSelection, selectedMetric, UUID_PATTERN } from "./backtests/comparisonData";
import { displayNumber } from "./backtests/resultData";
import { finite } from "./backtests/workbench";
import "./backtests/Comparison.css";
const COLORS = ["#D6542D", "#4E7C9C", "#A07126", "#7D619B", "#39877E", "#AD557F", "#5265A0", "#7A812C", "#925B39", "#536B72"];
const fieldLabels: Record<string, string> = { parameters: "策略参数", backtest_config: "运行配置", data_request: "数据请求", behavior_versions: "行为版本", component_snapshot: "组件快照", account_snapshot: "账户快照", data_evidence: "数据证据", random_seed: "随机种子" };
const text = (v: unknown, present = true) => !present ? "未记录" : v === null ? "空值" : typeof v === "object" ? JSON.stringify(v) : String(v);
export function BacktestComparePage() {
  const [query] = useSearchParams(), location = useLocation(), navigate = useNavigate();
  const selection = parseSelection(query), { ids, baseline } = selection, savedId = query.get("saved") || "";
  const [saved, setSaved] = useState<SavedComparison | null>(null), [savedLoading, setSavedLoading] = useState(false), [savedError, setSavedError] = useState("");
  const [result, setResult] = useState<{ key: string; data: ComparisonResult } | null>(null), [loading, setLoading] = useState(false), [error, setError] = useState(""), [refresh, setRefresh] = useState(0), [toast, setToast] = useState("");
  const [dialog, setDialog] = useState<"add" | "list" | "save" | "saveAs" | null>(null), [rename, setRename] = useState<SavedComparison | null>(null), [removeSaved, setRemoveSaved] = useState<SavedComparison | null>(null), [deleteBusy, setDeleteBusy] = useState(false), [deleteError, setDeleteError] = useState("");
  const [singleRun, setSingleRun] = useState<Record<string, any> | null>(null);
  const createIdentity = useRef<{ fingerprint: string; id: string } | null>(null), deleting = useRef(false), colors = useRef(new Map<string, number>());
  const key = canonical([ids, baseline]), current = result?.key === key ? result.data : null;
  const origin = location.state as { from?: string; workbench?: any } | null;
  const from = origin?.from?.match(/^\/admin\/(backtest-runs|strategies\/[^/]+\/backtests)$/) ? origin.from : "/admin/backtest-runs";
  const valid = !selection.error && ids.length >= 2;
  const name = saved?.id === savedId ? saved.name : "回测对比";
  const unsaved = saved?.id === savedId && (canonical(saved.run_ids) !== canonical(ids) || saved.baseline_run_id !== baseline);
  function change(next: string[], nextBaseline = next.includes(baseline) ? baseline : next[0] || "", nextSaved = savedId) {
    navigate(comparisonUrl(next, nextBaseline, nextSaved), { replace: true, state: location.state });
  }
  function openSaved(item: SavedComparison) { setDialog(null); setSaved(item); navigate(comparisonUrl(item.run_ids, item.baseline_run_id, item.id), { state: location.state }); }
  useEffect(() => { if (!toast) return; const timer = setTimeout(() => setToast(""), 4000); return () => clearTimeout(timer); }, [toast]);
  useEffect(() => {
    const c = new AbortController(); setSavedError("");
    if (!savedId) { setSaved(null); setSavedLoading(false); return; }
    if (!UUID_PATTERN.test(savedId)) { setSavedError("保存对比链接无效。"); setSavedLoading(false); return; }
    setSavedLoading(true);
    getComparison(savedId, c.signal).then(item => { if (!c.signal.aborted) { setSaved(item); if (!query.has("runs")) change(item.run_ids, item.baseline_run_id, item.id); } }).catch(e => { if (!c.signal.aborted) setSavedError(e.message); }).finally(() => { if (!c.signal.aborted) setSavedLoading(false); });
    return () => c.abort();
  }, [savedId, refresh]);
  useEffect(() => {
    const c = new AbortController(); setError("");
    if (!valid) { setLoading(false); return; }
    setLoading(true);
    fetchComparison(ids, baseline, c.signal).then(data => { if (c.signal.aborted) return; if (data.baseline_run_id !== baseline || data.run_summaries.length !== ids.length || ids.some(id => !data.run_summaries.some(r => r.run_id === id)) || data.run_summaries.some(r => r.status !== "succeeded")) throw new Error("对比包含不可用或未完成的运行，请移除后重新选择。"); setResult({ key, data }); }).catch(e => { if (!c.signal.aborted) setError(e.message); }).finally(() => { if (!c.signal.aborted) setLoading(false); });
    return () => c.abort();
  }, [key, valid, refresh]);
  // A one-run draft cannot call the comparison API. Resolve its display names
  // through the same authorized workspace projection used by the run picker.
  const singleId = !selection.error && ids.length === 1 ? ids[0] : "";
  useEffect(() => {
    if (!singleId) return;
    const c = new AbortController();
    fetchRunWorkbench({ search: singleId, status: "succeeded", offset: 0 }, c.signal).then(async page => {
      const row = page.items.find(r => r.run_id === singleId);
      if (!row || c.signal.aborted) return;
      setSingleRun(row);
      if (row.strategy_id) {
        const versions = await listStrategyRevisions(row.strategy_id);
        if (!c.signal.aborted) setSingleRun({ ...row, revision_alias: versions.find(v => v.id === row.strategy_revision_id)?.alias });
      }
    }).catch(() => { /* The draft remains removable when display metadata is unavailable. */ });
    return () => c.abort();
  }, [singleId]);
  // Keep a run's series color stable as the baseline changes or peers leave.
  const used = new Set(ids.map(id => colors.current.get(id)).filter((n): n is number => n !== undefined));
  ids.forEach(id => { if (!colors.current.has(id)) { const free = COLORS.findIndex((_, i) => !used.has(i)); colors.current.set(id, free < 0 ? used.size % COLORS.length : free); used.add(colors.current.get(id)!); } });
  for (const id of colors.current.keys()) if (!ids.includes(id)) colors.current.delete(id);
  const runs = ids.map((id, i) => { const summary = result?.data.run_summaries.find(r => r.run_id === id) || (singleRun?.run_id === id ? singleRun : undefined); return { id, slot: `R${i + 1}`, color: COLORS[colors.current.get(id) || 0], label: summary ? `${summary.strategy_name || "策略名称未记录"} · v${summary.revision_number || "—"} · ${summary.revision_alias || "未命名版本"}` : `运行 ${id.slice(0, 8)}`, summary }; });
  const baselineRun = current?.run_summaries.find(r => r.run_id === baseline);
  const differencePaths = [...new Set((current?.configuration_diff || []).flatMap(d => Object.keys(d.fields || {})))];
  const metricRows = current?.metric_matrix || [];
  async function save(name: string, asNew: boolean) {
    if (!current || !valid) throw new Error("请等待当前对比加载成功后再保存。");
    const definition = { name, run_ids: ids, baseline_run_id: baseline };
    let item: SavedComparison;
    if (!asNew && saved?.id === savedId) item = await updateComparison(saved.id, { ...definition, version: saved.version });
    else { const fingerprint = canonical(definition); if (createIdentity.current?.fingerprint !== fingerprint) createIdentity.current = { fingerprint, id: crypto.randomUUID() }; item = await saveComparison({ ...definition, id: createIdentity.current.id }); }
    setSaved(item); setDialog(null); createIdentity.current = null; change(item.run_ids, item.baseline_run_id, item.id); setToast("对比已保存，可从已保存列表重新打开。");
  }
  return <div className="qcmp-page"><header className="qcmp-heading"><div><div className="qcmp-eyebrow">BACKTEST COMPARISON</div><h1>{name}</h1><p>比较完整运行的收益、风险与配置差异。{unsaved && <span className="qcmp-unsaved">当前选择尚未保存</span>}</p></div><div className="qcmp-actions"><button onClick={() => navigate(from, { state: { workbench: { ...origin?.workbench, compareIds: ids, compareMode: true } } })}><ArrowLeft size={16}/>返回工作台</button><button onClick={() => setDialog("list")}>已保存的对比</button><button className="qcmp-primary" disabled={ids.length >= 10} onClick={() => setDialog("add")}><Plus size={16}/>添加运行</button></div></header>
    {toast && <div className="qcmp-toast" role="status">{toast}<button aria-label="关闭提示" onClick={() => setToast("")}><X size={16}/></button></div>}
    {(selection.error || savedError || error) && <div role="alert" className="qcmp-error">{selection.error || savedError || error}{error && current && <span>当前保留上次读取的结果。</span>}<button onClick={() => setRefresh(v => v + 1)}>重试</button>{savedError && <button onClick={() => change(ids, baseline, "")}>解除保存关联</button>}{selection.error && <button onClick={() => change([], "", "")}>重新选择</button>}</div>}
    <div className="qcmp-toolbar"><label>比较基准<Select aria-label="比较基准" value={baseline} disabled={!ids.length || !!selection.error} onChange={e => change(ids, e.target.value)}>{runs.map(run => <option key={run.id} value={run.id}>{run.slot} · {run.label}</option>)}</Select></label><span className="qcmp-loading" role="status">{loading || savedLoading ? "正在读取对比…" : `已选择 ${ids.length} / 10 个运行`}</span><div className="qcmp-actions"><button disabled={loading || savedLoading} onClick={() => setRefresh(n => n + 1)}><RefreshCw size={16}/>刷新</button><button disabled={!current || loading || savedLoading || !!savedError} onClick={() => setDialog("save")}><Save size={16}/>保存对比</button>{saved && <button disabled={!current || loading} onClick={() => setDialog("saveAs")}>另存为</button>}</div></div>
    <section className="qcmp-pool" aria-label="已选运行">{runs.map(run => <article className={`qcmp-run ${run.id === baseline ? "is-baseline" : ""}`} key={run.id}><div className="qcmp-run-top"><span style={{ color: run.color }}>{run.slot}{run.id === baseline ? " · 比较基准" : ""}</span><button aria-label={`移除 ${run.slot}`} onClick={() => change(ids.filter(id => id !== run.id))}><X size={15}/></button></div><strong>{run.label}</strong><small>{run.summary ? `${comparisonConfig(run.summary).start_date || "—"} — ${comparisonConfig(run.summary).end_date || "—"}` : "等待运行资料"}</small><code>{run.id}</code><div className="qcmp-actions"><button disabled={run.id === baseline} onClick={() => change(ids, run.id)}>设为基准</button><button onClick={() => navigate(`/admin/backtest-runs/${run.id}/results`, { state: { ...location.state, comparisonReturn: { url: location.pathname + location.search, state: location.state } } })}>查看结果</button></div></article>)}{ids.length < 10 && <button className="qcmp-add" onClick={() => setDialog("add")}><Plus size={18}/>添加运行</button>}</section>
    {!valid && !selection.error && <div className="qcmp-empty qcmp-card">{savedLoading ? "正在恢复保存的对比…" : "请选择至少2个已完成运行开始对比。"}</div>}
    {valid && !current && !error && <div className="qcmp-empty qcmp-card">正在读取所选运行的真实结果…</div>}
    {current && baselineRun && <>
      <section className="qcmp-card"><header><h2>关键指标</h2><span>比较基准 {runs.find(r => r.id === baseline)?.slot}；每列属于各自完整运行区间</span></header><div className="qcmp-table-scroll"><table className="qcmp-matrix"><thead><tr><th>指标</th>{runs.map(run => <th key={run.id}><span style={{ color: run.color }}>{run.slot}</span> · {run.label}</th>)}</tr></thead><tbody>{COMPARISON_METRICS.map(metric => {
        const baselineMetric = selectedMetric(metricRows, baseline, metric.key);
        const cells = runs.map(run => { const row = selectedMetric(metricRows, run.id, metric.key); return { run, row, reason: comparisonReason(baselineRun, run.summary!, baselineMetric, row) }; });
        const comparable = cells.every(cell => !cell.reason), values = cells.map(c => finite(c.row?.value));
        const best = comparable && metric.direction ? metric.direction === "max" ? Math.max(...values as number[]) : Math.min(...values.map(v => Math.abs(v!))) : null;
        return <tr key={metric.key}><th scope="row">{metric.name}{metric.key === "turnover" && <small>成交总额 / 平均日终权益</small>}</th>{cells.map(({ run, row, reason }) => { const value = finite(row?.value), selected = best !== null && value !== null && (metric.direction === "max" ? value === best : Math.abs(value) === best); return <td key={run.id}><strong>{displayNumber(row?.value, metric.percent)}</strong>{metric.key === "cumulative_fees" && <small>{comparisonConfig(run.summary!).currency || "币种未记录"}</small>}{selected && <span className="qcmp-best">{metric.direction === "max" ? "同口径最高" : "同口径最低"}</span>}{run.id !== baseline && <small>{reason || `相对基准 ${metricDifference(row?.value, baselineMetric?.value, metric.percent)}`}</small>}{value === null && <small>{row?.unavailable_reason || "未产出或存在多个计算版本"}</small>}{row && <EvidenceTable title="计算口径" value={row}/>}</td>; })}</tr>;
      })}</tbody></table></div><p className="qcmp-note">区间、计算口径或样本不同则不计算差值与最优标记。费用与换手率不作优劣排名。</p></section>
      <section className="qcmp-card"><header><h2>累计收益对比</h2><span>实际日期对齐 · 原始累计收益</span></header><ComparisonChart runs={runs} series={current.equity_curve_series} field="cumulative_return" title="累计收益对比"/><p className="qcmp-note">各序列保留原始起点；不同区间不会重算为统一期间收益。缺失数据断线，不补零、不沿用旧值。</p></section>
      <div className="qcmp-two"><section className="qcmp-card"><header><h2>回撤对比</h2><span>相对各自历史峰值</span></header><ComparisonChart runs={runs} series={current.equity_curve_series} field="drawdown" title="回撤对比"/></section><section className="qcmp-card"><header><h2>风险 / 收益分布</h2><span>完整运行区间</span></header><RiskScatter runs={runs} metrics={metricRows}/></section></div>
      <section className="qcmp-card"><header><h2>配置差异</h2><span>相对当前比较基准；未记录与空值分别展示</span></header><div className="qcmp-table-scroll"><table className="qcmp-matrix"><thead><tr><th>配置</th>{runs.map(run => <th key={run.id}>{run.slot} · {run.label}</th>)}</tr></thead><tbody><tr><th>策略 / 版本</th>{runs.map(run => <td key={run.id}>{run.label}</td>)}</tr><tr><th>回测区间</th>{runs.map(run => <td key={run.id}>{comparisonConfig(run.summary!).start_date || "—"} — {comparisonConfig(run.summary!).end_date || "—"}</td>)}</tr>{differencePaths.filter(path => !path.startsWith("data_evidence")).map(path => { const example = current.configuration_diff.find(d => d.fields?.[path])?.fields[path]; return <tr key={path}><th>{path.replace(/^[^.]+/, group => fieldLabels[group] || group)}</th>{runs.map(run => { const diff = current.configuration_diff.find(d => d.run_id === run.id)?.fields?.[path]; return <td key={run.id} className={diff ? "qcmp-changed" : ""}>{text(diff ? diff.current : example.baseline, diff ? diff.current_present : example.baseline_present)}</td>; })}</tr>; })}</tbody></table></div>{!differencePaths.length && <p className="qcmp-note">已记录配置未发现差异；不代表缺失证据相同。</p>}</section>
      <section className="qcmp-card"><header><h2>数据口径与证据</h2><span>数据覆盖、复权、时间边界与来源修订</span></header><div className="qcmp-evidence">{runs.map(run => <section key={run.id}><h3>{run.slot} · {run.label}</h3>{!run.summary?.data_evidence?.session_evidence_available && <p className="qcmp-note">缺少会话内最终检查证据，无法据此确认数据口径一致。</p>}<EvidenceTable title="查看数据与冻结配置" value={run.summary}/></section>)}</div></section>
    </>}
    {dialog === "add" && <AddRunsDrawer selected={ids} onToggle={id => change(ids.includes(id) ? ids.filter(v => v !== id) : ids.length < 10 ? [...ids, id] : ids)} onClose={() => setDialog(null)}/>}
    {dialog === "list" && <SavedListDrawer onClose={() => setDialog(null)} onOpen={openSaved} onRename={item => { setDialog(null); setRename(item); }} onDelete={item => { setDialog(null); setDeleteError(""); setRemoveSaved(item); }}/>}
    {(dialog === "save" || dialog === "saveAs") && <NameDrawer title={dialog === "saveAs" ? "另存对比" : "保存对比"} initialName={saved?.name || ""} onClose={() => setDialog(null)} onSubmit={name => save(name, dialog === "saveAs")}/>}
    {rename && <NameDrawer title="重命名对比" initialName={rename.name} onClose={() => { setRename(null); setDialog("list"); }} onSubmit={async name => { const updated = await updateComparison(rename.id, { version: rename.version, name }); if (savedId === updated.id) setSaved(updated); setRename(null); setDialog("list"); setToast("对比名称已更新。"); }}/>}
    {removeSaved && <ComparisonDrawer title="删除保存的对比" centered onClose={() => { if (!deleting.current) { setRemoveSaved(null); setDialog("list"); } }}><div className="qcmp-drawer-body"><p>确认删除“{removeSaved.name}”？</p><p className="qcmp-note">只删除此对比方案，原回测记录仍保留。</p>{deleteError && <p role="alert" className="qcmp-error">{deleteError} 请关闭后刷新保存列表再试。</p>}</div><footer><button disabled={deleteBusy} onClick={() => { setRemoveSaved(null); setDialog("list"); }}>取消</button><button className="qcmp-danger" disabled={deleteBusy} onClick={async () => { if (deleting.current) return; deleting.current = true; setDeleteBusy(true); try { await deleteComparison(removeSaved.id, removeSaved.version); if (savedId === removeSaved.id) change(ids, baseline, ""); setRemoveSaved(null); setDialog("list"); setToast("保存的对比已删除，回测记录仍保留。"); } catch (e) { setDeleteError(e instanceof Error ? e.message : "删除失败"); } finally { deleting.current = false; setDeleteBusy(false); } }}>{deleteBusy ? "删除中…" : "确认删除"}</button></footer></ComparisonDrawer>}
  </div>;
}
