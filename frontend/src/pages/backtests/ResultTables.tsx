import { useLocation, useNavigate } from "react-router-dom";
import { Select } from "../../components/controls/Select";
import type { ResultKind, ResultRow } from "../../api/backtestResults";
import { displayNumber, RESULT_KINDS } from "./resultData";
import { useResultPage } from "./useResultPage";

export function PageFeedback({ data }: { data: ReturnType<typeof useResultPage> }) {
  return <>{data.loading && <p role="status">正在读取本页… <button onClick={data.cancel}>取消加载</button></p>}
    {data.error && <p className="qfr-error" role="alert">本节读取失败：{data.error} {data.page && "当前展示上次读取的本页。"}<button onClick={data.reload}>重试本节</button></p>}
    {data.cancelled && <p role="status">本页加载已取消，尚未确认的内容不作为空结果。<button onClick={data.reload}>重新加载</button></p>}</>;
}
/** Paging state lives in the URL, so reload/back/result navigation preserves it. */
export function PageNavigation({ data, cursorKey, title }: { data: ReturnType<typeof useResultPage>; cursorKey: string; title: string }) {
  const location = useLocation(), navigate = useNavigate(), query = new URLSearchParams(location.search);
  const current = query.get(cursorKey) || "", page = data.page;
  function change(cursor: string) {
    const next = new URLSearchParams(location.search); cursor ? next.set(cursorKey, cursor) : next.delete(cursorKey);
    navigate({ pathname: location.pathname, search: next.toString(), hash: location.hash }, { state: location.state });
  }
  return <footer aria-label={`${title}分页`}><span>本页 {page?.items.length ?? "—"} 条 · 每页最多 100 条{page?.partial ? " · 部分结果" : ""}</span>
    <button disabled={!current || data.loading} onClick={() => change("")}>首页</button>
    <button disabled={data.loading || !page?.next_cursor} onClick={() => change(page!.next_cursor!)}>下一页</button></footer>;
}
function Cell({ value }: { value: unknown }) { return <>{value == null ? "—" : typeof value === "object" ? JSON.stringify(value) : typeof value === "string" ? value : displayNumber(value)}</>; }
const columns: Record<string, [string, string[]][]> = {
  equity: [["时间 / 纳秒", ["time_ns", "as_of"]], ["权益", ["equity"]], ["现金", ["cash"]], ["估值状态 / 原因", ["unavailable_reason", "valuation_reason", "valuation_status"]], ["累计费用", ["cumulative_fees"]]],
  orders: [["订单", ["order_id"]], ["标的", ["security", "event_trading_code", "instrument_id"]], ["方向", ["side"]], ["数量", ["quantity"]], ["成交数量", ["filled_quantity"]], ["限价", ["limit_price", "price"]], ["状态 / 原因", ["status", "reason_code", "status_reason"]]],
  trades: [["成交", ["trade_id", "fill_id"]], ["标的", ["security", "event_trading_code", "instrument_id"]], ["方向", ["side"]], ["数量", ["quantity"]], ["原始成交价", ["price"]], ["费用", ["fee", "fees"]], ["时间 / 纳秒", ["execution_time_ns", "time_ns", "timestamp"]]],
  positions: [["标的", ["security", "event_trading_code", "instrument_id"]], ["数量", ["quantity"]], ["可卖数量", ["sellable", "available_quantity"]], ["平均成本", ["average_cost", "average_price"]], ["市值（原保存）", ["market_value"]]],
};
export function ResultTables({ runId, enabled, version, legacy }: { runId: string; enabled: boolean; version: number; legacy: boolean }) {
  const location = useLocation(), navigate = useNavigate(), query = new URLSearchParams(location.search);
  const selected = query.get("detail") || "trades", kind = Object.hasOwn(RESULT_KINDS, selected) ? selected as ResultKind : "trades";
  const data = useResultPage(runId, kind, query.get("after") || "", enabled, version), fields = columns[kind];
  const unsupported = legacy && ["records", "logs"].includes(kind);
  return <section className="qfr-card" id="holding"><header><h2>结果明细</h2><span>金额、纳秒与原保存值完整展示</span></header>
    <div className="qfr-table-controls"><label>明细类型<Select value={kind} onChange={e => { const next = new URLSearchParams(location.search); next.set("detail", e.target.value); next.delete("after"); navigate({ pathname: location.pathname, search: next.toString(), hash: "#holding" }, { state: location.state }); }}>{Object.entries(RESULT_KINDS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</Select></label></div>
    <PageFeedback data={data}/>{!enabled && <p className="qfr-empty">等待运行终结后读取保存结果。</p>}
    {unsupported && <p className="qfr-note">旧历史没有保存此类结果时明确不可用，不执行旧引擎补算。</p>}
    {data.page && <>{data.page.items.length ? <div className="qfr-table-scroll">{fields ? <table><thead><tr>{fields.map(([label]) => <th key={label}>{label}</th>)}<th>原始记录</th></tr></thead><tbody>{data.page.items.map((row: ResultRow, i) => <tr key={row.result_sequence || row.id || i}>{fields.map(([label, keys]) => <td key={label}><Cell value={keys.map(key => row[key]).find(v => v !== undefined)}/></td>)}<td><details><summary>查看原值</summary><pre>{JSON.stringify(row, null, 2)}</pre></details></td></tr>)}</tbody></table> : <div className="qfr-records">{data.page.items.map((row, i) => <article key={row.result_sequence || i}><strong>{RESULT_KINDS[kind]} · {row.result_sequence || i + 1}</strong><pre>{JSON.stringify(row, null, 2)}</pre></article>)}</div>}</div> : <p className="qfr-empty">服务确认本页没有已保存记录。</p>}
    <p className="qfr-note">{data.page.notices?.logs_truncated ? "日志已截断，原服务通知保留在本页。" : "分页展示；未读取的记录不参与本页统计。"}</p><PageNavigation data={data} cursorKey="after" title="明细"/></>}
  </section>;
}
