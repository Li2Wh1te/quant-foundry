import { useEffect, useRef, useState } from "react";
import { fetchBacktestResult, type BacktestResultPage } from "../../api/backtestRuns";
import { Select } from "../../components/controls/Select";
import { displayNumber, RESULT_KINDS, type ResultRow } from "./resultData";
import { finite } from "./workbench";
const sides: Record<string, string> = { buy: "买入", sell: "卖出", long: "多头", short: "空头", net: "净持仓" };
const statuses: Record<string, string> = { filled: "已成交", partially_filled: "部分成交", cancelled: "已取消", rejected: "已拒绝", submitted: "已提交", accepted: "已接受", pending: "待处理" };
const stamp = (v: unknown) => v ? new Date(String(v)).toLocaleString("zh-CN", { hour12: false, timeZone: "Asia/Shanghai" }) : "—";
function Instrument({ row }: { row: ResultRow }) { return <><strong>{row.event_display_name || row.event_name || row.event_trading_code || "标的展示信息未记录"}</strong><small>{row.event_trading_code || "代码未记录"}</small><details><summary>标的身份</summary><code>{row.instrument_id}</code></details></>; }
/** Page only the selected evidence kind/date; a cursor is scoped to both. */
export function ResultTables({ runId, dates, disabled, version }: { runId: string; dates: string[]; disabled: boolean; version: string }) {
  const [kind, setKind] = useState("fills"), [date, setDate] = useState("");
  const [cursors, setCursors] = useState<Array<string | undefined>>([undefined]);
  const [storedPage, setPage] = useState<(BacktestResultPage & { scope: string }) | null>(null), [error, setError] = useState(""), [loading, setLoading] = useState(false), [retry, setRetry] = useState(0);
  const selectedDate = dates.includes(date) ? date : dates.at(-1) || "";
  const scope = `${runId}:${kind}:${kind === "positions" ? selectedDate : ""}`;
  const page = storedPage?.scope === scope ? storedPage : null;
  const cursorScope = `${kind}:${selectedDate}`;
  const previousScope = useRef(cursorScope);
  const cursor = previousScope.current === cursorScope ? cursors.at(-1) : undefined;
  useEffect(() => { if (previousScope.current !== cursorScope) { previousScope.current = cursorScope; setCursors([undefined]); } }, [cursorScope]);
  useEffect(() => {
    const controller = new AbortController(); setError("");
    if (disabled || kind === "positions" && !selectedDate) { setPage(null); return; }
    setLoading(true);
    fetchBacktestResult(runId, kind, cursor, controller.signal, kind === "positions" ? { start_time: selectedDate, end_time: selectedDate } : {}).then(value => {
      if (!controller.signal.aborted) { if (value.truncated || value.has_more && !value.next_cursor || value.next_cursor && cursors.includes(value.next_cursor)) throw new Error("明细分页异常，无法继续读取。"); setPage({ ...value, scope }); }
    }).catch(e => { if (!controller.signal.aborted) setError(e.message); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [runId, kind, selectedDate, cursor, disabled, version, retry]);
  const change = (value: string) => { setKind(value); setCursors([undefined]); setPage(null); };
  return <section className="qfr-card" id="holding"><header><h2>持仓与交易</h2><span>事件时名称与代码</span></header>
    <div className="qfr-table-controls"><label>明细类型<Select value={kind} onChange={e => change(e.target.value)}>{Object.entries(RESULT_KINDS).filter(([key]) => !["data-preflight", "data-chunks"].includes(key)).map(([key, name]) => <option value={key} key={key}>{name}</option>)}</Select></label>
    {kind === "positions" && <label>日终持仓时点<Select value={selectedDate} onChange={e => { setDate(e.target.value); setCursors([undefined]); setPage(null); }}>{dates.map(d => <option key={d} value={d}>{stamp(d)}</option>)}</Select></label>}
    <span className="qfr-loading" role="status">{loading ? "正在读取明细…" : ""}</span></div>
    {error && <p className="qfr-error" role="alert">{error}<button onClick={() => setRetry(v => v + 1)}>重试</button></p>}
    {disabled ? <p className="qfr-empty">结果待判定，暂不开放明细读取。</p> : kind === "positions" && !selectedDate ? <p className="qfr-empty">没有已记录的日终估值时点。</p> : page && <>
      {page.items.length ? <div className="qfr-table-scroll">{["fills", "orders", "positions"].includes(kind) ? <table><thead><tr><th>{kind === "positions" ? "标的" : "时间 / 标的"}</th><th>方向</th><th>数量</th>{kind === "positions" ? <><th>可用数量</th><th>平均成本</th><th>估值 / 市值</th><th>已实现 / 未实现损益</th></> : kind === "fills" ? <><th>成交价</th><th>费用</th><th>滑点金额</th></> : <><th>委托价</th><th>成交数量</th><th>状态</th></>}</tr></thead>
        <tbody>{page.items.map((raw, index) => { const row = raw as ResultRow; const mark = finite(row.mark_price), quantity = finite(row.quantity); return <tr key={String(row.fill_id || row.order_id || `${row.instrument_id}:${row.side}:${index}`)}><td>{kind !== "positions" && <small>{stamp(row.timestamp || row.submitted_at)}</small>}<Instrument row={row}/></td><td>{sides[row.side] || row.side || "—"}</td><td>{displayNumber(row.quantity)}</td>
        {kind === "positions" ? <><td>{displayNumber(row.available_quantity)}</td><td>{displayNumber(row.average_price)}</td><td>{displayNumber(row.mark_price)}<small>市值 {displayNumber(mark !== null && quantity !== null ? mark * quantity : null)}</small></td><td>{displayNumber(row.realized_pnl)} / {displayNumber(row.unrealized_pnl)}</td></> : kind === "fills" ? <><td>{displayNumber(row.price)}</td><td>{displayNumber(row.fees)}</td><td>{displayNumber(row.slippage_amount)}</td></> : <><td>{displayNumber(row.price)}</td><td>{displayNumber(row.filled_quantity)}</td><td>{statuses[row.status] || row.status}<small>{row.status_reason}</small></td></>}</tr>; })}</tbody></table> : <div className="qfr-records">{page.items.map((row, i) => <details key={i}><summary>{RESULT_KINDS[kind as keyof typeof RESULT_KINDS]} · 本页第 {i + 1} 条</summary><pre>{JSON.stringify(row, null, 2)}</pre></details>)}</div>}</div> : <p className="qfr-empty">{kind === "positions" ? "该时点没有已记录的非零持仓。" : "本次运行没有此类记录。"}</p>}
      <footer><span>第 {cursors.length} 页 · 本页 {page.items.length} 条 · 按时间顺序</span><button disabled={loading || cursors.length === 1} onClick={() => { setCursors(c => c.slice(0, -1)); setPage(null); }}>上一页</button><button disabled={loading || !page.next_cursor} onClick={() => { setCursors(c => [...c, page.next_cursor!]); setPage(null); }}>下一页</button></footer>
    </>}
  </section>;
}
