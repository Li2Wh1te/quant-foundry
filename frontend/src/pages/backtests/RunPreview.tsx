import { useEffect, useState } from "react";
import { fetchBacktestResult, type WorkbenchRun } from "../../api/backtestRuns";
import { METRICS, finite, metricText, runConfig, STATUS } from "./workbench";
type Row = Record<string, any>;
export function RunPreview({ run }: { run: WorkbenchRun }) {
  const [data, setData] = useState<{ id: string; equity: Row[]; metrics: Row[] } | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController(); setError("");
    async function all(kind: string) {
      const rows: Row[] = [], seen = new Set<string>(); let cursor: string | undefined;
      do {
        const page = await fetchBacktestResult(run.run_id, kind, cursor, controller.signal);
        rows.push(...page.items); cursor = page.next_cursor || undefined;
        if (page.has_more && !cursor || cursor && seen.has(cursor)) throw new Error("结果分页异常，请刷新重试。");
        if (cursor) seen.add(cursor);
      } while (cursor && !controller.signal.aborted);
      return rows;
    }
    // Indeterminate roots intentionally reject result reads; show their evidence.
    if (run.status === "indeterminate") { setData({ id: run.run_id, equity: [], metrics: [] }); return; }
    Promise.all([all("equity"), all("metrics")]).then(([equity, metrics]) => {
      if (!controller.signal.aborted) setData({ id: run.run_id, equity, metrics });
    }).catch(e => { if (!controller.signal.aborted) setError(e.message); });
    return () => controller.abort();
  }, [run.run_id, run.status, run.completed_steps]);
  const current = data?.id === run.run_id ? data : null, config = runConfig(run);
  const waiting = !current && !error;
  const reason = run.status === "succeeded" ? "本次运行未产出此指标" : run.status === "indeterminate" ? "结果尚未确定" : "运行尚未完成，指标未产出";
  return <>
    {error && <p className="qfb-error" role="alert">{error}</p>}
    <div className="qfb-metrics">{METRICS.map(metric => {
      const row = current?.metrics.find(item => item.metric_key === metric.key);
      const value = finite(row?.value);
      return <section key={metric.key}><span>{metric.name}</span><strong className={metric.key.includes("return") && value !== null ? value >= 0 ? "qfb-positive" : "qfb-negative" : ""}>{metricText(row?.value, metric.percent)}</strong><small>{waiting ? "正在读取指标…" : !row ? reason : value === null ? row.unavailable_reason || "计算依据不足" : metric.key === "turnover" ? "成交总额 / 平均日终权益" : row.annualization_factor ? `年化因子 ${row.annualization_factor}` : "按本次运行实际结果"}</small></section>;
    })}</div>
    <section className="qfb-card"><header><h3>账户权益曲线</h3><span>账户权益 · CNY</span></header><EquityChart rows={current?.equity || []} waiting={waiting} /></section>
    <div className="qfb-bottom"><section className="qfb-card"><header><h3>运行配置</h3><span>创建时冻结</span></header><dl>
      <dt>策略</dt><dd>{run.strategy_name || "策略信息未提供"} · {run.revision_number ? `版本 ${run.revision_number}` : "版本信息未提供"}</dd>
      <dt>回测账户</dt><dd>{String((run.backtest_config?.account as Row)?.name || run.account_profile_id || "未记录")} · v{run.account_profile_version || "—"}</dd>
      <dt>回测区间</dt><dd>{config.start_date || "—"} — {config.end_date || "—"}</dd><dt>初始资金</dt><dd>{String(config.initial_cash ?? "—")} {config.currency || ""}</dd>
      <dt>策略参数</dt><dd>{Object.keys(run.parameters || {}).length ? <pre>{JSON.stringify(run.parameters, null, 2)}</pre> : "无额外参数"}</dd>
    </dl></section><section className="qfb-card"><header><h3>运行状态</h3><span className={`qfb-badge qfb-${run.status}`}>{STATUS[run.status]}</span></header><dl>
      <dt>进度</dt><dd>{Math.round(Number(run.progress_ratio || 0)*100)}% {run.total_steps != null && ` · ${run.completed_steps || 0} / ${run.total_steps} 步`}</dd>
      <dt>当前交易日</dt><dd>{run.current_trading_date || "—"}</dd><dt>创建时间</dt><dd>{dateText(run.created_at)}</dd><dt>完成时间</dt><dd>{dateText(run.finished_at)}</dd>
      <dt>准入检查</dt><dd>{run.formal_gates?.allowed === true ? "创建时检查通过" : "可在完整结果中查看检查证据"}</dd>
    </dl>{run.error_message && <p className="qfb-error">{run.error_message}</p>}{run.status !== "succeeded" && <p className="qfb-note">{run.status === "indeterminate" ? "结果待判定，请查看终态证据。" : "当前展示已保存的结果，未完成运行的指标可能不完整。"}</p>}</section></div>
  </>;
}
export function dateText(value?: string | null) { return value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "—"; }
function EquityChart({ rows, waiting }: { rows: Row[]; waiting: boolean }) {
  const parsed = rows.map(row => ({ x: Date.parse(row.as_of), y: finite(row.equity) }));
  const valid = parsed.filter(p => Number.isFinite(p.x) && p.y !== null);
  if (!valid.length) return <div className="qfb-chart-empty">{waiting ? "正在读取权益曲线…" : "尚无可绘制的权益数据"}</div>;
  const bounds = valid.reduce((b,p) => [Math.min(b[0],p.x),Math.max(b[1],p.x),Math.min(b[2],p.y!),Math.max(b[3],p.y!)], [Infinity,-Infinity,Infinity,-Infinity]);
  const [xmin,xmax,ymin,ymax] = bounds;
  const x = (v:number) => 90 + 680*(v-xmin)/(xmax-xmin || 1), y=(v:number) => 200-160*(v-ymin)/(ymax-ymin || 1);
  let connected = false;
  const path = parsed.map(p => { if (!Number.isFinite(p.x) || p.y === null) { connected=false; return ""; } const d = `${connected ? "L" : "M"}${x(p.x)},${y(p.y)}`; connected=true; return d; }).join(" ");
  return <svg className="qfb-chart" role="img" aria-label="账户权益曲线，金额单位人民币" viewBox="0 0 820 250">
    {[0,.5,1].map(f => <g key={f}><line x1="90" x2="770" y1={40+160*f} y2={40+160*f} stroke="var(--border)" strokeDasharray="4 4"/><text x="8" y={45+160*f}>{(ymax-(ymax-ymin)*f).toLocaleString("zh-CN", { maximumFractionDigits: 2 })}</text></g>)}
    <path d={path} fill="none" stroke="var(--orange)" strokeWidth="2"/>
    {valid.map((p,i) => <circle key={i} cx={x(p.x)} cy={y(p.y!)} r="2" fill="var(--orange)"><title>{new Date(p.x).toISOString().slice(0,10)} · {p.y!.toLocaleString()} CNY</title></circle>)}
    <text x="90" y="235">{new Date(xmin).toISOString().slice(0,10)}</text><text x="690" y="235">{new Date(xmax).toISOString().slice(0,10)}</text>
  </svg>;
}
