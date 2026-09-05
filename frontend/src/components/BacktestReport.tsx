import { useEffect, useState } from "react";
import { fetchBacktestResult, type BacktestRun } from "../api/backtestRuns";
import { fetchBacktestPreflight } from "../api/backtestPreflight";

type Row = Record<string, any>;
export type Series = { run_id: string; points: Row[] };
const colors = ["#2563eb", "#dc2626", "#059669", "#9333ea", "#d97706", "#0891b2", "#be185d", "#4f46e5", "#4d7c0f", "#475569"];
const metricNames: Record<string, string> = { total_return: "总收益", annualized_return: "年化收益", max_drawdown: "最大回撤", volatility: "年化波动率", sharpe: "夏普比率", turnover: "换手率", cumulative_fees: "累计费用", fee_to_gross_traded_notional: "费用占成交额" };
const fieldNames: Record<string, string> = { parameters: "策略参数", backtest_config: "运行配置", data_request: "数据请求", behavior_versions: "行为版本", component_snapshot: "组件快照", account_snapshot: "账户与费用", data_evidence: "数据证据", random_seed: "随机种子" };
const number = (value: unknown) => value === null || value === undefined || value === "" ? null : Number.isFinite(Number(value)) ? Number(value) : null;
const text = (value: unknown) => value === undefined || value === null ? "未记录" : typeof value === "object" ? JSON.stringify(value) : String(value);

export function EvidenceTable({ title, value }: { title: string; value: unknown }) {
  const rows: [string, unknown][] = [];
  const flatten = (item: unknown, path: string) => {
    if (item && typeof item === "object" && !Array.isArray(item) && Object.keys(item).length) {
      Object.entries(item).forEach(([key, child]) => flatten(child, path ? `${path} / ${key}` : key));
    } else rows.push([path, item]);
  };
  flatten(value, "");
  return <details><summary>{title}</summary><table><tbody>{rows.map(([key, item]) => <tr key={key}><th scope="row">{key || title}</th><td style={{ overflowWrap: "anywhere" }}>{text(item)}</td></tr>)}</tbody></table></details>;
}

/** Calendar-aligned plots preserve gaps instead of connecting missing marks. */
export function BacktestCurve({ title, series, field, percent = false }: { title: string; series: Series[]; field: string; percent?: boolean }) {
  const parsed = series.map((item) => ({ ...item, points: item.points.map((point) => ({ x: Date.parse(String(point.as_of)), y: number(point[field]) })) }));
  const points = parsed.flatMap((item) => item.points).filter((p) => Number.isFinite(p.x) && p.y !== null);
  if (!points.length) return <section><h3>{title}</h3><p>暂无可绘制的数据。</p></section>;
  // Reduce bounds so long histories cannot exceed the JS argument limit.
  const { xmin, xmax, ymin, ymax } = points.reduce((bounds, p) => ({
    xmin: Math.min(bounds.xmin, p.x), xmax: Math.max(bounds.xmax, p.x),
    ymin: Math.min(bounds.ymin, p.y!), ymax: Math.max(bounds.ymax, p.y!),
  }), { xmin: Infinity, xmax: -Infinity, ymin: Infinity, ymax: -Infinity });
  const x = (v: number) => 80 + 680 * (v - xmin) / (xmax - xmin || 1);
  const y = (v: number) => 190 - 150 * (v - ymin) / (ymax - ymin || 1);
  const label = (v: number) => percent ? `${(v * 100).toFixed(2)}%` : v.toLocaleString(undefined, { maximumFractionDigits: 2 });
  return <section><h3>{title}</h3><svg role="img" aria-label={title} viewBox="0 0 800 240" style={{ width: "100%", maxHeight: 300 }}>
    <line x1="80" y1="190" x2="760" y2="190" stroke="currentColor" />
    <text x="5" y="42" fontSize="13">{label(ymax)}</text><text x="5" y="190" fontSize="13">{label(ymin)}</text>
    <text x="80" y="220" fontSize="13">{new Date(xmin).toISOString().slice(0, 10)}</text><text x="660" y="220" fontSize="13">{new Date(xmax).toISOString().slice(0, 10)}</text>
    {parsed.map((item, index) => {
      let connected = false;
      const d = item.points.map((point) => {
        if (point.y === null || !Number.isFinite(point.x)) { connected = false; return ""; }
        const command = `${connected ? "L" : "M"}${x(point.x)},${y(point.y)}`; connected = true; return command;
      }).join(" ");
      return <g key={item.run_id}><path d={d} fill="none" stroke={colors[index % colors.length]} strokeWidth="2" />{item.points.filter((p) => p.y !== null && Number.isFinite(p.x)).map((p, i) => <circle key={i} cx={x(p.x)} cy={y(p.y!)} r="2" fill={colors[index % colors.length]}><title>{`${item.run_id} · ${new Date(p.x).toISOString().slice(0, 10)} · ${label(p.y!)}`}</title></circle>)}</g>;
    })}
  </svg><ul>{series.map((item, index) => <li key={item.run_id} style={{ color: colors[index % colors.length] }}>{item.run_id}</li>)}</ul></section>;
}

