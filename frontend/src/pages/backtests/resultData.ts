import { fetchBacktestResult, type BacktestResultPage } from "../../api/backtestRuns";
import { finite } from "./workbench";
export type ResultRow = Record<string, any>;
export const RESULT_KINDS = { steps: "运行步骤", decisions: "策略决策", orders: "订单", fills: "成交", positions: "持仓", equity: "权益", metrics: "指标", events: "运行事件", "order-updates": "订单变更", "data-chunks": "数据分块", "data-preflight": "数据检查" };
/** Consume opaque cursors without silently returning a truncated report. The
 * same reader serves charts and export so pagination has one integrity rule. */
export async function readAllResults(runId: string, kind: string, signal: AbortSignal, onProgress?: (count: number) => void, reader = fetchBacktestResult): Promise<ResultRow[]> {
  const rows: ResultRow[] = [], seen = new Set<string>();
  let cursor: string | undefined;
  do {
    signal.throwIfAborted();
    const page: BacktestResultPage = await reader(runId, kind, cursor, signal);
    signal.throwIfAborted();
    if (page.truncated && !page.next_cursor) throw new Error("结果已被截断，无法作为完整结果读取或导出。");
    rows.push(...page.items); onProgress?.(rows.length);
    cursor = page.next_cursor || undefined;
    if (page.has_more && !cursor || cursor && seen.has(cursor)) throw new Error("结果分页异常，请重试。");
    if (cursor) seen.add(cursor);
  } while (cursor);
  return rows;
}
export function localDay(value: unknown, timezone = "Asia/Shanghai"): string {
  const date = new Date(typeof value === "number" ? value : String(value));
  if (!Number.isFinite(date.getTime())) return "";
  return new Intl.DateTimeFormat("sv-SE", { timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit" }).format(date);
}
export function displayNumber(value: unknown, percent = false) {
  const n = finite(value);
  return n === null ? "—" : percent ? `${n > 0 ? "+" : ""}${(n * 100).toFixed(2)}%` : n.toLocaleString("zh-CN", { maximumFractionDigits: 6 });
}
export interface MonthlyResult { month: string; value: number | null; reason: string; partial: boolean; }
/** A monthly compound is meaningful only when every recorded interval belongs
 * to that month. Block missing/duplicate samples and cross-month intervals
 * after a valuation gap; never reinterpret a missing return as zero. */
export function monthlyResults(rows: ResultRow[], start: string, end: string, timezone = "Asia/Shanghai"): MonthlyResult[] {
  const groups = new Map<string, { product: number; reason: string }>();
  let previousDay = "", gap = false, invalidTime = false;
  const seen = new Set<string>();
  for (const row of rows) {
    const day = localDay(row.as_of, timezone), month = day.slice(0, 7);
    if (!day) { invalidTime = true; gap = true; for (const g of groups.values()) g.reason = "存在无效估值时间"; continue; }
    const group = groups.get(month) || { product: 1, reason: "" }; groups.set(month, group);
    const value = finite(row.period_return);
    if (seen.has(day) || previousDay && day < previousDay) group.reason = "估值日期重复或顺序异常";
    seen.add(day);
    if (previousDay && previousDay.slice(0, 7) !== month && (gap || Date.parse(day) - Date.parse(previousDay) > 7 * 86400000)) group.reason = "估值缺失跨越月界，无法划分月收益";
    if (value === null || row.valuation_status === "blocked") { group.reason = "月内存在缺失估值或收益"; gap = true; }
    else { group.product *= 1 + value; if (!Number.isFinite(group.product)) group.reason = "收益数值无法计算"; gap = false; previousDay = day; }
  }
  return [...groups].map(([month, group]) => ({ month, value: group.reason || invalidTime ? null : group.product - 1, reason: invalidTime ? "存在无效估值时间" : group.reason,
    partial: month === start.slice(0, 7) && start.slice(8) !== "01" || month === end.slice(0, 7) && end !== new Date(Date.UTC(Number(month.slice(0, 4)), Number(month.slice(5, 7)), 0)).toISOString().slice(0, 10),
  }));
}
