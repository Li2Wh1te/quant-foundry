import { useEffect, useRef, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, Download, RefreshCw } from "lucide-react";
import { useAuth } from "../auth/AuthContext";
import { getRun, onRunAccessDenied, type RunSummary } from "../api/runService";
import { onResultHidden, resultSummary, type ResultSummary } from "../api/backtestResults";
import { EvidenceTable } from "../components/EvidenceTable";
import { ResultChart } from "./backtests/ResultChart";
import { PageFeedback, PageNavigation, ResultTables } from "./backtests/ResultTables";
import { chartRows, displayNumber, METRICS, summaryMetrics } from "./backtests/resultData";
import { useResultPage } from "./backtests/useResultPage";
import { comparisonUrl } from "./backtests/comparisonData";
import { STATUS, terminal } from "./backtests/run-ui/config";
import "./backtests/Workbench.css";
import "./backtests/Result.css";

export function BacktestResultPage() { const { runId = "" } = useParams(); return <Report key={runId} id={runId}/>; }
function Report({ id }: { id: string }) {
  const location = useLocation(), navigate = useNavigate(), { logout } = useAuth(), origin = location.state as { from?: string; comparisonReturn?: { url: string; state?: any } } | null;
  const [run, setRun] = useState<RunSummary | null>(null), [summary, setSummary] = useState<ResultSummary | null>(null), [error, setError] = useState(""), [summaryError, setSummaryError] = useState("");
  const revoked = useRef(false);
  const [loading, setLoading] = useState(false), [refresh, setRefresh] = useState(0), [denied, setDenied] = useState(false);
  useEffect(() => onRunAccessDenied(status => { revoked.current = true; setRun(null); setSummary(null); setLoading(false); setDenied(true); setError(status === 401 ? "登录状态已失效。" : "权限已撤销，已清除结果。" ); if (status === 401) { logout(); navigate("/login", { replace: true }); } }), [logout, navigate]);
  useEffect(() => onResultHidden(ids => { if (ids.includes(id)) { revoked.current = true; setRun(null); setSummary(null); setLoading(false); setDenied(true); setError("运行不存在或已不可读取，已清除缓存结果。"); } }), [id]);
  useEffect(() => {
    if (denied) return;
    const c = new AbortController(); setLoading(true); setError("");
    getRun(id, c.signal).then(value => { if (!c.signal.aborted && !revoked.current) setRun(value); }).catch(e => { if (!c.signal.aborted && !revoked.current) { if (e.status === 404) { revoked.current = true; setRun(null); setSummary(null); setDenied(true); } setError(e.message); setLoading(false); } }).finally(() => { if (!c.signal.aborted && !revoked.current) setLoading(false); });
    return () => c.abort();
  }, [id, refresh, denied]);
  const enabled = !!run && terminal(run.status) && run.status !== "indeterminate" && !denied;
  useEffect(() => {
    const c = new AbortController(); setSummaryError("");
    if (!enabled) { setSummary(null); return () => c.abort(); }
    resultSummary(id, c.signal).then(value => { if (!c.signal.aborted && !revoked.current) setSummary(value); }).catch(e => { if (!c.signal.aborted && !revoked.current) { if (e.status === 404) { setRun(null); setSummary(null); } setSummaryError(e.message); } });
    return () => c.abort();
  }, [id, enabled, refresh]);
  const curve = useResultPage(id, "equity", new URLSearchParams(location.search).get("curve") || "", enabled, refresh);
  const metrics = summaryMetrics(summary), config = (run?.normalized_config || (run as any)?.backtest_config?.spec || (run as any)?.backtest_config || {}) as Record<string, any>;
  const legacy = run?.engine_kind === "legacy_python";
  const returnUrl = origin?.comparisonReturn?.url?.match(/^\/admin\/backtest-compare(?:\?|$)/) ? origin.comparisonReturn : null;
  const back = origin?.from?.match(/^\/admin\/(?:backtest-runs|strategies\/[^/]+\/backtests)(?:\?|$)/) ? origin.from : "/admin/backtest-runs";
  function download() {
    const blob = new Blob([JSON.stringify({ run, summary, equity_page: curve.page, scope: "current_summary_and_equity_page_only" }, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob), a = document.createElement("a"); a.href = url; a.download = `backtest-${id}-page.json`; a.click(); setTimeout(() => URL.revokeObjectURL(url), 0);
  }
  return <div className="qfr-page" data-results-ui="s3-d16"><div className="qfr-heading"><div><div className="qfb-eyebrow">BACKTEST RESULT</div><h1>{legacy ? "历史结果 · 只读" : "回测结果"}</h1><p>保存值与模型假设；曲线和明细按页读取。</p><code>{id}</code></div><div className="qfr-actions"><button onClick={() => navigate(returnUrl?.url || back, { state: returnUrl?.state || location.state })}><ArrowLeft size={16}/>{returnUrl ? "返回对比" : "返回工作台"}</button><button disabled={loading || denied} onClick={() => setRefresh(v => v + 1)}><RefreshCw size={16}/>刷新</button><button disabled={!summary || denied} onClick={download}><Download size={16}/>导出摘要与当前权益页</button></div></div>
    {error && <p className="qfr-error" role="alert">{error}{run && " 当前保留上次读取的运行状态。"}</p>}{summaryError && <p className="qfr-error" role="alert">摘要读取失败：{summaryError}{summary && " 当前保留上次读取的摘要。"}<button onClick={() => setRefresh(v => v + 1)}>重试摘要</button></p>}
    {!run ? <p className="qfr-empty">{loading ? "正在读取运行…" : "此运行不可读取，请核对权限或链接。"}</p> : <main className="qfr-main">
      <section className="qfr-card qfr-run-strip"><div><strong>{legacy ? "legacy_python · 原保存值" : "Rust 引擎"}</strong><span className="qfb-badge">{STATUS[run.status] || run.status}</span><small>{config.start || config.start_date || "区间未记录"} — {config.end || config.end_date || "区间未记录"} · {config.frequency || "频率未记录"}</small></div><div><small>初始资金</small><strong>{displayNumber(config.initial_cash ?? (run as any).initial_cash)} {config.currency || "CNY"}</strong></div><div className="qfr-actions"><button disabled={!enabled} onClick={() => navigate(comparisonUrl([id]), { state: { from: back } })}>加入对比</button>{legacy && <button onClick={() => navigate("/admin/backtest-runs?create=1")}>迁入新配置</button>}</div></section>
      {(run.partial || summary?.partial) && <p role="status" className="qfr-error">部分结果：运行失败或取消；不能据此得出完整收益结论。</p>}
      {legacy && <p className="qfr-note">旧数据按原保存值只读展示，未保存的指标不补算。迁入需发布新的 S3 修订并重新配置。</p>}
      {!enabled && <p role="status">{run.status === "indeterminate" ? "历史结果待判定，受保护的明细不可读取。" : "运行尚未终结，请返回工作台跟踪状态。"}</p>}
      <section className="qfr-kpis" id="overview">{METRICS.map(metric => { const matches = metrics.filter(row => row.metric_key === metric.key), row = matches.length === 1 ? matches[0] : undefined; return <div key={metric.key}><span>{metric.name}</span><strong>{displayNumber(row?.value, metric.percent)}</strong><small>{row?.value == null ? row?.unavailable_reason || "未保存或存在多个公式版本" : legacy ? "原保存值" : metric.percent ? "float64 统计量" : "服务原值"}</small>{row && <EvidenceTable title="查看原值与口径" value={row}/>}</div>; })}</section>
      <section className="qfr-card" id="curve"><header><h2>权益与收益曲线</h2><span>仅当前页 · 保存的采样点</span></header><PageFeedback data={curve}/>{curve.page && <><p className="qfr-note">采样：{summary?.performance?.policy?.curve_sampling || config.result_sampling || "原历史采样未记录"}。最多 100 点 / 页；不是完整 Tick 曲线，不将分页点重算为月度或全年收益。</p><ResultChart rows={chartRows(curve.page.items)} field="equity" percent={false} title="权益" timezone="Asia/Shanghai"/><ResultChart rows={chartRows(curve.page.items)} field="cumulative_return" title="累计收益" timezone="Asia/Shanghai"/><ResultChart rows={chartRows(curve.page.items)} field="drawdown" title="回撤" timezone="Asia/Shanghai"/><PageNavigation data={curve} cursorKey="curve" title="权益"/></>}</section>
      <ResultTables runId={id} enabled={enabled} version={refresh} legacy={legacy}/>
      <section className="qfr-card" id="configuration"><header><h2>模型、公式与费用</h2><span>本次受理配置 / 原历史记录</span></header><div className="qfr-body"><p>无基准时不提供基准收益与超额。当前修订数据不代表历史快照；缺少生产映射的能力仍不可用。</p><EvidenceTable title="运行配置与费用" value={run}/><EvidenceTable title="结果、模型假设、统计公式与截断说明" value={summary}/></div></section>
    </main>}
  </div>;
}