function metricValue(row?: Row) {
  if (!row) return "未产出";
  const value = number(row.value);
  if (value === null) return `不可计算：${row.unavailable_reason || "证据不足"}`;
  if (["total_return", "annualized_return", "max_drawdown", "volatility", "fee_to_gross_traded_notional"].includes(row.metric_key)) return `${(value * 100).toFixed(2)}%`;
  return value.toLocaleString(undefined, { maximumFractionDigits: 4 });
}

export function MetricMatrix({ metrics, ids }: { metrics: Row[]; ids: string[] }) {
  const keys = [...new Set(metrics.map((row) => row.metric_key))];
  return <table><caption>绩效与费用指标</caption><thead><tr><th>指标</th>{ids.map((id) => <th key={id}>{id}</th>)}</tr></thead><tbody>{keys.map((key) => {
    const rows = ids.map((id) => metrics.find((row) => row.run_id === id && row.metric_key === key));
    const conventions = rows.map((row) => row ? JSON.stringify([row.formula_version, row.analyzer_key, row.analyzer_version, row.annualization_factor, row.risk_free_rate_note, row.analyzer_metadata]) : null);
    return <tr key={key}><th scope="row">{metricNames[key] || key}{new Set(conventions).size > 1 && <p>口径或产出不同</p>}</th>{rows.map((row, i) => <td key={ids[i]}>{metricValue(row)}{row && <EvidenceTable title="公式与口径" value={{ formula_version: row.formula_version, analyzer_key: row.analyzer_key, analyzer_version: row.analyzer_version, sample_count: row.sample_count, annualization_factor: row.annualization_factor, risk_free_rate_note: row.risk_free_rate_note, ...row.analyzer_metadata }} />}</td>)}</tr>;
  })}</tbody></table>;
}

export function BacktestComparisonView({ result }: { result: Row }) {
  const summaries: Row[] = result.run_summaries || result.summaries || [];
  return <div>
    <p>曲线按日期对齐；账户权益使用原始金额，初始资金或持仓不同会影响比较。</p>
    <BacktestCurve title="账户权益叠加" series={result.equity_curve_series || []} field="equity" />
    <BacktestCurve title="回撤叠加" series={result.drawdown_curve_series || []} field="drawdown" percent />
    <MetricMatrix metrics={result.metric_matrix || []} ids={summaries.map((item) => item.run_id)} />
    <h3>配置与数据口径差异</h3>
    {(result.configuration_diff || []).map((diff: Row) => <section key={diff.run_id}><h4>{diff.run_id}</h4>{!Object.keys(diff.fields || {}).length ? <p>与基准已记录字段一致；缺失证据仍需单独确认。</p> : <table><thead><tr><th>字段</th><th>基准运行</th><th>当前运行</th></tr></thead><tbody>{Object.entries(diff.fields).map(([key, raw]) => { const item = raw as Row; return <tr key={key}><th>{key.replace(/^[^.]+/, (group) => fieldNames[group] || group)}</th><td>{item.baseline_present ? text(item.baseline) : "未记录"}</td><td>{item.current_present ? text(item.current) : "未记录"}</td></tr>; })}</tbody></table>}</section>)}
    {summaries.map((summary) => <section key={summary.run_id}><h4>{summary.run_id} 数据证据</h4>{!summary.data_evidence?.session_evidence_available && <p role="alert">缺少会话内最终预检证据，不能据此认定数据口径相同。</p>}<EvidenceTable title="PIT、覆盖度、复权与来源修订" value={summary.data_evidence} /><EvidenceTable title="冻结配置与行为版本" value={summary} /></section>)}
  </div>;
}

async function allResults(runId: string, kind: string, alive: () => boolean, signal: AbortSignal): Promise<Row[]> {
  let cursor: string | undefined;
  const rows: Row[] = [];
  const seen = new Set<string>();
  do {
    const page = await fetchBacktestResult(runId, kind, cursor, signal);
    if (!alive()) return [];
    rows.push(...page.items);
    cursor = page.next_cursor || undefined;
    if (page.has_more && !cursor) throw new Error("结果分页缺少后续游标。");
    if (cursor && seen.has(cursor)) throw new Error("结果分页游标重复。");
    if (cursor) seen.add(cursor);
  } while (cursor);
  return rows;
}

/** Query only the selected close, so long runs never preload every holding. */
function PositionTable({ run, dates }: { run: BacktestRun; dates: string[] }) {
  const [chosenDate, setChosenDate] = useState("");
  const [cursor, setCursor] = useState<string>();
  const [page, setPage] = useState<{ items: Row[]; next_cursor: string | null } | null>(null);
  const [error, setError] = useState("");
  const positionDate = dates.includes(chosenDate) ? chosenDate : dates.at(-1) || "";
  useEffect(() => {
    const controller = new AbortController();
    setPage(null); setError("");
    if (positionDate) fetchBacktestResult(run.run_id, "positions", cursor, controller.signal, { start_time: positionDate, end_time: positionDate })
      .then((value) => { if (!controller.signal.aborted) setPage({ items: value.items, next_cursor: value.next_cursor || null }); })
      .catch((failure: Error) => { if (!controller.signal.aborted) setError(failure.message); });
    return () => controller.abort();
  }, [run.run_id, run.status, positionDate, cursor]);
  return <section>
    <label>日终持仓时点<select value={positionDate} onChange={(event) => { setChosenDate(event.target.value); setCursor(undefined); }}>{dates.map((day) => <option key={day}>{day}</option>)}</select></label>
    {error && <p role="alert">{error}</p>}
    {positionDate && !page && !error && <p>正在加载持仓…</p>}
    {page && <><table><thead><tr><th>标的</th><th>数量</th><th>可用数量</th><th>市值</th></tr></thead><tbody>{page.items.map((row) => <tr key={`${row.instrument_id}:${row.side}`}><th>{row.event_display_name || row.event_name || row.event_trading_code || "展示信息缺失"}</th><td>{text(row.quantity)}</td><td>{text(row.available_quantity)}</td><td>{row.mark_price == null ? "估值缺失" : (Number(row.quantity) * Number(row.mark_price)).toLocaleString()}</td></tr>)}</tbody></table>
      {!page.items.length && <p>该时点没有已记录的非零持仓。</p>}
      {page.next_cursor && <button type="button" onClick={() => setCursor(page.next_cursor!)}>下一页持仓</button>}
      {cursor && <button type="button" onClick={() => setCursor(undefined)}>返回持仓首页</button>}
    </>}
  </section>;
}

export function BacktestReport({ run }: { run: BacktestRun }) {
  const [data, setData] = useState<{ equity: Row[]; metrics: Row[]; preflight: unknown } | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true; const controller = new AbortController(); setData(null); setError("");
    Promise.all([allResults(run.run_id, "equity", () => active, controller.signal), allResults(run.run_id, "metrics", () => active, controller.signal), fetchBacktestPreflight(run.run_id)])
      .then(([equity, metrics, preflight]) => { if (active) setData({ equity, metrics, preflight }); })
      .catch((failure: Error) => { if (active) setError(failure.message); });
    return () => { active = false; controller.abort(); };
  }, [run.run_id, run.status]);
  const dates = [...new Set(data?.equity.map((item) => String(item.as_of)) || [])];
  return <section aria-label="回测研究报告">
    <p>{run.status === "succeeded" ? "已完成运行" : "未完成结果：仅展示已持久化部分"}；完整性：{run.result_integrity_status || "待核实"}</p>
    {error && <p role="alert">{error}</p>}{!data && !error && <p>正在加载报告…</p>}
    {data && <><MetricMatrix metrics={data.metrics.map((row) => ({ ...row, run_id: run.run_id }))} ids={[run.run_id]} />{!data.metrics.length && <p>本次运行未产出指标，请检查分析器选择和运行状态。</p>}
      <BacktestCurve title="账户权益" series={[{ run_id: run.run_id, points: data.equity }]} field="equity" />
      <BacktestCurve title="回撤" series={[{ run_id: run.run_id, points: data.equity }]} field="drawdown" percent />
      <BacktestCurve title="现金与持仓市值变化" series={[{ run_id: "现金", points: data.equity.map((row) => ({ ...row, amount: row.cash })) }, { run_id: "持仓市值", points: data.equity.map((row) => ({ ...row, amount: row.market_value })) }]} field="amount" />
      <PositionTable key={run.run_id} run={run} dates={dates} />
      <EvidenceTable title="页面准入与会话内最终数据证据" value={data.preflight} />
    </>}
    <EvidenceTable title="策略版本、参数与数据请求" value={{ revision: run.strategy_revision_id, parameters: run.parameters, config: run.backtest_config, data_request: run.data_request }} />
    <EvidenceTable title="账户、费用、滑点与行为版本" value={{ account: run.account_profile_id, account_version: run.account_profile_version, fee_schedule: run.fee_schedule_key, fee_version: run.fee_schedule_version, components: run.component_snapshot, behavior_versions: run.behavior_versions }} />
    {Array.isArray((run.backtest_config as Row)?.metadata?.disabled_metrics) && (run.backtest_config as Row).metadata.disabled_metrics.length > 0 && <p role="status">已禁用指标：{(run.backtest_config as Row).metadata.disabled_metrics.join("、")}；相应数据源不满足计算条件。</p>}
    <EvidenceTable title="退出码、完成标记与终态决议" value={{ exit_code: run.child_exit_code, completion_marker: run.completion_marker, integrity: run.result_integrity_evidence, decision: run.terminal_decision_reason }} />
  </section>;
}
